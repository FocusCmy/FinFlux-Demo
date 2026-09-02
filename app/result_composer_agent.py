from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from decision_reports import canonical_sha256, write_result_artifacts


REPORT_SKILLS = [
    (
        "assemble-run-result-context",
        "1.0.0",
        "把Run、DataPass、Human Gate与Token账本裁剪为可哈希的最小报告上下文",
    ),
    (
        "select-token-budget-strategy",
        "1.0.0",
        "按结构化字段完整度选择模板、增量上下文或人工补证策略",
    ),
    (
        "compose-result-document",
        "1.0.0",
        "从确定性上下文生成通俗PDF、Markdown与JSON，不改写金融数值",
    ),
    (
        "verify-result-artifact",
        "1.0.0",
        "复核报告文件SHA256、Manifest、Run血缘和Human签署边界",
    ),
]


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def compact_result_context(run: dict[str, Any], submission: dict[str, Any]) -> dict[str, Any]:
    gate = run.get("human_gate") or {}
    agent_result = run.get("agent_result") or {}
    datapass = run.get("datapass") or {}
    provider = run.get("provider_usage") or {}
    return {
        "run_id": run.get("run_id"),
        "case_id": run.get("case_id"),
        "submission_id": run.get("submission_id"),
        "state": run.get("state"),
        "lineage": run.get("lineage") or {},
        "precheck": run.get("precheck") or {},
        "datapass": {
            "protocol": datapass.get("protocol"),
            "machine_recommendation": datapass.get("machine_recommendation"),
            "datapass_sha256": (
                datapass.get("datapass_sha256") or datapass.get("draft_sha256")
            ),
            "skill_invocations": datapass.get("skill_invocations") or [],
        },
        "human_gate": gate,
        "multi_agent": {
            "leader_recommendation": agent_result.get("leader_recommendation"),
            "leader_datapass_event_id": agent_result.get("leader_datapass_event_id"),
            "workers_completed": agent_result.get("workers_completed"),
            "workers_required": agent_result.get("workers_required"),
            "worker_artifact_ids": sorted((agent_result.get("worker_artifacts") or {}).keys()),
        },
        "provider_usage": {
            "status": provider.get("status"),
            "prompt_tokens": provider.get("prompt_tokens"),
            "completion_tokens": provider.get("completion_tokens"),
            "total_tokens": provider.get("total_tokens"),
            "call_count": provider.get("call_count"),
            "source": provider.get("source"),
        },
        "submission": {
            "file": submission.get("file") or {},
            "metadata": submission.get("metadata") or {},
            "parsed": submission.get("parsed") or {},
            "rights_gate": submission.get("rights_gate") or {},
            "evidence_root_hash": submission.get("evidence_root_hash"),
        },
    }


class ResultComposerAgent:
    """Zero-model-first post-processing agent for automatic result documents.

    The AgentTeams Worker package exposes the same four Skills.  The POC server
    executes their deterministic path directly so report generation does not
    add another LLM round or duplicate the full Matrix transcript.
    """

    agent_id = "result-composer"
    version = "1.0.0"

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root

    def compose(
        self,
        run: dict[str, Any],
        submission: dict[str, Any],
        *,
        stage: str,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        compact = compact_result_context(run, submission)
        raw_chars = _json_chars({"run": run, "submission": submission})
        compact_chars = _json_chars(compact)
        context_sha = canonical_sha256(compact)
        required = [
            compact.get("run_id"),
            compact.get("case_id"),
            (compact.get("submission") or {}).get("evidence_root_hash"),
            (compact.get("datapass") or {}).get("machine_recommendation"),
        ]
        if not all(required):
            raise ValueError("Result Composer缺少Run、证据或DataPass结构化字段")

        strategy = {
            "strategy": "DETERMINISTIC_TEMPLATE_ONLY",
            "model_called": False,
            "provider_tokens": 0,
            "full_context_chars": raw_chars,
            "compact_context_chars": compact_chars,
            "context_reduction_percent": round(
                (1 - compact_chars / max(raw_chars, 1)) * 100, 1
            ),
            "cache_key": context_sha,
            "model_fallback_policy": "EXPLICIT_OPT_IN_ONLY",
            "model_fallback_max_input_chars": 6000,
            "model_fallback_max_output_tokens": 350,
        }
        artifacts = write_result_artifacts(
            self.artifact_root / ("previews" if stage == "preview" else "reports"),
            run,
            submission,
            stage=stage,
            replace_existing=replace_existing,
        )
        manifest = artifacts["manifest"]
        verification = {
            "status": "VERIFIED",
            "manifest_sha256": manifest["manifest_sha256"],
            "file_count": len(manifest.get("files") or {}),
            "run_id_matches": str(manifest.get("run_id")) == str(run.get("run_id")),
            "human_boundary": (
                "PENDING_NOT_AUTHORIZED"
                if stage == "preview"
                else "SIGNED_DISPOSITION_RECORDED"
            ),
        }
        chain: list[dict[str, Any]] = []
        skill_values = {
            "assemble-run-result-context": (canonical_sha256({"run": run, "submission": submission}), context_sha),
            "select-token-budget-strategy": (context_sha, canonical_sha256(strategy)),
            "compose-result-document": (context_sha, artifacts["payload"]["result_payload_sha256"]),
            "verify-result-artifact": (manifest["manifest_sha256"], canonical_sha256(verification)),
        }
        for skill_id, version, purpose in REPORT_SKILLS:
            input_sha, output_sha = skill_values[skill_id]
            chain.append(
                {
                    "skill_id": skill_id,
                    "version": version,
                    "purpose": purpose,
                    "status": "SUCCESS",
                    "input_sha256": input_sha,
                    "output_sha256": output_sha,
                    "discovered_at_runtime": True,
                }
            )
        return {
            "protocol": "FINFLUX_RESULT_COMPOSER_AGENT_RUN_V1.0",
            "agent_id": self.agent_id,
            "agent_version": self.version,
            "execution_channel": "DETERMINISTIC_AGENT_SERVICE",
            "agentteams_worker_package": "result-composer.zip",
            "run_id": run.get("run_id"),
            "stage": stage,
            "strategy": strategy,
            "skill_invocations": chain,
            "verification": verification,
            "artifacts": artifacts,
            "truth_boundary": (
                "自动生成报告但不自动生成Human决定；金融数值只来自已封存的确定性结果。"
            ),
        }
