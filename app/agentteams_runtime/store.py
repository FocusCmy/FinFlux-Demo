from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from .config import RUNS_ROOT


_LOCK = threading.RLock()
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{8,160}$")


class RunStore:
    """One immutable identity and one mutable projection file per Run."""

    def __init__(self, root: Path = RUNS_ROOT) -> None:
        self.root = root.resolve()

    def path(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(str(run_id or "")):
            raise ValueError("invalid run_id")
        return self.root / f"{run_id}.json"

    def exists(self, run_id: str) -> bool:
        return self.path(run_id).is_file()

    def load(self, run_id: str) -> dict[str, Any]:
        with _LOCK:
            return json.loads(self.path(run_id).read_text(encoding="utf-8-sig"))

    def save(self, run: dict[str, Any]) -> None:
        with _LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.path(str(run["run_id"]))
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(target)

    def active(self) -> list[dict[str, Any]]:
        terminal = {
            "COMPLETED",
            "STOPPED_BY_GATE",
            "BUDGET_EXCEEDED",
            "CANCELLED",
            "CANCELLED_BY_SESSION_RESET",
            "MODEL_CONTROL_CLEANUP_FAILED",
            "FAILED_CLOSED",
        }
        rows: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return rows
        for path in self.root.glob("*.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(row.get("state")) not in terminal:
                rows.append(row)
        return rows
