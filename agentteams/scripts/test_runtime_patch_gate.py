from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_patch_gate as gate


class RuntimePatchGateTests(unittest.TestCase):
    def test_pid1_environment_wins_over_pid1_cwd(self) -> None:
        workspace, source = gate.resolve_runtime_workspace(
            environ={"QWENPAW_WORKING_DIR": "/wrong/exec-workspace"},
            pid1_environ={"QWENPAW_WORKING_DIR": "/state/worker/.qwenpaw"},
            pid1_cwd=Path("/root/worker-workspace"),
            exists=lambda _path: False,
        )
        self.assertEqual(workspace, Path("/state/worker/.qwenpaw"))
        self.assertEqual(source, "PID1_ENV:QWENPAW_WORKING_DIR")

    def test_pid1_cwd_resolves_existing_hidden_runtime_root(self) -> None:
        pid_cwd = Path("/root/manager-workspace")
        workspace, source = gate.resolve_runtime_workspace(
            environ={},
            pid1_environ={},
            pid1_cwd=pid_cwd,
            exists=lambda path: path == pid_cwd / ".qwenpaw",
        )
        self.assertEqual(workspace, pid_cwd / ".qwenpaw")
        self.assertEqual(source, "PID1_CWD:.qwenpaw")

    def test_install_and_post_restart_readback_cover_both_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "server.py"
            source.write_text("# finflux-bounded-tool-profile\n", encoding="utf-8")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            workspace = root / "pid1-workspace" / ".qwenpaw"
            installed = gate.install_patch(
                "teamharness",
                source,
                expected,
                workspace=workspace,
                workspace_source="PID1_CWD:.qwenpaw",
                pid1_cwd=root / "pid1-workspace",
                root=root,
            )
            self.assertEqual(installed["status"], "INSTALL_VERIFIED")
            self.assertEqual(len(installed["targets"]), 2)
            readback = gate.readback_patch(
                "teamharness",
                expected,
                workspace=workspace,
                workspace_source="PID1_CWD:.qwenpaw",
                pid1_cwd=root / "pid1-workspace",
                root=root,
            )
            self.assertEqual(
                readback["status"], "POST_RESTART_READBACK_VERIFIED"
            )
            self.assertEqual(readback["targets"], installed["targets"])

    def test_readback_fails_if_only_one_copy_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "plugin.py"
            source.write_text("expected\n", encoding="utf-8")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            workspace = root / "manager-workspace" / ".qwenpaw"
            gate.install_patch(
                "manager",
                source,
                expected,
                workspace=workspace,
                workspace_source="PID1_CWD:.qwenpaw",
                pid1_cwd=root / "manager-workspace",
                root=root,
            )
            stateful = gate.patch_targets("manager", workspace, root)[1]
            stateful.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
                gate.readback_patch(
                    "manager",
                    expected,
                    workspace=workspace,
                    workspace_source="PID1_CWD:.qwenpaw",
                    pid1_cwd=root / "manager-workspace",
                    root=root,
                )


if __name__ == "__main__":
    unittest.main()
