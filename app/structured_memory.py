from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROTOCOL = "FINFLUX_STRUCTURED_MEMORY_V1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


class StructuredMemoryStore:
    """Hash-addressed operational memory; never stores raw financial bytes or CoT."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_root = root / "runs"
        self.skill_root = root / "skill-cache"
        self.failure_root = root / "failures"

    @staticmethod
    def skill_cache_key(receipt: dict[str, Any]) -> str:
        return canonical_sha256(
            {
                "skill_id": receipt.get("skill_id"),
                "version": receipt.get("version"),
                "digest": receipt.get("digest"),
                "input_sha256": receipt.get("input_sha256"),
            }
        )

    def update_run(
        self,
        run: dict[str, Any],
        submission: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = run.get("agent_result") or {}
        artifacts = result.get("worker_artifacts") or {}
        datapass = run.get("datapass") or {}
        gate = run.get("human_gate") or {}
        lifecycle = run.get("lifecycle") or {}
        submission = submission or {}
        memory = {
            "protocol": PROTOCOL,
            "updated_at_utc": utc_now(),
            "run_id": run.get("run_id"),
            "case_id": run.get("case_id"),
            "raw_state": run.get("state"),
            "current_phase": lifecycle.get("current_phase"),
            "parent_run_id": (run.get("lineage") or {}).get("parent_run_id"),
            "child_run_id": (run.get("lineage") or {}).get("child_run_id"),
            "evidence_handles": {
                "submission_id": run.get("submission_id"),
                "evidence_root_hash": submission.get("evidence_root_hash"),
                "source_file_sha256": (submission.get("file") or {}).get("sha256"),
                "change_bundle_id": run.get("change_bundle_id"),
            },
            "route": {
                "decision_id": (run.get("root_route_decision") or {}).get("decision_id"),
                "route": (run.get("root_route_decision") or {}).get("route"),
                "workers": ((run.get("root_route_decision") or {}).get("worker_plan") or {}).get("workers", []),
            },
            "completed_tasks": [
                {
                    "agent_id": agent_id,
                    "task_id": artifact.get("task_id"),
                    "status": artifact.get("status"),
                    "tool_run_id": artifact.get("tool_run_id"),
                    "output_sha256": canonical_sha256(artifact),
                }
                for agent_id, artifact in sorted(artifacts.items())
            ],
            "decision_summary": {
                "machine_recommendation": datapass.get("machine_recommendation"),
                "datapass_sha256": datapass.get("draft_sha256"),
                "human_state": gate.get("state"),
                "human_decision": gate.get("decision"),
                "human_decision_sha256": gate.get("post_decision_hash"),
            },
            "token_summary": {
                key: (run.get("provider_usage") or {}).get(key)
                for key in (
                    "status",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "call_count",
                    "source",
                )
            },
            "privacy": {
                "raw_financial_bytes_stored": False,
                "model_chain_of_thought_stored": False,
                "credentials_stored": False,
                "context_loading": "ROUTE_SELECTED_HASH_HANDLES_ONLY",
            },
        }
        unsigned = dict(memory)
        memory["memory_sha256"] = canonical_sha256(unsigned)
        _atomic(self.run_root / f"{run['run_id']}.json", memory)
        self._index_skill_receipts(run, artifacts)
        self._index_failures(run)
        return memory

    def _index_skill_receipts(
        self, run: dict[str, Any], artifacts: dict[str, Any]
    ) -> None:
        receipts = list((run.get("datapass") or {}).get("skill_invocations") or [])
        if not receipts:
            for artifact in artifacts.values():
                receipts.extend(artifact.get("skill_invocations") or [])
        for receipt in receipts:
            if not receipt.get("skill_id") or not receipt.get("input_sha256"):
                continue
            key = self.skill_cache_key(receipt)
            path = self.skill_root / f"{key}.json"
            record = _read(path, {}) or {}
            observed_runs = sorted(
                set(record.get("observed_run_ids") or []) | {str(run.get("run_id"))}
            )
            record = {
                "protocol": "FINFLUX_SKILL_CACHE_INDEX_V1.0",
                "cache_key": key,
                "skill_id": receipt.get("skill_id"),
                "version": receipt.get("version"),
                "digest": receipt.get("digest"),
                "input_sha256": receipt.get("input_sha256"),
                "output_sha256": receipt.get("output_sha256"),
                "status": receipt.get("status", "SUCCESS"),
                "observed_run_ids": observed_runs,
                "last_observed_at_utc": utc_now(),
                "provider_tokens": int(receipt.get("provider_tokens", 0) or 0),
                "reusable_only_when_digest_and_input_match": True,
                "raw_output_stored": False,
            }
            record["index_sha256"] = canonical_sha256(record)
            _atomic(path, record)

    def _index_failures(self, run: dict[str, Any]) -> None:
        raw_state = str(run.get("state", ""))
        if raw_state not in {
            "AGENTTEAMS_DISPATCH_FAILED",
            "BUDGET_EXCEEDED",
            "RUNTIME_UNAVAILABLE",
            "CANCELLED_BY_SESSION_RESET",
            "FAILED",
        }:
            return
        record = {
            "protocol": "FINFLUX_FAILURE_MEMORY_V1.0",
            "run_id": run.get("run_id"),
            "case_id": run.get("case_id"),
            "state": raw_state,
            "recorded_at_utc": utc_now(),
            "last_event": (run.get("events") or [{}])[-1],
            "resume_from": (run.get("lifecycle") or {}).get("current_phase"),
            "provider_tokens_at_failure": (run.get("provider_usage") or {}).get(
                "total_tokens"
            ),
        }
        record["failure_sha256"] = canonical_sha256(record)
        _atomic(self.failure_root / f"{run['run_id']}.json", record)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return _read(self.run_root / f"{run_id}.json")

    def status(self) -> dict[str, Any]:
        runs = list(self.run_root.glob("RUN-*.json"))
        cache = list(self.skill_root.glob("*.json"))
        failures = list(self.failure_root.glob("RUN-*.json"))
        return {
            "protocol": "FINFLUX_MEMORY_STATUS_V1.0",
            "run_memories": len(runs),
            "skill_cache_entries": len(cache),
            "failure_memories": len(failures),
            "raw_financial_bytes_stored": False,
            "model_chain_of_thought_stored": False,
            "cache_policy": "skill digest + input SHA256 must both match",
        }

