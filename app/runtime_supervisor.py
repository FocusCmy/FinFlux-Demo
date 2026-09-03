"""Cold-start admission and infrastructure self-healing for FinFlux.

This supervisor owns the AgentTeams runtime, not a financial business Run.
It proves Docker/Team topology, the authenticated AI Proxy route, deployed
Worker package bytes and one real model call before the UI may create a Run.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agentteams_runtime.config import (
    CONTROLLER,
    DOCKER_CONTEXT,
    MANAGER,
    TEAM_NAME,
    env_path,
    read_env,
)
from agentteams_runtime.gateway import capture_usage
from agentteams_runtime.runtime import docker, resource
from model_gateway_control import close_model_gateway_run, prepare_model_gateway_run


ROUTE_WORKERS = (
    "evidence-investigator",
    "semantic-impact-analyst",
    "downstream-impact-analyst",
    "data-rights-steward",
    "research-context-analyst",
    "runtime-resilience-auditor",
    "independent-validator",
    "result-composer",
)
LEADER = "finchange-case-lead"
EXPECTED_WORKER_CONTAINERS = {
    role: f"agentteams-worker-{role}" for role in (LEADER, *ROUTE_WORKERS)
}
CHECK_ORDER = (
    "docker_ports",
    "worker_quorum",
    "ai_proxy_route",
    "worker_packages",
    "model_canary",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    # Do not persist bearer material accidentally returned by a dependency.
    text = re.sub(r"(?i)(authorization|api[_-]?key|token)\s*[:=]\s*\S+", r"\1=<redacted>", text)
    return text[:1000]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _check(
    check_id: str,
    status: str,
    summary: str,
    *,
    detail: Any = None,
    remediation: str = "",
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": status,
        "summary": summary,
        "detail": detail,
        "remediation": remediation,
        "checked_at_utc": utc_now(),
    }


class RuntimeOperations:
    """Concrete local Docker/AgentTeams operations used by RuntimeSupervisor."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.agentteams_root = self.project_root / "agentteams"
        self.control_root = self.project_root / "app" / "runtime" / "model_gateway"

    @staticmethod
    def _container_rows() -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        raw = docker("ps", "-a", "--format", "{{json .}}", timeout=20)
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = str(item.get("Names") or "")
            if name:
                rows[name] = item
        return rows

    @staticmethod
    def _excluded_port_ranges() -> list[tuple[int, int]]:
        if os.name != "nt":
            return []
        try:
            completed = subprocess.run(
                ["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        ranges = []
        for line in completed.stdout.splitlines():
            match = re.match(r"^\s*(\d+)\s+(\d+)(?:\s+\*)?\s*$", line)
            if match:
                ranges.append((int(match.group(1)), int(match.group(2))))
        return ranges

    def inspect_runtime(self) -> dict[str, Any]:
        rows = self._container_rows()
        excluded = self._excluded_port_ranges()
        expected = (CONTROLLER, MANAGER, *EXPECTED_WORKER_CONTAINERS.values())
        missing = [name for name in expected if name not in rows]
        stopped = [
            name
            for name in expected
            if name in rows and str(rows[name].get("State") or "").lower() != "running"
        ]
        port_issues: list[dict[str, Any]] = []
        dynamic_ports: dict[str, int] = {}
        for role, name in EXPECTED_WORKER_CONTAINERS.items():
            ports_text = str((rows.get(name) or {}).get("Ports") or "")
            matches = re.findall(r"(?:127\.0\.0\.1:|0\.0\.0\.0:|\[::\]:)?(\d+)->8088/tcp", ports_text)
            if matches:
                port = int(matches[0])
                dynamic_ports[role] = port
                hit = next(((start, end) for start, end in excluded if start <= port <= end), None)
                if hit:
                    port_issues.append(
                        {
                            "role": role,
                            "container": name,
                            "host_port": port,
                            "reason": f"WINDOWS_EXCLUDED_PORT:{hit[0]}-{hit[1]}",
                        }
                    )
        conflict_roles = sorted(
            {
                role
                for role, name in EXPECTED_WORKER_CONTAINERS.items()
                if name in stopped
            }
            | {str(item["role"]) for item in port_issues}
        )
        team = resource("teams", TEAM_NAME) if CONTROLLER in rows else None
        team_status = dict(team or {})
        if isinstance(team_status.get("status"), dict):
            team_status = dict(team_status["status"])
        worker_names = tuple(str(v) for v in team_status.get("workerNames") or ())
        ready = int(team_status.get("readyWorkers") or 0)
        total = int(team_status.get("totalWorkers") or 0)
        quorum_ok = (
            str(team_status.get("phase") or "").upper() == "ACTIVE"
            and ready == 8
            and total == 8
            and set(worker_names) == set(ROUTE_WORKERS)
            and all(
                str((rows.get(EXPECTED_WORKER_CONTAINERS[role]) or {}).get("State") or "").lower()
                == "running"
                for role in ROUTE_WORKERS
            )
        )
        ports_ok = not missing and not stopped and not port_issues
        return {
            "checks": {
                "docker_ports": _check(
                    "docker_ports",
                    "PASS" if ports_ok else "FAIL",
                    "Docker容器和动态端口无冲突" if ports_ok else "发现缺失、停止或保留端口冲突",
                    detail={
                        "missing_containers": missing,
                        "stopped_containers": stopped,
                        "worker_host_ports": dynamic_ports,
                        "port_issues": port_issues,
                    },
                    remediation="自动重建异常角色容器；核心容器异常时执行一键修复运行环境。",
                ),
                "worker_quorum": _check(
                    "worker_quorum",
                    "PASS" if quorum_ok else "FAIL",
                    f"AgentTeams Worker {ready}/{total}",
                    detail={
                        "team": TEAM_NAME,
                        "phase": team_status.get("phase"),
                        "ready_workers": ready,
                        "total_workers": total,
                        "worker_names": list(worker_names),
                    },
                    remediation="恢复缺失Worker，并要求Team回读严格等于8/8。",
                ),
            },
            "repairable_roles": conflict_roles,
            "core_repair_required": any(name in missing or name in stopped for name in (CONTROLLER, MANAGER)),
        }

    def verify_proxy_route(self) -> dict[str, Any]:
        env = read_env()
        username = str(env.get("AGENTTEAMS_ADMIN_USER") or "").strip()
        password = str(env.get("AGENTTEAMS_ADMIN_PASSWORD") or "")
        if not username or not password:
            raise RuntimeError("HIGRESS_CONSOLE_CREDENTIALS_MISSING")
        port = int(env.get("AGENTTEAMS_PORT_CONSOLE") or 18001)
        base = f"http://127.0.0.1:{port}"
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        login = urllib.request.Request(
            base + "/session/login",
            data=json.dumps({"username": username, "password": password}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(login, timeout=10) as response:
            # Higress currently returns 201 Created for a successful session;
            # accept the complete HTTP success class, then prove the session by
            # performing both authenticated readbacks below.
            if int(response.status) < 200 or int(response.status) >= 300:
                raise RuntimeError("HIGRESS_LOGIN_FAILED")

        def get(path: str) -> dict[str, Any]:
            with opener.open(base + path, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                payload = payload["data"]
            if not isinstance(payload, dict):
                raise RuntimeError("HIGRESS_READBACK_INVALID")
            return payload

        provider = get("/v1/ai/providers/openai-compat")
        source = get("/v1/service-sources/openai-compat")
        raw = provider.get("rawConfigs") or {}
        expected_url = "http://finflux-model-budget-gateway.local:8090/v1"
        ok = (
            str(raw.get("openaiCustomUrl") or "").rstrip("/") == expected_url
            and str(raw.get("openaiCustomServiceName") or "") == "openai-compat.dns"
            and int(raw.get("openaiCustomServicePort") or 0) == 8090
            and str(source.get("type") or "") == "dns"
            and str(source.get("domain") or "") == "finflux-model-budget-gateway.local"
            and int(source.get("port") or 0) == 8090
            and str(source.get("protocol") or "") == "http"
        )
        return _check(
            "ai_proxy_route",
            "PASS" if ok else "FAIL",
            "AI Proxy已认证回读并固定到8090" if ok else "AI Proxy路由未指向受控8090网关",
            detail={
                "provider": "openai-compat",
                "custom_url": raw.get("openaiCustomUrl"),
                "service_name": raw.get("openaiCustomServiceName"),
                "service_port": raw.get("openaiCustomServicePort"),
                "source_domain": source.get("domain"),
                "source_port": source.get("port"),
                "authenticated_readback": True,
                "secret_exposed": False,
            },
            remediation="重新部署模型预算网关并认证回读Higress Provider与Service Source。",
        )

    @staticmethod
    def _zip_tree_digest(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        count = 0
        with zipfile.ZipFile(path) as archive:
            for name in sorted(item.filename for item in archive.infolist() if not item.is_dir()):
                file_hash = _sha_bytes(archive.read(name))
                digest.update(name.encode("utf-8") + b"\0" + file_hash.encode("ascii") + b"\n")
                count += 1
        return digest.hexdigest(), count

    def verify_worker_packages(self) -> dict[str, Any]:
        script = (
            "import hashlib,json,pathlib,sys;root=pathlib.Path(sys.argv[1]);"
            "rows=[];"
            "[(rows.append((p.relative_to(root).as_posix(),hashlib.sha256(p.read_bytes()).hexdigest()))) "
            "for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'];"
            "h=hashlib.sha256();"
            "[h.update(n.encode()+b'\\0'+d.encode()+b'\\n') for n,d in sorted(rows)];"
            "print(json.dumps({'tree_sha256':h.hexdigest(),'file_count':len(rows)}))"
        )
        rows = []
        mismatches = []
        for role in ROUTE_WORKERS:
            package = self.agentteams_root / "build" / "packages" / f"{role}.zip"
            if not package.is_file():
                rows.append({"role": role, "status": "MISSING_REPOSITORY_PACKAGE"})
                mismatches.append(role)
                continue
            expected, expected_count = self._zip_tree_digest(package)
            container = EXPECTED_WORKER_CONTAINERS[role]
            root = f"/root/agentteams-fs/agents/{role}/.qwenpaw/agent-packages/current"
            try:
                observed = json.loads(docker("exec", container, "python3", "-c", script, root, timeout=25))
            except Exception as exc:  # noqa: BLE001
                rows.append({"role": role, "status": "READBACK_FAILED", "error": _safe_error(exc)})
                mismatches.append(role)
                continue
            matched = (
                observed.get("tree_sha256") == expected
                and int(observed.get("file_count") or 0) == expected_count
            )
            rows.append(
                {
                    "role": role,
                    "status": "MATCH" if matched else "MISMATCH",
                    "repository_tree_sha256": expected,
                    "container_tree_sha256": observed.get("tree_sha256"),
                    "repository_file_count": expected_count,
                    "container_file_count": observed.get("file_count"),
                }
            )
            if not matched:
                mismatches.append(role)
        return _check(
            "worker_packages",
            "PASS" if not mismatches else "FAIL",
            "8/8容器Worker包与仓库字节摘要一致" if not mismatches else "Worker包摘要不一致",
            detail={"workers": rows, "mismatched_roles": mismatches},
            remediation="只重建摘要不一致的同角色Worker容器，不创建业务Run。",
        )

    def _run_powershell(self, script: Path, *args: str, timeout: int = 360) -> str:
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        if completed.returncode:
            raise RuntimeError(f"{script.name} exit {completed.returncode}: {output[-1200:]}")
        return output[-1200:]

    def repair_worker(self, role: str) -> dict[str, Any]:
        if role not in ROUTE_WORKERS:
            raise ValueError(f"ROLE_NOT_AUTOMATICALLY_REPAIRABLE:{role}")
        output = self._run_powershell(
            self.agentteams_root / "scripts" / "Repair-AgentTeamsRole.ps1",
            "-Role",
            role,
            "-EnvFile",
            str(env_path()),
            timeout=300,
        )
        return {
            "role": role,
            "status": "REBUILT_SAME_ROLE",
            "new_business_run_created": False,
            "log_tail": output,
        }

    def repair_proxy(self) -> dict[str, Any]:
        output = self._run_powershell(
            self.agentteams_root / "scripts" / "Deploy-ModelBudgetGateway.ps1",
            "-EnvFile",
            str(env_path()),
            timeout=360,
        )
        return {"status": "ROUTE_REPAIRED", "log_tail": output}

    def repair_core(self) -> dict[str, Any]:
        output = self._run_powershell(
            self.agentteams_root / "scripts" / "Deploy-AgentTeams.ps1",
            "-EnvFile",
            str(env_path()),
            timeout=900,
        )
        return {"status": "CORE_RECONCILED", "log_tail": output}

    @staticmethod
    def _configure_canary_headers(run_id: str, identity: str, task_id: str) -> None:
        headers = {
            "X-FinFlux-Run-ID": run_id,
            "X-FinFlux-Actor": "evidence-investigator",
            "X-FinFlux-Identity": identity,
            "X-FinFlux-Task-ID": task_id,
        }
        script = (
            "import json,sys,urllib.request;"
            "body=json.dumps({'custom_headers':json.loads(sys.argv[1])}).encode();"
            "req=urllib.request.Request('http://127.0.0.1:8088/api/models/agentteams-gateway/config',"
            "data=body,headers={'Content-Type':'application/json'},method='PUT');"
            "r=urllib.request.urlopen(req,timeout=15);print(r.status)"
        )
        result = docker(
            "exec",
            EXPECTED_WORKER_CONTAINERS["evidence-investigator"],
            "python3",
            "-c",
            script,
            json.dumps(headers, separators=(",", ":")),
            timeout=25,
        )
        if result.strip() != "200":
            raise RuntimeError("CANARY_ACTOR_HEADER_BINDING_FAILED")

    def run_model_canary(self) -> dict[str, Any]:
        env = read_env()
        model = str(env.get("FINCHANGE_EVIDENCE_MODEL") or env.get("AGENTTEAMS_DEFAULT_MODEL") or "").strip()
        if not model:
            raise RuntimeError("CANARY_MODEL_NOT_CONFIGURED")
        run_id = "RUNTIME-CANARY-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
        task_id = run_id + "-provider-roundtrip"
        identity = secrets.token_urlsafe(32)
        baseline = capture_usage()
        prepare_model_gateway_run(
            self.control_root,
            run_id=run_id,
            provider_token_hard_cap=4096,
            max_model_calls=1,
            max_output_tokens_per_call=32,
            max_wall_time_seconds=90,
            provider_usage_baseline=baseline,
            actor_identities={"evidence-investigator": identity},
            actor_task_ids={"evidence-investigator": task_id},
        )
        passed = False
        response_summary: dict[str, Any] = {}
        try:
            self._configure_canary_headers(run_id, identity, task_id)
            # Use the Worker's encrypted provider store rather than copying a
            # credential into host argv.  The key is decrypted and consumed
            # only inside the Worker process; output contains hashes/counts,
            # never the key or generated text.
            script = r'''import hashlib,json,urllib.request
from qwenpaw.constant import SECRET_DIR
from qwenpaw.security.secret_store import PROVIDER_SECRET_FIELDS,decrypt_dict_fields
path=SECRET_DIR/"providers"/"custom"/"agentteams-gateway.json"
p=decrypt_dict_fields(json.load(open(path,encoding="utf-8")),PROVIDER_SECRET_FIELDS)
active=json.load(urllib.request.urlopen("http://127.0.0.1:8088/api/models/active?scope=agent&agent_id=default",timeout=10))
active_llm=active.get("active_llm") or active
if active_llm.get("provider_id")!="agentteams-gateway": raise RuntimeError("CANARY_ACTIVE_PROVIDER_NOT_AGENTTEAMS_GATEWAY")
body=json.dumps({"model":active_llm["model"],"messages":[{"role":"user","content":"Reply exactly FINFLUX_RUNTIME_CANARY_OK"}],"max_tokens":24,"temperature":0,"stream":False}).encode()
headers={"Content-Type":"application/json","Authorization":"Bearer "+p["api_key"],**(p.get("custom_headers") or {})}
req=urllib.request.Request(p["base_url"].rstrip("/")+"/chat/completions",data=body,headers=headers,method="POST")
with urllib.request.urlopen(req,timeout=60) as r: raw=r.read(); status=r.status
payload=json.loads(raw); choices=payload.get("choices") or []
if not choices: raise RuntimeError("CANARY_PROVIDER_CHOICES_MISSING")
content=str(((choices[0].get("message") or {}).get("content") or (choices[0].get("message") or {}).get("reasoning_content") or ""))
print(json.dumps({"http_status":status,"choices":len(choices),"content_length":len(content),"response_sha256":hashlib.sha256(raw).hexdigest()}))'''
            response_summary = json.loads(
                docker(
                    "exec",
                    EXPECTED_WORKER_CONTAINERS["evidence-investigator"],
                    "/opt/venv/qwenpaw/bin/python",
                    "-c",
                    script,
                    timeout=75,
                )
            )
            ledger = json.loads((self.control_root / "gateway_ledger.json").read_text(encoding="utf-8"))
            calls = int(ledger.get("provider_call_count") or 0)
            tokens = int(ledger.get("total_tokens") or 0)
            passed = calls == 1 and tokens > 0 and int(response_summary.get("http_status") or 0) == 200
            if not passed:
                raise RuntimeError("CANARY_LEDGER_DID_NOT_RECORD_ONE_REAL_PROVIDER_CALL")
            return _check(
                "model_canary",
                "PASS",
                f"真实模型网关canary通过：1次调用，{tokens} Token",
                detail={
                    "canary_id": run_id,
                    "model": model,
                    "provider_call_count": calls,
                    "total_tokens": tokens,
                    "gateway_ledger_sha256": ledger.get("ledger_sha256"),
                    **response_summary,
                    "business_run_created": False,
                    "secret_exposed": False,
                },
                remediation="",
            )
        finally:
            try:
                close_model_gateway_run(
                    self.control_root,
                    run_id=run_id,
                    state="RUNTIME_CANARY_PASSED" if passed else "RUNTIME_CANARY_FAILED",
                )
            except Exception:
                pass


class RuntimeSupervisor:
    """Background cold-start gate with bounded, role-local self healing."""

    def __init__(
        self,
        *,
        operations: RuntimeOperations,
        state_root: Path,
        interval_seconds: float = 5.0,
        expensive_interval_seconds: float = 60.0,
        max_repairs: int = 3,
        active_business_run: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.operations = operations
        self.state_root = Path(state_root)
        self.status_path = self.state_root / "status.json"
        self.log_path = self.state_root / "events.jsonl"
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.expensive_interval_seconds = max(self.interval_seconds, float(expensive_interval_seconds))
        self.max_repairs = max(1, int(max_repairs))
        self.active_business_run = active_business_run
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._repair_requested = True
        self._canary_requested = True
        self._last_expensive = 0.0
        self._repair_counts: dict[str, int] = {}
        self._status = {
            "protocol": "FINFLUX_RUNTIME_SUPERVISOR_V1",
            "state": "STOPPED",
            "gate_open": False,
            "cold_start_complete": False,
            "checks": [],
            "errors": [],
            "repair_attempts": [],
            "interval_seconds": self.interval_seconds,
            "expensive_interval_seconds": self.expensive_interval_seconds,
            "max_repairs": self.max_repairs,
            "new_business_run_created": False,
            "last_action": "NOT_STARTED",
            "log_path": str(self.log_path),
        }
        self._persist()

    def _persist(self) -> None:
        with self._lock:
            payload = json.loads(json.dumps(self._status, ensure_ascii=False))
        payload["updated_at_utc"] = utc_now()
        _atomic_json(self.status_path, payload)

    def _event(self, action: str, **detail: Any) -> None:
        row = {"at_utc": utc_now(), "action": action, **detail}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._status, ensure_ascii=False))

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._status.update(
                state="STARTING",
                gate_open=False,
                cold_start_complete=False,
                last_action="COLD_START_CHECK_QUEUED",
            )
            self._persist()
            self._thread = threading.Thread(
                target=self._loop, name="finflux-runtime-supervisor", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def request_repair(self, *, actor: str = "demo.operator", reason: str = "") -> dict[str, Any]:
        with self._lock:
            if self._status.get("state") in {"CHECKING", "REPAIRING"}:
                return {"status": "ALREADY_RUNNING", **self.status()}
            self._repair_requested = True
            self._canary_requested = True
            self._status.update(
                state="REPAIRING",
                gate_open=False,
                last_action="MANUAL_REPAIR_QUEUED",
                requested_by=str(actor or "demo.operator"),
                request_reason=str(reason or "一键修复运行环境")[:240],
            )
            self._persist()
        self._event("MANUAL_REPAIR_QUEUED", actor=actor, reason=reason[:240])
        self._wake.set()
        return {"status": "ACCEPTED", **self.status()}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                full = self._repair_requested or (time.monotonic() - self._last_expensive >= self.expensive_interval_seconds)
                self.run_once(full=full, repair=self._repair_requested, run_canary=self._canary_requested)
                self._repair_requested = False
                self._canary_requested = False
                if full:
                    self._last_expensive = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                error = _safe_error(exc)
                with self._lock:
                    self._status.update(
                        state="OPERATIONAL_WAIT",
                        gate_open=False,
                        cold_start_complete=False,
                        last_action="SUPERVISOR_TICK_FAILED",
                        errors=[error],
                    )
                    self._persist()
                self._event("SUPERVISOR_TICK_FAILED", error=error)
            self._wake.wait(self.interval_seconds)
            self._wake.clear()

    def _repair_role(self, role: str, reason: str) -> dict[str, Any]:
        count = self._repair_counts.get(role, 0)
        if count >= self.max_repairs:
            raise RuntimeError(f"ROLE_REPAIR_LIMIT_REACHED:{role}:{count}")
        self._repair_counts[role] = count + 1
        receipt = self.operations.repair_worker(role)
        event = {
            "target": role,
            "attempt": count + 1,
            "reason": reason,
            "receipt": receipt,
            "at_utc": utc_now(),
        }
        with self._lock:
            attempts = list(self._status.get("repair_attempts") or [])
            attempts.append(event)
            self._status["repair_attempts"] = attempts[-30:]
        self._event("WORKER_REBUILT", role=role, attempt=count + 1, reason=reason)
        return receipt

    def run_once(self, *, full: bool, repair: bool, run_canary: bool) -> dict[str, Any]:
        with self._lock:
            keep_admission_during_probe = bool(
                self._status.get("gate_open")
                and self._status.get("cold_start_complete")
                and not repair
            )
            self._status.update(
                state="REPAIRING" if repair else "CHECKING",
                gate_open=keep_admission_during_probe,
                last_action="RUNTIME_RECONCILIATION",
                errors=[],
            )
            self._persist()
        snapshot = self.operations.inspect_runtime()
        checks = dict(snapshot.get("checks") or {})
        errors: list[str] = []

        if repair and snapshot.get("core_repair_required"):
            try:
                self.operations.repair_core()
                snapshot = self.operations.inspect_runtime()
                checks.update(snapshot.get("checks") or {})
            except Exception as exc:  # noqa: BLE001
                errors.append(_safe_error(exc))

        repair_roles = list(snapshot.get("repairable_roles") or [])
        # A stopped/invalid Worker is a role-local infrastructure fault.  It is
        # always reconciled in-place (subject to the three-attempt fuse), even
        # during an ordinary periodic full check.  This never creates a
        # business Run and never replays healthy roles.
        if repair or repair_roles:
            if repair_roles:
                with self._lock:
                    self._status.update(
                        state="REPAIRING",
                        gate_open=False,
                        last_action="ROLE_LOCAL_REPAIR",
                    )
                    self._persist()
            for role in repair_roles:
                if role == LEADER:
                    errors.append("CASE_LEAD_REPAIR_REQUIRES_CORE_RECONCILIATION")
                    continue
                try:
                    self._repair_role(role, "CONTAINER_STOPPED_OR_PORT_CONFLICT")
                except Exception as exc:  # noqa: BLE001
                    errors.append(_safe_error(exc))
            if repair_roles:
                snapshot = self.operations.inspect_runtime()
                checks.update(snapshot.get("checks") or {})

        if full:
            try:
                proxy = self.operations.verify_proxy_route()
                if proxy["status"] != "PASS":
                    with self._lock:
                        self._status.update(state="REPAIRING", gate_open=False, last_action="AI_PROXY_REPAIR")
                        self._persist()
                    self.operations.repair_proxy()
                    proxy = self.operations.verify_proxy_route()
                checks["ai_proxy_route"] = proxy
            except Exception as exc:  # noqa: BLE001
                if repair or full:
                    try:
                        self.operations.repair_proxy()
                        checks["ai_proxy_route"] = self.operations.verify_proxy_route()
                    except Exception as repair_exc:  # noqa: BLE001
                        errors.append(_safe_error(repair_exc))
                else:
                    errors.append(_safe_error(exc))
                checks.setdefault(
                    "ai_proxy_route",
                    _check("ai_proxy_route", "FAIL", "AI Proxy认证回读失败", remediation="点击一键修复运行环境。"),
                )
            try:
                packages = self.operations.verify_worker_packages()
                bad_roles = list((packages.get("detail") or {}).get("mismatched_roles") or [])
                if bad_roles:
                    with self._lock:
                        self._status.update(state="REPAIRING", gate_open=False, last_action="PACKAGE_REPAIR")
                        self._persist()
                    for role in bad_roles:
                        self._repair_role(role, "PACKAGE_DIGEST_MISMATCH")
                    packages = self.operations.verify_worker_packages()
                checks["worker_packages"] = packages
            except Exception as exc:  # noqa: BLE001
                errors.append(_safe_error(exc))
                checks["worker_packages"] = _check(
                    "worker_packages", "FAIL", "Worker包摘要校验失败", remediation="点击一键修复运行环境。"
                )

        previous = {item.get("check_id"): item for item in self.status().get("checks") or []}
        if not full:
            # Five-second heartbeats deliberately avoid the expensive proxy,
            # package and model calls.  Preserve their last full-check receipts
            # instead of accidentally closing a healthy admission gate merely
            # because the lightweight snapshot contains only liveness checks.
            for check_id in ("ai_proxy_route", "worker_packages", "model_canary"):
                prior = previous.get(check_id)
                if prior:
                    checks[check_id] = prior
        if run_canary:
            active = self.active_business_run() if self.active_business_run else None
            active_state = str((active or {}).get("state") or "")
            canary_blocked = bool(active and active_state not in {"", "AWAITING_HUMAN"})
            if canary_blocked:
                prior = previous.get("model_canary") or {}
                if prior.get("status") == "PASS":
                    checks["model_canary"] = prior
                else:
                    checks["model_canary"] = _check(
                        "model_canary",
                        "WAIT",
                        f"现有业务Run {active.get('run_id')} 正在执行，暂不抢占其模型网关",
                        remediation="等待现有Run进入Human Gate后点击一键修复运行环境。",
                    )
            else:
                try:
                    checks["model_canary"] = self.operations.run_model_canary()
                except Exception as exc:  # noqa: BLE001
                    errors.append(_safe_error(exc))
                    checks["model_canary"] = _check(
                        "model_canary",
                        "FAIL",
                        "真实模型网关canary失败",
                        detail={"error": _safe_error(exc), "business_run_created": False},
                        remediation="检查供应商额度/网络和8090路由，然后点击一键修复运行环境。",
                    )
        elif "model_canary" not in checks and previous.get("model_canary"):
            checks["model_canary"] = previous["model_canary"]

        ordered = [checks[key] for key in CHECK_ORDER if key in checks]
        failed = [item for item in ordered if item.get("status") != "PASS"]
        gate_open = not failed and not errors and len(ordered) == len(CHECK_ORDER)
        remediation = [
            item.get("remediation") for item in failed if item.get("remediation")
        ]
        with self._lock:
            self._status.update(
                state="READY" if gate_open else "OPERATIONAL_WAIT",
                gate_open=gate_open,
                cold_start_complete=gate_open,
                checks=ordered,
                errors=errors,
                remediation_actions=list(dict.fromkeys(remediation)),
                last_action="COLD_START_ADMISSION_OPEN" if gate_open else "RUNTIME_REPAIR_REQUIRED",
                last_full_check_utc=utc_now() if full else self._status.get("last_full_check_utc"),
                last_heartbeat_utc=utc_now(),
            )
            self._persist()
        self._event(
            "RUNTIME_READY" if gate_open else "RUNTIME_OPERATIONAL_WAIT",
            failed_checks=[item.get("check_id") for item in failed],
            errors=errors,
        )
        return self.status()
