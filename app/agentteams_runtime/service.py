from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from emergency_stop import EmergencyStopLedger
from model_gateway_control import ModelGatewayControlError
from protocol_v02 import DATAPASS_PROTOCOL, validate_datapass

from .config import (
    AgentTeamsConfigurationError,
    AgentTeamsUnavailable,
    APP_ROOT,
    CORE_WORKERS,
    FORMAL_RUNS_ROOT,
    MANAGER,
    ROLE_LABELS,
    TEAM_NAME,
    execution_policy,
)
from .envelope import build_handle, build_transport, sha256
from .gateway import (
    activate as activate_gateway,
    close as close_gateway,
    extend_same_run_recovery_window as extend_gateway_recovery_window,
    rebind_same_run_actors as rebind_gateway_same_run_actors,
    rearm_expired_before_first_call as rearm_gateway_expired_before_first_call,
    rearm_transport_failure as rearm_gateway_transport_failure,
    usage as gateway_usage,
)
from .matrix import MatrixClient
from .runtime import docker, provider_guard, publish_context, status as runtime_status
from .store import RunStore


_FILES = {
    "evidence-investigator": "evidence_result.json",
    "semantic-impact-analyst": "semantic_impact_result.json",
    "independent-validator": "independent_validation.json",
}
_TERMINAL = {
    "COMPLETED",
    "STOPPED_BY_GATE",
    "BUDGET_EXCEEDED",
    "FAILED_CLOSED",
    "CANCELLED_BY_SESSION_RESET",
    "MODEL_CONTROL_CLEANUP_FAILED",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _contains_protocol_line(body: str, expected: str) -> bool:
    """Accept the protocol line even when a model adds a Markdown bullet."""

    for line in str(body or "").splitlines():
        normalized = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line.strip())
        if normalized == expected:
            return True
    return False


def _transaction(run_id: str, phase: str) -> str:
    return "finflux_" + sha256({"run_id": run_id, "phase": phase})[:48]


def _actor(sender: str) -> dict[str, str]:
    localpart = sender.split(":", 1)[0].lstrip("@")
    label, role = ROLE_LABELS.get(localpart, (localpart or "Unknown", "unknown"))
    return {"sender": sender, "name": label, "role": role}


class AgentTeamsService:
    """Single orchestration entry point for the FinFlux AgentTeams Team.

    The service owns transport and state projection only. Worker Skills own
    deterministic calculation; Human owns the final decision.
    """

    def __init__(self, store: RunStore | None = None) -> None:
        self.store = store or RunStore()
        self.stop_ledger = EmergencyStopLedger(
            APP_ROOT / "runtime" / "control_plane" / "emergency_stops"
        )

    def runtime_status(self) -> dict[str, Any]:
        return runtime_status()

    def provider_guard(self, force: bool = False) -> dict[str, Any]:
        return provider_guard(self.store, force)

    def active_run(self) -> dict[str, Any] | None:
        rows = sorted(self.store.active(), key=lambda row: str(row.get("submitted_at_utc")))
        return rows[-1] if rows else None

    def peek(self, run_id: str) -> dict[str, Any]:
        """Return the persisted AgentTeams projection without advancing it."""

        return self.store.load(run_id)

    def submit(self, submission: dict[str, Any], live_run: dict[str, Any]) -> dict[str, Any]:
        run_id = str(live_run.get("run_id") or "")
        if self.store.exists(run_id):
            existing = self.store.load(run_id)
            if existing.get("submission_id") != submission.get("submission_id"):
                raise AgentTeamsConfigurationError("Run ID已绑定其他Submission")
            return self._view(existing, [], runtime_status())
        runtime = runtime_status()
        if not runtime.get("connected"):
            raise AgentTeamsUnavailable(str(runtime.get("truthful_note") or "AgentTeams未就绪"))
        guard = self.provider_guard(force=True)
        if not guard["allowed"]:
            raise AgentTeamsConfigurationError("派发门禁拒绝: " + ";".join(guard["reasons"]))
        policy = execution_policy()
        envelope = build_transport(submission, live_run, policy)
        handle = build_handle(envelope)
        workers = tuple(envelope["required_workers"])
        if workers != CORE_WORKERS:
            raise AgentTeamsConfigurationError("公开版适配器仅允许固定3个核心Worker")
        publish_receipt = publish_context(envelope["context_capsule_handle"], workers)
        matrix = MatrixClient.admin()
        manager_room = matrix.create_room(
            run_id=run_id, actor="manager", purpose="MANAGER_ROUTE"
        )
        leader_room = matrix.create_room(
            run_id=run_id,
            actor="finchange-case-lead",
            purpose="CASE_LEAD",
            invite=("finchange-data-owner", *CORE_WORKERS),
        )
        leader_id = leader_room["actor_id"]
        dispatch_hash = sha256(
            {
                "run_id": run_id,
                "handle_sha256": handle["handle_sha256"],
                "leader_room_id": leader_room["room_id"],
            }
        )
        expected = (
            f"FINFLUX_MANAGER_DISPATCHED {envelope['case_id']} {run_id} "
            f"{envelope['dispatch_idempotency_key']} {dispatch_hash} {leader_id}"
        )
        prompt = (
            f"FINFLUX_MANAGER_ROUTE_AUTH {envelope['case_id']} {run_id}\n"
            f"route={handle['route']} workers={','.join(workers)}\n"
            f"handle_sha256={handle['handle_sha256']} dispatch_hash={dispatch_hash}\n"
            "只校验结构化路由，不读取金融数值、不调用工具、不执行Worker任务。\n"
            f"REPLY_EXACTLY: {expected}"
        )
        run = {
            "protocol": "FINFLUX_AGENTTEAMS_RUN_V0.3",
            "run_id": run_id,
            "case_id": live_run["case_id"],
            "submission_id": submission["submission_id"],
            "asset": envelope["asset_class"],
            "scenario": "live_submission",
            "state": "SUBMITTED",
            "submitted_at_utc": utc_now(),
            "submitted_at_ms": int(time.time() * 1000),
            "case_envelope": envelope,
            "formal_case_envelope_handle": envelope["formal_case_envelope_handle"],
            "case_envelope_sha256": sha256(envelope),
            "matrix_handle": handle,
            "manager_room_id": manager_room["room_id"],
            "leader_room_id": leader_room["room_id"],
            "manager_id": manager_room["actor_id"],
            "leader_id": leader_id,
            "dispatch_hash": dispatch_hash,
            "expected_manager_authorization": expected,
            "context_publish_receipt": publish_receipt,
            "manager_skill_receipt": envelope["manager_skill_receipt"],
            "execution_policy_id": policy["policy_id"],
            "manager_dispatch_receipt": {"status": "PREPARED"},
            "leader_relay": {"status": "NOT_SENT"},
            "provider_usage": {"status": "NOT_CAPTURED", "source": "MODEL_GATEWAY_PENDING"},
            "human_decision": None,
            "self_healing_attempts": [],
        }
        gateway = activate_gateway(run)
        run["provider_usage_baseline"] = gateway["baseline"]
        run["model_gateway_binding"] = gateway["binding"]
        run["model_gateway_actor_binding_receipt"] = gateway["actor_binding_receipt"]
        self.store.save(run)
        event_id = matrix.send(
            manager_room["room_id"],
            prompt,
            mention=manager_room["actor_id"],
            transaction_id=_transaction(run_id, "manager-route"),
        )
        run["manager_dispatch_receipt"] = {
            "status": "MANAGER_WAKEUP_SENT",
            "event_id": event_id,
            "sent_at_utc": utc_now(),
        }
        self.store.save(run)
        return self._view(run, [], runtime)

    def get(self, run_id: str) -> dict[str, Any]:
        run = self.store.load(run_id)
        runtime = runtime_status()
        if run.get("state") in _TERMINAL:
            # Terminal business state must not hide the immutable transport
            # evidence.  In particular, the Human decision is a real Matrix
            # event created after the model boundary has closed.  Re-reading
            # the two Run-scoped rooms is read-only and lets the public Run
            # projection archive that event without replaying any Agent.
            trace: list[dict[str, Any]] = []
            if runtime.get("connected"):
                trace = self._trace(MatrixClient.admin(), run)
            close_receipt = run.get("model_gateway_close_receipt")
            if isinstance(close_receipt, dict) and close_receipt.get("state") == "CLOSED":
                run["model_gateway_binding"] = dict(close_receipt)
            human = run.get("human_decision")
            if isinstance(human, dict):
                result = dict(run.get("agent_result") or {})
                if result.get("human_decision") != human:
                    result["human_decision"] = human
                    result["run_state"] = "COMPLETED"
                    result["final_decision"] = str(
                        result.get("leader_recommendation") or "PENDING"
                    )
                    run["agent_result"] = result
                    self.store.save(run)
            return self._view(run, trace, runtime)
        if not runtime.get("connected"):
            return self._view({**run, "state": "RUNTIME_UNAVAILABLE"}, [], runtime)
        matrix = MatrixClient.admin()
        trace = self._trace(matrix, run)
        self._relay_authorized_dispatch(matrix, run, trace)
        self._relay_recovery_workers(matrix, run, trace)
        artifacts = self._artifacts(run)
        self._finalize(matrix, run, artifacts)
        recommendation = self._leader_recommendation(trace)
        if (
            recommendation == "PENDING"
            and len(artifacts) == len(CORE_WORKERS)
            and (run.get("leader_finalization") or {}).get("recommendation")
        ):
            # The three Worker artifacts are immutable and already contain the
            # financial recommendation.  Asking the LLM to echo a fixed
            # DATAPASS_DRAFT line adds no reasoning but can fail after all
            # useful work is done (for example at a gateway exposure fuse).
            # Deterministically aggregate the sealed artifacts and retain an
            # explicit receipt instead of inventing a chat response.
            recommendation = str(run["leader_finalization"]["recommendation"])
            receipt = {
                "protocol": "FINFLUX_SEALED_ARTIFACT_AGGREGATION_V1",
                "run_id": run_id,
                "case_id": run["case_id"],
                "recommendation": recommendation,
                "worker_artifact_sha256": {
                    role: str(artifacts[role].get("artifact_sha256") or "")
                    for role in CORE_WORKERS
                },
                "model_generated_financial_truth": False,
                "reason": "3/3 sealed Worker artifacts; fixed-format aggregation does not require another model call",
                "created_at_utc": utc_now(),
            }
            receipt["receipt_sha256"] = sha256(receipt)
            run["sealed_artifact_aggregation"] = receipt
        if run.get("human_decision"):
            state = "COMPLETED"
        elif len(artifacts) == len(CORE_WORKERS) and recommendation != "PENDING":
            state = "AWAITING_HUMAN"
        elif trace or artifacts:
            state = "RUNNING"
        else:
            state = "SUBMITTED"
        run["state"] = state
        run["agent_result"] = self._result(
            run, trace, artifacts, state, recommendation=recommendation
        )
        run["budget"] = self._budget(run, trace)
        run["provider_usage"] = gateway_usage(run_id)
        if state == "AWAITING_HUMAN" and not run.get("model_gateway_close_receipt"):
            run["model_gateway_close_receipt"] = close_gateway(run_id, state)
            # The close receipt is the authoritative immutable binding after
            # model execution.  Keeping the obsolete ACTIVE copy in the Run
            # made the UI and export validator disagree about the same Run.
            run["model_gateway_binding"] = dict(run["model_gateway_close_receipt"])
        self.store.save(run)
        return self._view(run, trace, runtime)

    def _trace(self, matrix: MatrixClient, run: dict[str, Any]) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for room_id in (run.get("manager_room_id"), run.get("leader_room_id")):
            if not str(room_id).startswith("!"):
                continue
            for event in matrix.messages(str(room_id)):
                body = (event.get("content") or {}).get("body")
                if not isinstance(body, str) or run["run_id"] not in body:
                    continue
                event_id = str(event.get("event_id") or "")
                timestamp = int(event.get("origin_server_ts") or 0)
                found[event_id] = {
                    "event_id": event_id,
                    "room_id": room_id,
                    "timestamp_ms": timestamp,
                    "timestamp_utc": datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat() if timestamp else None,
                    "actor": _actor(str(event.get("sender") or "")),
                    "body": body,
                    "source": "MATRIX_RUN_ROOM",
                }
        return sorted(found.values(), key=lambda item: item["timestamp_ms"])

    def _relay_authorized_dispatch(
        self, matrix: MatrixClient, run: dict[str, Any], trace: list[dict[str, Any]]
    ) -> None:
        if (run.get("leader_relay") or {}).get("status") == "SENT":
            return
        exact = str(run["expected_manager_authorization"])
        authorization = next(
            (
                item for item in trace
                if item["actor"]["role"] == "manager"
                and _contains_protocol_line(item["body"], exact)
            ),
            None,
        )
        if not authorization:
            return
        membership = matrix.ensure_joined(run["leader_room_id"], CORE_WORKERS)
        if not membership["ready"]:
            run["leader_room_membership"] = membership
            self.store.save(run)
            return
        body = self._leader_relay_body(run)
        event_id = matrix.send(
            run["leader_room_id"], body, mention=run["leader_id"],
            transaction_id=_transaction(run["run_id"], "leader-relay"),
        )
        run["leader_relay"] = {
            "status": "SENT",
            "event_id": event_id,
            "authorization_event_id": authorization["event_id"],
            "sent_at_utc": utc_now(),
        }
        run["manager_dispatch_receipt"] = {
            **(run.get("manager_dispatch_receipt") or {}),
            "status": "MANAGER_AUTHORIZED_DISPATCHED",
            "authorization_event_id": authorization["event_id"],
        }
        self.store.save(run)

    @staticmethod
    def _worker_assignment(run: dict[str, Any], role: str) -> str:
        handle = run["matrix_handle"]
        slices = handle["role_slice_sha256"]
        brief = ((run.get("case_envelope") or {}).get("semantic_discovery_brief") or {})
        auto = str(
            (((run.get("case_envelope") or {}).get("precheck_attestation") or {}).get("semantic_resolution_mode") or "")
        ) == "AGENT_PROPOSES"
        base = (
            "python ../../agent-packages/current/tool_gateway.py "
            f"--entry signed-worker --timeout-s 90 -- --role {role} "
            f"--context-capsule-ref {slices[role]}"
        )
        if auto and role in {"semantic-impact-analyst", "independent-validator"}:
            return (
                f"ASSIGN {role}: 独立阅读下方SEMANTIC_DISCOVERY_BRIEF，"
                "不得照抄另一Worker，也不得把模型生成数值作为金融真值。选择一个候选语义后执行："
                f"{base} --proposed-field <候选字段或语义值> "
                "--proposed-semantic <snake_case语义> --confidence-bps <0至10000> "
                "--reason-code <UPPER_SNAKE_CASE> --uncertainty-code <UPPER_SNAKE_CASE>"
            )
        return f"ASSIGN {role}: {base}"

    @classmethod
    def _leader_relay_body(cls, run: dict[str, Any], repair: bool = False) -> str:
        handle = run["matrix_handle"]
        brief = ((run.get("case_envelope") or {}).get("semantic_discovery_brief") or {})
        assignments_list = [cls._worker_assignment(run, role) for role in CORE_WORKERS]
        assignments = "\n".join(assignments_list)
        verb = "FINFLUX_SAME_RUN_RELAY" if repair else "FINFLUX_LIVE_RELAY"
        return (
            f"{verb} {run['case_id']} {run['run_id']}\n"
            f"LEADER_ROOM_ID: {run['leader_room_id']}\n"
            "TASKFLOW_REQUIRED: delegate_task必须显式传入上方LEADER_ROOM_ID作为roomId；"
            "每个任务使用CASE_ENVELOPE_HANDLE中的唯一task_id，不得自建Room或Run。\n"
            f"TASK_SCOPE: {handle['task_identity']['task_scope']}\n"
            f"{assignments}\n"
            f"SEMANTIC_DISCOVERY_BRIEF:{json.dumps(brief, ensure_ascii=False, separators=(',', ':'))}\n"
            "每个Worker只执行自己的签名Context Slice；不得互相复制上下文。完成后由Case Lead汇总。\n"
            f"CASE_ENVELOPE_HANDLE:{json.dumps(handle, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _artifacts(self, run: dict[str, Any]) -> dict[str, Any]:
        task_ids = run["matrix_handle"]["task_identity"]["task_ids"]
        root = "/root/agentteams-fs/teams/finchange-cross-asset-review/shared/tasks"
        artifacts: dict[str, Any] = {}
        for role in CORE_WORKERS:
            task_id = str(task_ids.get(role) or "")
            if not re.fullmatch(r"task-[0-9A-Za-z._-]{8,220}", task_id):
                continue
            path = f"{root}/{task_id}/{_FILES[role]}"
            try:
                raw = docker(
                    "exec", f"agentteams-worker-{role}", "cat", path, timeout=20
                )
                value = json.loads(raw)
            except (AgentTeamsUnavailable, json.JSONDecodeError):
                continue
            if (
                isinstance(value, dict)
                and value.get("role") == role
                and value.get("task_id") == task_id
                and value.get("run_id") == run["run_id"]
            ):
                artifacts[role] = value
        return artifacts

    def _finalize(
        self, matrix: MatrixClient, run: dict[str, Any], artifacts: dict[str, Any]
    ) -> None:
        if len(artifacts) != len(CORE_WORKERS) or run.get("leader_finalization"):
            return
        semantic = artifacts["semantic-impact-analyst"]
        validator = artifacts["independent-validator"]
        recommendation = str(semantic.get("recommendation") or "NEEDS_EVIDENCE")
        semantic_proposal = semantic.get("agent_semantic_proposal") or {}
        validator_proposal = validator.get("agent_semantic_proposal") or {}
        if (
            semantic_proposal
            and validator_proposal
            and str(semantic_proposal.get("proposed_field"))
            != str(validator_proposal.get("proposed_field"))
        ):
            recommendation = "NEEDS_EVIDENCE"
        if str(validator.get("independent_recommendation") or recommendation) != recommendation:
            recommendation = "NEEDS_EVIDENCE"
        body = (
            f"FINFLUX_LEADER_FINALIZE {run['case_id']} {run['run_id']}\n"
            f"3/3 sealed artifacts available; recommendation={recommendation}.\n"
            "Do not rerun Workers. Reply exactly: "
            f"DATAPASS_DRAFT CASE_ID={run['case_id']} RUN_ID={run['run_id']} RECOMMENDATION={recommendation}"
        )
        event_id = matrix.send(
            run["leader_room_id"], body, mention=run["leader_id"],
            transaction_id=_transaction(run["run_id"], "leader-finalize"),
        )
        run["leader_finalization"] = {"status": "REQUESTED", "event_id": event_id, "recommendation": recommendation}
        self.store.save(run)

    @staticmethod
    def _leader_recommendation(trace: list[dict[str, Any]]) -> str:
        for item in reversed(trace):
            body = item["body"].upper()
            if item["actor"]["role"] == "team_leader" and "DATAPASS_DRAFT" in body:
                for value in ("NEEDS_EVIDENCE", "BLOCK", "PASS"):
                    if re.search(rf"\b{value}\b", body):
                        return value
        return "PENDING"

    def _result(
        self,
        run: dict[str, Any],
        trace: list[dict[str, Any]],
        artifacts: dict[str, Any],
        state: str,
        recommendation: str | None = None,
    ) -> dict[str, Any]:
        recommendation = recommendation or self._leader_recommendation(trace)
        return {
            "run_id": run["run_id"],
            "case_id": run["case_id"],
            "asset_class": run["asset"],
            "execution_policy_id": run["execution_policy_id"],
            "run_state": state,
            "worker_results": {
                role: {"status": str((artifacts.get(role) or {}).get("status") or "PENDING")}
                for role in CORE_WORKERS
            },
            "workers_completed": len(artifacts),
            "workers_required": len(CORE_WORKERS),
            "leader_recommendation": recommendation,
            "worker_artifacts": artifacts,
            "human_decision": run.get("human_decision"),
            "final_decision": recommendation,
            "result_source": "MATRIX_AND_SEALED_TASK_ARTIFACTS",
        }

    @staticmethod
    def _budget(run: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
        elapsed = max(0, int(time.time() - int(run.get("submitted_at_ms", 0)) / 1000))
        characters = sum(len(item["body"]) for item in trace)
        return {
            "status": "OBSERVED",
            "elapsed_seconds": elapsed,
            "matrix_events": len(trace),
            "observed_characters": characters,
            "observed_message_token_estimate": (characters + 3) // 4,
            "truth_boundary": "Matrix字符估算不是模型Token；模型Token只接受网关usage。",
        }

    @staticmethod
    def _view(run: dict[str, Any], trace: list[dict[str, Any]], runtime: dict[str, Any]) -> dict[str, Any]:
        return {**run, "trace": trace, "trace_source": "RUN_SCOPED_MATRIX_ROOMS", "runtime": runtime}

    def repair(self, run_id: str, requested_by: str, reason: str) -> dict[str, Any]:
        run = self.store.load(run_id)
        matrix = MatrixClient.admin()
        trace = self._trace(matrix, run)
        manager_authorized = any(
            item["actor"]["role"] == "manager"
            and _contains_protocol_line(
                item["body"], str(run.get("expected_manager_authorization") or "")
            )
            for item in trace
        )
        if not manager_authorized:
            return self.recover_manager_session(run_id, requested_by, reason)
        artifacts = self._artifacts(run)
        missing = [role for role in CORE_WORKERS if role not in artifacts]
        attempts = list(run.get("self_healing_attempts") or [])
        attempt_no = len(attempts) + 1
        # Recovery is bounded by the model gateway's real call/token fuse.  A
        # small, arbitrary retry counter previously made a recoverable Run
        # permanently stuck even though two role artifacts were still missing
        # and budget remained.  Keep an operational ceiling only as a last
        # defence; normal protection comes from the gateway ledger.
        if run.get("state") in _TERMINAL or not missing or attempt_no > 8:
            status = "WAIT_HUMAN_REARM" if run.get("state") in _TERMINAL else "NO_ACTION_REQUIRED"
            receipt = {"protocol": "FINFLUX_SAME_RUN_REPAIR_V2", "run_id": run_id, "status": status, "missing_workers": missing, "new_run_created": False, "requested_at_utc": utc_now()}
        else:
            handle = run["matrix_handle"]
            membership = matrix.ensure_joined(run["leader_room_id"], tuple(missing))
            if membership["ready"]:
                prior_approved = next(
                    (
                        item
                        for item in reversed(attempts)
                        if item.get("case_lead_authorization_observed") is True
                    ),
                    None,
                )
                gateway_snapshot = gateway_usage(run_id)
                gateway = gateway_snapshot.get("model_gateway_ledger") or {}
                gateway_status = str(
                    gateway.get("status") or gateway_snapshot.get("status") or ""
                )
                gateway_rearm = None
                if (
                    gateway_status == "FUSE_TRIPPED"
                    and gateway.get("fuse_reason") == "UPSTREAM_TRANSPORT_FAILURE"
                ):
                    gateway_rearm = rearm_gateway_transport_failure(
                        run_id, requested_by=requested_by, reason=reason
                    )
                elif gateway_status == "ACTIVE":
                    lease = extend_gateway_recovery_window(
                        run_id, reason=reason
                    )
                    actor_rebind = rebind_gateway_same_run_actors(
                        run_id,
                        requested_by=requested_by,
                        reason="恢复同一Run的短期Actor请求绑定：" + reason,
                    )
                    gateway_rearm = {
                        "lease": lease,
                        "actor_rebind": actor_rebind,
                    }
                elif gateway_status == "NOT_CAPTURED":
                    # A delayed Matrix delivery may reach the first actor only
                    # after the original lease expired.  Rearm is fail-closed:
                    # the gateway helper permits it only for the same Run with
                    # zero provider calls and zero observed Tokens.
                    try:
                        lease = rearm_gateway_expired_before_first_call(
                            run_id,
                            requested_by=requested_by,
                            reason=reason,
                        )
                    except ModelGatewayControlError as exc:
                        if "BINDING_NOT_EXPIRED" not in str(exc):
                            raise
                        lease = {
                            "status": "LEASE_STILL_ACTIVE",
                            "usage_counters_reset": False,
                            "new_run_created": False,
                        }
                    actor_rebind = rebind_gateway_same_run_actors(
                        run_id,
                        requested_by=requested_by,
                        reason="恢复同一Run的短期Actor请求绑定：" + reason,
                    )
                    gateway_rearm = {
                        "lease": lease,
                        "actor_rebind": actor_rebind,
                    }
                if prior_approved:
                    receipt = {
                        "protocol": "FINFLUX_SAME_RUN_REPAIR_V3",
                        "run_id": run_id,
                        "status": "CASE_LEAD_RECOVERY_REVIEW",
                        "missing_workers": missing,
                        "room_membership": membership,
                        "new_run_created": False,
                        "requested_by": requested_by,
                        "reason": reason,
                        "expected_case_lead_authorization": prior_approved.get(
                            "expected_case_lead_authorization"
                        ),
                        "reuses_prior_case_lead_authorization": True,
                        "gateway_rearm": gateway_rearm,
                        "budget_extension": None,
                        "requested_at_utc": utc_now(),
                    }
                    receipt["receipt_sha256"] = sha256(receipt)
                    attempts.append(receipt)
                    run["self_healing_attempts"] = attempts
                    run["state"] = "REPAIRING"
                    self.store.save(run)
                    trace = self._trace(matrix, run)
                    self._relay_recovery_workers(matrix, run, trace)
                    return (self.store.load(run_id).get("self_healing_attempts") or [])[-1]
                expected = (
                    f"FINFLUX_RECOVERY_APPROVED {run['case_id']} {run_id} "
                    f"attempt={attempt_no} missing={','.join(missing)}"
                )
                body = (
                    f"FINFLUX_RECOVERY_DIAGNOSE {run['case_id']} {run_id}\n"
                    f"attempt={attempt_no} failure=UPSTREAM_TRANSPORT_FAILURE "
                    f"missing_workers={','.join(missing)}\n"
                    "原Manager授权、Case Lead派发、证据哈希与已完成产物全部保留；"
                    "只判断是否允许在同一Run唤醒缺失Worker，不重做金融结论。\n"
                    f"REPLY_EXACTLY: {expected}"
                )
                event_id = matrix.send(
                    run["leader_room_id"], body, mention=run["leader_id"],
                    transaction_id=_transaction(run_id, f"repair-{attempt_no}"),
                )
                receipt = {"protocol": "FINFLUX_SAME_RUN_REPAIR_V3", "run_id": run_id, "status": "CASE_LEAD_RECOVERY_REVIEW", "event_id": event_id, "missing_workers": missing, "room_membership": membership, "new_run_created": False, "requested_by": requested_by, "reason": reason, "expected_case_lead_authorization": expected, "gateway_rearm": gateway_rearm, "requested_at_utc": utc_now()}
                run["state"] = "REPAIRING"
            else:
                receipt = {"protocol": "FINFLUX_SAME_RUN_REPAIR_V2", "run_id": run_id, "status": "WORKER_ROOM_JOIN_PENDING", "missing_workers": missing, "room_membership": membership, "new_run_created": False, "requested_by": requested_by, "reason": reason, "requested_at_utc": utc_now()}
        receipt["receipt_sha256"] = sha256(receipt)
        attempts.append(receipt)
        run["self_healing_attempts"] = attempts
        self.store.save(run)
        return receipt

    def supervisor_wake_manager(
        self, run_id: str, requested_by: str, reason: str
    ) -> dict[str, Any]:
        """Wake Manager once in the same room without creating a new Run."""

        run = self.store.load(run_id)
        if run.get("state") in _TERMINAL or run.get("state") == "AWAITING_HUMAN":
            return {
                "protocol": "FINFLUX_SUPERVISOR_RECOVERY_V1",
                "run_id": run_id,
                "status": "NO_ACTION_REQUIRED",
                "new_run_created": False,
            }
        matrix = MatrixClient.admin()
        trace = self._trace(matrix, run)
        authorized = any(
            item["actor"]["role"] == "manager"
            and _contains_protocol_line(
                item["body"], str(run.get("expected_manager_authorization") or "")
            )
            for item in trace
        )
        wakeups = list(run.get("manager_supervisor_wakeups") or [])
        if authorized or wakeups:
            return {
                "protocol": "FINFLUX_SUPERVISOR_RECOVERY_V1",
                "run_id": run_id,
                "status": "MANAGER_ALREADY_AUTHORIZED" if authorized else "MANAGER_WAKEUP_LIMIT_REACHED",
                "new_run_created": False,
            }
        gateway_snapshot = gateway_usage(run_id)
        gateway = gateway_snapshot.get("model_gateway_ledger") or {}
        gateway_status = str(gateway.get("status") or gateway_snapshot.get("status") or "")
        gateway_recovery = None
        if gateway_status == "ACTIVE":
            gateway_recovery = {
                "lease": extend_gateway_recovery_window(run_id, reason=reason),
                "actor_rebind": rebind_gateway_same_run_actors(
                    run_id, requested_by=requested_by, reason=reason
                ),
            }
        elif gateway_status in {"NOT_CAPTURED", "NO_MODEL_CALL", ""}:
            try:
                lease = rearm_gateway_expired_before_first_call(
                    run_id, requested_by=requested_by, reason=reason
                )
            except ModelGatewayControlError as exc:
                if "BINDING_NOT_EXPIRED" not in str(exc):
                    raise
                lease = {"status": "LEASE_STILL_ACTIVE", "new_run_created": False}
            gateway_recovery = {
                "lease": lease,
                "actor_rebind": rebind_gateway_same_run_actors(
                    run_id, requested_by=requested_by, reason=reason
                ),
            }
        body = (
            f"FINFLUX_MANAGER_ROUTE_AUTH {run['case_id']} {run_id}\n"
            f"route={run['matrix_handle']['route']} workers={','.join(CORE_WORKERS)}\n"
            f"handle_sha256={run['matrix_handle']['handle_sha256']} dispatch_hash={run['dispatch_hash']}\n"
            "后台Supervisor检测到首次Manager超时；保持同一Run、Room、证据和任务身份。"
            "只校验结构化路由，不读取金融数值、不调用工具。\n"
            f"REPLY_EXACTLY: {run['expected_manager_authorization']}"
        )
        event_id = matrix.send(
            run["manager_room_id"],
            body,
            mention=run["manager_id"],
            transaction_id=_transaction(run_id, "supervisor-manager-wakeup-1"),
        )
        receipt = {
            "protocol": "FINFLUX_SUPERVISOR_RECOVERY_V1",
            "run_id": run_id,
            "status": "MANAGER_REAWAKENED",
            "event_id": event_id,
            "requested_by": requested_by,
            "reason": reason,
            "new_run_created": False,
            "gateway_recovery": gateway_recovery,
            "requested_at_utc": utc_now(),
        }
        receipt["receipt_sha256"] = sha256(receipt)
        wakeups.append(receipt)
        run["manager_supervisor_wakeups"] = wakeups
        recoveries = list(run.get("supervisor_recovery_attempts") or [])
        recoveries.append(receipt)
        run["supervisor_recovery_attempts"] = recoveries
        self.store.save(run)
        return receipt

    def supervisor_dispatch_missing_workers(
        self, run_id: str, requested_by: str, reason: str
    ) -> dict[str, Any]:
        """Execute the Manager-authorized dispatch transaction for missing Workers.

        This is a control-plane fallback after Case Lead timeout.  It does not
        derive a financial conclusion and does not replace completed artifacts.
        """

        run = self.store.load(run_id)
        matrix = MatrixClient.admin()
        trace = self._trace(matrix, run)
        authorization = next(
            (
                item
                for item in trace
                if item["actor"]["role"] == "manager"
                and _contains_protocol_line(
                    item["body"], str(run.get("expected_manager_authorization") or "")
                )
            ),
            None,
        )
        if not authorization:
            raise AgentTeamsConfigurationError(
                "Manager尚未授权，Supervisor不得替代Manager派发"
            )
        artifacts = self._artifacts(run)
        missing = [role for role in CORE_WORKERS if role not in artifacts]
        if not missing or run.get("state") in _TERMINAL or run.get("state") == "AWAITING_HUMAN":
            return {
                "protocol": "FINFLUX_SUPERVISOR_RECOVERY_V1",
                "run_id": run_id,
                "status": "NO_ACTION_REQUIRED",
                "missing_workers": missing,
                "new_run_created": False,
            }
        membership = matrix.ensure_joined(run["leader_room_id"], tuple(missing))
        if not membership["ready"]:
            raise AgentTeamsUnavailable(
                "缺失Worker尚未加入原Case Lead Room: "
                + ",".join(membership.get("missing") or missing)
            )
        recoveries = list(run.get("supervisor_recovery_attempts") or [])
        attempt_no = len(recoveries) + 1
        event_ids: dict[str, str] = {}
        task_ids = run["matrix_handle"]["task_identity"]["task_ids"]
        brief = ((run.get("case_envelope") or {}).get("semantic_discovery_brief") or {})
        for role in missing:
            actor_id = matrix.mxid(role)
            assignment = self._worker_assignment(run, role)
            body = (
                f"FINFLUX_AUTHORIZED_WORKER_DISPATCH {run['case_id']} {run_id}\n"
                f"MANAGER_AUTHORIZATION_EVENT: {authorization['event_id']}\n"
                f"LEADER_ROOM_ID: {run['leader_room_id']}\n"
                f"TASK_ID: {task_ids[role]}\n"
                "Case Lead未在监督窗口内完成派发；控制器只执行Manager已经授权的派发事务。"
                "仅完成你的签名Context Slice，调用一次指定signed-worker命令；不得创建新Run、"
                "不得补写其他Worker产物、不得编造金融数值。\n"
                f"{assignment}\n"
                f"SEMANTIC_DISCOVERY_BRIEF:{json.dumps(brief, ensure_ascii=False, separators=(',', ':'))}"
            )
            event_ids[role] = matrix.send(
                run["leader_room_id"],
                body,
                mention=actor_id,
                transaction_id=_transaction(
                    run_id, f"supervisor-worker-{attempt_no}-{role}"
                ),
            )
        receipt = {
            "protocol": "FINFLUX_SUPERVISOR_RECOVERY_V1",
            "run_id": run_id,
            "status": "MISSING_WORKERS_REAWAKENED",
            "missing_workers": missing,
            "preserved_worker_artifacts": sorted(artifacts),
            "worker_event_ids": event_ids,
            "manager_authorization_event_id": authorization["event_id"],
            "requested_by": requested_by,
            "reason": reason,
            "new_run_created": False,
            "requested_at_utc": utc_now(),
        }
        receipt["receipt_sha256"] = sha256(receipt)
        recoveries.append(receipt)
        run["supervisor_recovery_attempts"] = recoveries
        run["state"] = "RUNNING"
        self.store.save(run)
        return receipt

    def supervisor_wait(
        self,
        run_id: str,
        requested_by: str,
        reason: str,
        reason_codes: list[str],
    ) -> dict[str, Any]:
        """Fail closed as a user-facing WAIT without fabricating DataPass."""

        action_ref = {
            "evidence_type": "RUN_SUPERVISOR_DECISION",
            "action": "FAILED_CLOSED_TO_WAIT",
            "status": "NO_CONTAINER_MUTATION_REQUIRED",
            "run_id": run_id,
            "reason_codes": list(reason_codes),
        }
        action_ref["evidence_sha256"] = sha256(action_ref)
        run = self.emergency_stop(
            run_id,
            terminal_state="STOPPED_BY_GATE",
            actor=requested_by,
            reason=reason,
            token_guard_snapshot=gateway_usage(run_id),
            container_action_evidence_refs=[action_ref],
            reason_codes=["WAIT", *reason_codes],
        )
        close_error = None
        try:
            run["model_gateway_close_receipt"] = close_gateway(
                run_id, "SUPERVISOR_WAIT"
            )
            run["model_gateway_binding"] = dict(run["model_gateway_close_receipt"])
        except Exception as exc:  # noqa: BLE001 - stop fact remains authoritative
            close_error = f"{type(exc).__name__}: {exc}"
        run["supervisor_outcome"] = {
            "protocol": "FINFLUX_SUPERVISOR_WAIT_V1",
            "decision": "WAIT",
            "reason": reason,
            "reason_codes": reason_codes,
            "human_action": "补充运行证据或由运维人员处理失败环节后，显式创建修订子Run",
            "datapass_created": False,
            "new_run_created": False,
            "gateway_close_error": close_error,
            "created_at_utc": utc_now(),
        }
        run["supervisor_outcome"]["receipt_sha256"] = sha256(
            run["supervisor_outcome"]
        )
        self.store.save(run)
        return run

    def _relay_recovery_workers(
        self, matrix: MatrixClient, run: dict[str, Any], trace: list[dict[str, Any]]
    ) -> None:
        """Wake only missing Workers after the Case Lead approves same-Run recovery."""

        attempts = list(run.get("self_healing_attempts") or [])
        if not attempts:
            return
        attempt = attempts[-1]
        if attempt.get("status") != "CASE_LEAD_RECOVERY_REVIEW":
            return
        expected = str(attempt.get("expected_case_lead_authorization") or "")
        approved = any(
            item["actor"]["role"] == "team_leader"
            and _contains_protocol_line(item["body"], expected)
            for item in trace
        )
        if not approved:
            return
        authored_by_role: dict[str, str] = {}
        for role in attempt.get("missing_workers") or []:
            task_id = str(
                ((run.get("matrix_handle") or {}).get("task_identity") or {})
                .get("task_ids", {})
                .get(role, "")
            )
            authored_by_role[str(role)] = next(
                (
                    item["body"]
                    for item in reversed(trace)
                    if item["actor"]["role"] == "team_leader"
                    and task_id
                    and task_id in item["body"]
                    and "You are assigned task" in item["body"]
                ),
                "",
            )
        if not any(authored_by_role.values()):
            event_id = matrix.send(
                run["leader_room_id"],
                self._leader_relay_body(run, repair=True),
                mention=run["leader_id"],
                transaction_id=_transaction(
                    run["run_id"], f"repair-leader-relay-{len(attempts)}"
                ),
            )
            attempt["status"] = "CASE_LEAD_REDISPATCH_SENT"
            attempt["leader_relay_event_id"] = event_id
            attempt["case_lead_authorization_observed"] = True
            attempt["reawakened_at_utc"] = utc_now()
            attempt["receipt_sha256"] = sha256(
                {key: value for key, value in attempt.items() if key != "receipt_sha256"}
            )
            attempts[-1] = attempt
            run["self_healing_attempts"] = attempts
            run["state"] = "RUNNING"
            self.store.save(run)
            return
        event_ids: dict[str, str] = {}
        for role in attempt.get("missing_workers") or []:
            authored = authored_by_role.get(str(role), "")
            if not authored:
                continue
            actor_id = matrix.mxid(str(role))
            event_ids[str(role)] = matrix.send(
                run["leader_room_id"],
                (
                    "FINFLUX_SAME_RUN_WORKER_REWAKE\n"
                    "本次只完成你被分配的一个缺失交付物。不要解释、不要运行 --help、"
                    "不要读取目录或历史。若命令含候选占位符，先依据业务用途和可用字段"
                    "独立选择候选，再把全部占位符替换为具体值；随后只调用一次 Bash "
                    "执行 signed-worker 命令。只有网关返回 result_path 和 receipt_sha256 "
                    "才算完成；工具失败则原样报告错误，不得编造产物。\n"
                    + authored
                ),
                mention=actor_id,
                transaction_id=_transaction(
                    run["run_id"],
                    f"repair-worker-{len(attempts)}-{role}",
                ),
            )
        if event_ids:
            attempt["status"] = "MISSING_WORKERS_REAWAKENED"
            attempt["worker_event_ids"] = event_ids
            attempt["case_lead_authorization_observed"] = True
            attempt["reawakened_at_utc"] = utc_now()
            attempt["receipt_sha256"] = sha256(
                {key: value for key, value in attempt.items() if key != "receipt_sha256"}
            )
            attempts[-1] = attempt
            run["self_healing_attempts"] = attempts
            run["state"] = "RUNNING"
            self.store.save(run)

    def rearm_manager(self, run_id: str) -> dict[str, Any]:
        """Restore a missing model-gateway binding and replay Manager in the same Run."""
        run = self.store.load(run_id)
        if run.get("state") in _TERMINAL:
            raise AgentTeamsConfigurationError("终态Run不能重新唤醒Manager")
        usage_snapshot = gateway_usage(run_id)
        if usage_snapshot.get("status") not in {"NOT_CAPTURED", "NO_MODEL_CALL"}:
            raise AgentTeamsConfigurationError(
                "Manager重发只允许零有效模型调用的同一Run；已有usage必须走缺失Worker恢复"
            )
        try:
            lease = rearm_gateway_expired_before_first_call(
                run_id,
                requested_by="finflux.same-run-manager-rearm",
                reason="首次Manager请求未形成有效Provider调用",
            )
        except ModelGatewayControlError as exc:
            if "BINDING_NOT_EXPIRED" not in str(exc):
                raise
            # Before the first accepted Provider call there is deliberately no
            # gateway ledger to extend.  The unexpired binding itself is the
            # lease; preserve it and rotate only the volatile actor headers.
            lease = {
                "status": "LEASE_STILL_ACTIVE_BEFORE_FIRST_PROVIDER_CALL",
                "usage_counters_reset": False,
                "new_run_created": False,
            }
        actor_rebind = rebind_gateway_same_run_actors(
            run_id,
            requested_by="finflux.same-run-manager-rearm",
            reason="轮换短期Actor身份并保持原Run、原任务与原Token基线",
        )
        body = (
            f"FINFLUX_MANAGER_ROUTE_AUTH {run['case_id']} {run_id}\n"
            f"route={run['matrix_handle']['route']} workers={','.join(CORE_WORKERS)}\n"
            f"handle_sha256={run['matrix_handle']['handle_sha256']} dispatch_hash={run['dispatch_hash']}\n"
            "只校验结构化路由，不读取金融数值、不调用工具、不执行Worker任务。\n"
            f"REPLY_EXACTLY: {run['expected_manager_authorization']}"
        )
        prior_attempts = list(run.get("manager_rearm_attempts") or [])
        if not prior_attempts and run.get("manager_rearm_receipt"):
            prior_attempts.append(run["manager_rearm_receipt"])
        attempt_no = len(prior_attempts) + 1
        event_id = MatrixClient.admin().send(
            run["manager_room_id"], body, mention=run["manager_id"],
            transaction_id=_transaction(run_id, f"manager-rearm-{attempt_no}"),
        )
        receipt = {
            "protocol": "FINFLUX_SAME_RUN_MANAGER_REARM_V1",
            "run_id": run_id,
            "status": "RESENT",
            "event_id": event_id,
            "new_run_created": False,
            "provider_calls_before_rearm": 0,
            "provider_tokens_before_rearm": 0,
            "attempt": attempt_no,
            "lease": lease,
            "actor_rebind": actor_rebind,
            "rearmed_at_utc": utc_now(),
        }
        receipt["receipt_sha256"] = sha256(receipt)
        prior_attempts.append(receipt)
        run["manager_rearm_attempts"] = prior_attempts
        run["manager_rearm_receipt"] = receipt
        self.store.save(run)
        return receipt

    def recover_manager_session(
        self, run_id: str, requested_by: str, reason: str
    ) -> dict[str, Any]:
        """Quarantine one wedged Manager room session and resume the same Run.

        This recovery is intentionally narrow: it is permitted only before the
        first accepted Provider call, moves (never deletes) the one Run-scoped
        session file, restarts only the Manager container, and then publishes a
        fresh Matrix event with a unique transaction id.
        """
        run = self.store.load(run_id)
        if run.get("state") in _TERMINAL:
            raise AgentTeamsConfigurationError("终态Run不能恢复Manager会话")
        usage_snapshot = gateway_usage(run_id)
        if usage_snapshot.get("status") not in {"NOT_CAPTURED", "NO_MODEL_CALL"}:
            raise AgentTeamsConfigurationError(
                "Manager进程恢复仅允许零有效Provider调用的同一Run"
            )
        room_id = str(run.get("manager_room_id") or "")
        match = re.fullmatch(r"(![A-Za-z0-9_-]+):.+", room_id)
        if not match:
            raise AgentTeamsConfigurationError("Manager Room ID无效，拒绝会话恢复")
        room_localpart = match.group(1)
        script = r'''
import hashlib, json, pathlib, shutil, sys
root = pathlib.Path("/root/manager-workspace/.qwenpaw/workspaces/default/sessions/matrix")
room, run_id = sys.argv[1], sys.argv[2]
matches = sorted(root.glob(room + "--*.json"))
quarantine = root / "quarantine" / run_id
quarantine.mkdir(parents=True, exist_ok=True)
rows = []
for source in matches:
    raw = source.read_bytes()
    target = quarantine / source.name
    if target.exists():
        target = quarantine / (source.stem + "-retry" + source.suffix)
    shutil.move(str(source), str(target))
    rows.append({"name": source.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
print(json.dumps({"status": "QUARANTINED" if rows else "NO_SESSION_FILE", "files": rows}))
'''
        raw_quarantine = docker(
            "exec", MANAGER, "python3", "-c", script, room_localpart, run_id,
            timeout=30,
        )
        try:
            quarantine = json.loads(raw_quarantine)
        except json.JSONDecodeError as exc:
            raise AgentTeamsUnavailable("Manager会话隔离回执不可解析") from exc
        if quarantine.get("status") != "QUARANTINED":
            raise AgentTeamsConfigurationError("未找到当前Run的Manager会话文件，拒绝宽泛清理")
        docker("restart", MANAGER, timeout=90)
        readiness_script = r'''
import json, time, urllib.request
deadline = time.time() + 45
last = ""
while time.time() < deadline:
    try:
        response = urllib.request.urlopen("http://127.0.0.1:18799/", timeout=2)
        print(json.dumps({"status": "READY", "http_status": response.status}))
        raise SystemExit(0)
    except Exception as exc:
        last = type(exc).__name__
        time.sleep(2)
print(json.dumps({"status": "NOT_READY", "last_error": last}))
raise SystemExit(2)
'''
        try:
            readiness = json.loads(
                docker(
                    "exec", MANAGER, "python3", "-c", readiness_script,
                    timeout=55,
                )
            )
        except json.JSONDecodeError as exc:
            raise AgentTeamsUnavailable("Manager就绪探针回执不可解析") from exc
        process_receipt = {
            "protocol": "FINFLUX_SAME_RUN_MANAGER_PROCESS_RECOVERY_V1",
            "run_id": run_id,
            "status": "MANAGER_RESTARTED",
            "requested_by": requested_by,
            "reason": reason,
            "new_run_created": False,
            "provider_calls_before_recovery": 0,
            "provider_tokens_before_recovery": 0,
            "quarantined_sessions": quarantine.get("files") or [],
            "manager_readiness": readiness,
            "recovered_at_utc": utc_now(),
        }
        process_receipt["receipt_sha256"] = sha256(process_receipt)
        process_attempts = list(run.get("manager_process_recovery_attempts") or [])
        process_attempts.append(process_receipt)
        run["manager_process_recovery_attempts"] = process_attempts
        self.store.save(run)
        rearm = self.rearm_manager(run_id)
        receipt = {**process_receipt, "status": "MANAGER_RESTARTED_AND_REARMED", "rearm": rearm}
        receipt["receipt_sha256"] = sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
        return receipt

    def human_decision(self, run_id: str, decision: str, reviewer: str, reason: str = "") -> dict[str, Any]:
        if decision not in {"CONFIRM_BLOCK", "REQUEST_EVIDENCE", "APPROVE_PASS"}:
            raise ValueError("unsupported human decision")
        run = self.store.load(run_id)
        if run.get("state") != "AWAITING_HUMAN":
            raise AgentTeamsConfigurationError("Run尚未进入AWAITING_HUMAN")
        formal_path = FORMAL_RUNS_ROOT / f"{run_id}.json"
        formal = json.loads(formal_path.read_text(encoding="utf-8-sig"))
        datapass = formal.get("datapass") or {}
        if datapass.get("protocol") != DATAPASS_PROTOCOL:
            raise AgentTeamsConfigurationError("正式DataPass尚未固化")
        validate_datapass(datapass, envelope=formal.get("case_envelope"), worker_artifacts=(formal.get("agent_result") or {}).get("worker_artifacts") or {})
        human = MatrixClient.human()
        admin = MatrixClient.admin()
        if human.user_id not in admin.joined_members(run["leader_room_id"]):
            try:
                admin.invite(run["leader_room_id"], str(human.user_id))
            except AgentTeamsUnavailable as exc:
                if "Matrix HTTP 403" not in str(exc):
                    raise
        human.join(run["leader_room_id"])
        body = (
            f"HUMAN_DECISION {run['case_id']} {run_id}\n"
            f"decision={decision} reviewer={human.user_id} datapass_sha256={datapass['datapass_sha256']}\n"
            f"reason={reason.strip() or 'NOT_PROVIDED'}"
        )
        event_id = human.send(run["leader_room_id"], body, mention=run["leader_id"], transaction_id=_transaction(run_id, "human-decision"))
        record = {"reviewer": human.user_id, "decision": decision, "reason": reason.strip(), "datapass_sha256": datapass["datapass_sha256"], "room_id": run["leader_room_id"], "event_id": event_id, "decided_at_utc": utc_now(), "scope": "AGENTTEAMS_MATRIX_HUMAN_GATE"}
        record["decision_binding_sha256"] = sha256(record)
        run["human_decision"] = record
        result = dict(run.get("agent_result") or {})
        result["human_decision"] = record
        result["run_state"] = "COMPLETED"
        result["final_decision"] = str(
            result.get("leader_recommendation") or "PENDING"
        )
        run["agent_result"] = result
        run["state"] = "COMPLETED"
        self.store.save(run)
        return record

    def terminal_status(self, run_id: str) -> dict[str, Any]:
        run = self.store.load(run_id)
        return {"run_id": run_id, "state": run.get("state"), "terminal": run.get("state") in _TERMINAL, "has_emergency_stop_record": bool(run.get("emergency_stop_record"))}

    def emergency_stop(self, run_id: str, **fields: Any) -> dict[str, Any]:
        run = self.store.load(run_id)
        record = self.stop_ledger.append(
            run_id=run_id,
            case_id=str(run["case_id"]),
            terminal_state=str(fields["terminal_state"]),
            actor=str(fields["actor"]),
            reason=str(fields["reason"]),
            token_guard_snapshot=fields.get("token_guard_snapshot") or {},
            container_action_evidence_refs=fields.get("container_action_evidence_refs") or [],
            case_envelope_sha256=str(run.get("case_envelope_sha256") or ""),
            reason_codes=fields.get("reason_codes") or [],
        )
        run["state"] = fields["terminal_state"]
        run["emergency_stop_record"] = record
        self.store.save(run)
        return run

    def recover_terminal(self, run_id: str, **fields: Any) -> dict[str, Any]:
        run = self.store.load(run_id)
        if run.get("emergency_stop_record"):
            return run
        return self.emergency_stop(run_id, **fields)

    @staticmethod
    def reset_sessions() -> dict[str, Any]:
        return {
            "protocol": "FINFLUX_RUN_SCOPED_SESSION_POLICY_V2",
            "status": "NOT_REQUIRED",
            "model_called": False,
            "provider_tokens": 0,
            "reason": "每次Run创建独立Manager和Case Lead Room；不再清空或复用历史会话。",
        }
