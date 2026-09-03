import tempfile
import unittest
from pathlib import Path

from runtime_supervisor import CHECK_ORDER, RuntimeSupervisor


def passed(check_id):
    return {
        "check_id": check_id,
        "status": "PASS",
        "summary": f"{check_id} passed",
        "detail": {},
        "remediation": "",
    }


class FakeRuntimeOperations:
    def __init__(self):
        self.bad_roles = []
        self.core_bad = False
        self.proxy_ok = True
        self.packages_ok = True
        self.canary_ok = True
        self.repaired_roles = []
        self.core_repairs = 0
        self.proxy_repairs = 0

    def inspect_runtime(self):
        return {
            "checks": {
                "docker_ports": passed("docker_ports") if not self.core_bad else {
                    **passed("docker_ports"), "status": "FAIL", "remediation": "repair core"
                },
                "worker_quorum": passed("worker_quorum") if not self.bad_roles else {
                    **passed("worker_quorum"), "status": "FAIL", "remediation": "repair worker"
                },
            },
            "core_repair_required": self.core_bad,
            "repairable_roles": list(self.bad_roles),
        }

    def verify_proxy_route(self):
        return passed("ai_proxy_route") if self.proxy_ok else {
            **passed("ai_proxy_route"), "status": "FAIL", "remediation": "repair proxy"
        }

    def verify_worker_packages(self):
        bad = list(self.bad_roles) if not self.packages_ok else []
        return passed("worker_packages") if not bad else {
            **passed("worker_packages"),
            "status": "FAIL",
            "detail": {"mismatched_roles": bad},
            "remediation": "repair worker package",
        }

    def run_model_canary(self):
        if not self.canary_ok:
            raise RuntimeError("UPSTREAM_CANARY_FAILED")
        return {
            **passed("model_canary"),
            "detail": {"provider_call_count": 1, "total_tokens": 9, "business_run_created": False},
        }

    def repair_worker(self, role):
        self.repaired_roles.append(role)
        self.bad_roles = [item for item in self.bad_roles if item != role]
        self.packages_ok = True
        return {"status": "REPAIRED", "role": role, "business_run_created": False}

    def repair_core(self):
        self.core_repairs += 1
        self.core_bad = False
        return {"status": "REPAIRED", "business_run_created": False}

    def repair_proxy(self):
        self.proxy_repairs += 1
        self.proxy_ok = True
        return {"status": "REPAIRED", "business_run_created": False}


class RuntimeSupervisorTests(unittest.TestCase):
    def supervisor(self, operations, active=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return RuntimeSupervisor(
            operations=operations,
            state_root=Path(temporary.name),
            interval_seconds=1,
            expensive_interval_seconds=60,
            max_repairs=3,
            active_business_run=(lambda: active),
        )

    def test_cold_start_requires_all_five_checks_and_real_canary(self):
        operations = FakeRuntimeOperations()
        supervisor = self.supervisor(operations)
        status = supervisor.run_once(full=True, repair=True, run_canary=True)
        self.assertEqual("READY", status["state"])
        self.assertTrue(status["gate_open"])
        self.assertEqual(list(CHECK_ORDER), [item["check_id"] for item in status["checks"]])
        canary = next(item for item in status["checks"] if item["check_id"] == "model_canary")
        self.assertEqual(1, canary["detail"]["provider_call_count"])
        self.assertGreater(canary["detail"]["total_tokens"], 0)
        self.assertFalse(status["new_business_run_created"])

    def test_light_heartbeat_preserves_full_receipts_and_gate(self):
        operations = FakeRuntimeOperations()
        supervisor = self.supervisor(operations)
        supervisor.run_once(full=True, repair=True, run_canary=True)
        status = supervisor.run_once(full=False, repair=False, run_canary=False)
        self.assertTrue(status["gate_open"])
        self.assertEqual(5, len(status["checks"]))

    def test_periodic_check_rebuilds_only_missing_worker_without_business_run(self):
        operations = FakeRuntimeOperations()
        supervisor = self.supervisor(operations)
        supervisor.run_once(full=True, repair=True, run_canary=True)
        operations.bad_roles = ["semantic-impact-analyst"]
        status = supervisor.run_once(full=True, repair=False, run_canary=False)
        self.assertEqual(["semantic-impact-analyst"], operations.repaired_roles)
        self.assertTrue(status["gate_open"])
        self.assertFalse(status["new_business_run_created"])

    def test_failed_canary_closes_gate_with_operational_wait(self):
        operations = FakeRuntimeOperations()
        operations.canary_ok = False
        supervisor = self.supervisor(operations)
        status = supervisor.run_once(full=True, repair=True, run_canary=True)
        self.assertEqual("OPERATIONAL_WAIT", status["state"])
        self.assertFalse(status["gate_open"])
        canary = next(item for item in status["checks"] if item["check_id"] == "model_canary")
        self.assertEqual("FAIL", canary["status"])
        self.assertTrue(status["remediation_actions"])

    def test_canary_does_not_preempt_active_business_run(self):
        operations = FakeRuntimeOperations()
        supervisor = self.supervisor(operations, {"run_id": "RUN-LIVE-ACTIVE", "state": "RUNNING"})
        status = supervisor.run_once(full=True, repair=True, run_canary=True)
        self.assertFalse(status["gate_open"])
        canary = next(item for item in status["checks"] if item["check_id"] == "model_canary")
        self.assertEqual("WAIT", canary["status"])
        self.assertIn("RUN-LIVE-ACTIVE", canary["summary"])


if __name__ == "__main__":
    unittest.main()
