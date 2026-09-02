from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import (
    AgentTeamsConfigurationError,
    AgentTeamsUnavailable,
    read_env,
)


class MatrixClient:
    """Minimal Matrix client used by the AgentTeams bridge.

    It creates only Run-scoped control rooms and reads only those room IDs.
    Historical joined-room enumeration is deliberately unsupported.
    """

    def __init__(self, token: str | None = None, user_id: str | None = None) -> None:
        env = read_env()
        port = env.get("AGENTTEAMS_MATRIX_CLIENT_PORT", "18080").strip() or "18080"
        self.base_url = f"http://127.0.0.1:{port}"
        self.domain = env.get(
            "AGENTTEAMS_MATRIX_DOMAIN", "matrix-local.agentteams.io:18080"
        ).strip()
        self.token = token
        self.user_id = user_id

    def request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None if data is None else json.dumps(
            data, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise AgentTeamsUnavailable(f"Matrix HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentTeamsUnavailable(f"Matrix连接失败: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentTeamsUnavailable("Matrix返回非JSON响应") from exc

    @classmethod
    def login(cls, user: str, password: str) -> "MatrixClient":
        if not user or not password:
            raise AgentTeamsConfigurationError("Matrix用户名或密码未配置")
        client = cls()
        response = client.request(
            "POST",
            "/_matrix/client/v3/login",
            {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": user},
                "password": password,
            },
        )
        token = str(response.get("access_token") or "")
        user_id = str(response.get("user_id") or "")
        if not token or not user_id:
            raise AgentTeamsConfigurationError(f"Matrix用户{user}登录失败")
        return cls(token, user_id)

    @classmethod
    def admin(cls) -> "MatrixClient":
        env = read_env()
        return cls.login(
            env.get("AGENTTEAMS_ADMIN_USER", "admin"),
            env.get("AGENTTEAMS_ADMIN_PASSWORD", ""),
        )

    @classmethod
    def human(cls) -> "MatrixClient":
        env = read_env()
        return cls.login(
            env.get("FINCHANGE_MATRIX_HUMAN_USER", "finchange-data-owner"),
            env.get("FINCHANGE_MATRIX_HUMAN_PASSWORD", ""),
        )

    def mxid(self, localpart: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._=+/\-]+", localpart):
            raise AgentTeamsConfigurationError("Matrix localpart无效")
        return f"@{localpart}:{self.domain}"

    def create_room(
        self, *, run_id: str, actor: str, purpose: str, invite: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        actor_id = self.mxid(actor)
        invited = list(dict.fromkeys((actor_id, *(self.mxid(item) for item in invite))))
        response = self.request(
            "POST",
            "/_matrix/client/v3/createRoom",
            {
                "is_direct": True,
                "invite": invited,
                "preset": "trusted_private_chat",
                "name": f"FinFlux {purpose} {run_id}",
                "topic": f"FINFLUX_RUN {run_id} {purpose}",
            },
        )
        room_id = str(response.get("room_id") or "")
        if not room_id.startswith("!"):
            raise AgentTeamsUnavailable(f"无法创建{purpose} Room")
        return {
            "room_id": room_id,
            "actor_id": actor_id,
            "invited_actor_ids": invited,
            "created_for_run_id": run_id,
            "freshly_created": True,
        }

    def join(self, room_id: str) -> None:
        encoded = urllib.parse.quote(room_id, safe="")
        self.request("POST", f"/_matrix/client/v3/join/{encoded}", {})

    def invite(self, room_id: str, user_id: str) -> None:
        encoded = urllib.parse.quote(room_id, safe="")
        self.request(
            "POST",
            f"/_matrix/client/v3/rooms/{encoded}/invite",
            {"user_id": user_id},
        )

    def joined_members(self, room_id: str) -> set[str]:
        encoded = urllib.parse.quote(room_id, safe="")
        response = self.request(
            "GET", f"/_matrix/client/v3/rooms/{encoded}/joined_members"
        )
        return {
            str(user_id)
            for user_id in (response.get("joined") or {}).keys()
            if str(user_id).startswith("@")
        }

    def ensure_joined(
        self, room_id: str, localparts: tuple[str, ...], timeout_s: float = 20.0
    ) -> dict[str, Any]:
        """Invite the bounded Team and wait until Matrix confirms membership."""

        expected = {self.mxid(item) for item in localparts}
        joined = self.joined_members(room_id)
        for user_id in sorted(expected - joined):
            try:
                self.invite(room_id, user_id)
            except AgentTeamsUnavailable as exc:
                # Matrix returns 403 when an invitation is already pending.  The
                # membership poll below remains the source of truth.
                if "Matrix HTTP 403" not in str(exc):
                    raise
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            joined = self.joined_members(room_id)
            if expected <= joined:
                break
            time.sleep(0.5)
        missing = sorted(expected - joined)
        return {
            "room_id": room_id,
            "expected": sorted(expected),
            "joined": sorted(expected & joined),
            "missing": missing,
            "ready": not missing,
        }

    def send(
        self,
        room_id: str,
        body: str,
        *,
        mention: str | None = None,
        transaction_id: str | None = None,
    ) -> str:
        encoded = urllib.parse.quote(room_id, safe="")
        txn = transaction_id or f"finflux_{time.time_ns()}"
        content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if mention:
            content["m.mentions"] = {"user_ids": [mention]}
        response = self.request(
            "PUT",
            f"/_matrix/client/v3/rooms/{encoded}/send/m.room.message/{txn}",
            content,
        )
        event_id = str(response.get("event_id") or "")
        if not event_id:
            raise AgentTeamsUnavailable("Matrix未返回event_id")
        return event_id

    def messages(self, room_id: str, limit: int = 100) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(room_id, safe="")
        response = self.request(
            "GET", f"/_matrix/client/v3/rooms/{encoded}/messages?dir=b&limit={limit}"
        )
        return [item for item in response.get("chunk", []) if isinstance(item, dict)]
