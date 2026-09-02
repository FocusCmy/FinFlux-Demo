from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from live_intake import LiveIntakeRepository


class LiveDispatchQueueTests(unittest.TestCase):
    @staticmethod
    def _queued_run(run_id: str, requested_epoch: float) -> dict:
        return {
            "run_id": run_id,
            "submission_id": "SUB-QUEUE-TEST",
            "state": "DISPATCH_GUARDED",
            "events": [],
            "dispatch_request": {
                "protocol": "FINFLUX_BACKGROUND_DISPATCH_REQUEST_V1",
                "status": "QUEUED",
                "requested_epoch": requested_epoch,
                "next_attempt_epoch": 0,
            },
        }

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def test_latest_explicit_request_supersedes_older_unstarted_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = LiveIntakeRepository(Path(temp))
            old = self._queued_run("RUN-LIVE-QUEUE-OLD", 10.0)
            current = self._queued_run("RUN-LIVE-QUEUE-CURRENT", 20.0)
            old_path = repository.runs / f"{old['run_id']}.json"
            self._write(old_path, old)

            repository.get_run = Mock(return_value=copy.deepcopy(current))
            persisted: list[dict] = []
            repository._persist_run = Mock(
                side_effect=lambda run: persisted.append(copy.deepcopy(run))
            )

            result = repository.request_dispatch(
                current["run_id"], "test.operator"
            )

            retired = json.loads(old_path.read_text(encoding="utf-8"))
            old_request = retired["dispatch_request"]
            self.assertEqual(old_request["status"], "SUPERSEDED")
            self.assertEqual(
                old_request["superseded_by_run_id"], current["run_id"]
            )
            self.assertIsNone(old_request["next_attempt_epoch"])
            self.assertEqual(retired["run_id"], old["run_id"])
            self.assertEqual(result["dispatch_request"]["status"], "QUEUED")
            self.assertEqual(
                result["dispatch_request"]["superseded_request_count"], 1
            )
            self.assertEqual(len(persisted), 1)

    def test_existing_backlog_selects_newest_request_first(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = LiveIntakeRepository(Path(temp))
            older = self._queued_run("RUN-LIVE-QUEUE-OLDER", 10.0)
            newest = self._queued_run("RUN-LIVE-QUEUE-NEWEST", 30.0)
            self._write(repository.runs / f"{older['run_id']}.json", older)
            self._write(repository.runs / f"{newest['run_id']}.json", newest)

            selected = repository.next_dispatch_request()

            self.assertIsNotNone(selected)
            self.assertEqual(selected["run_id"], newest["run_id"])


if __name__ == "__main__":
    unittest.main()
