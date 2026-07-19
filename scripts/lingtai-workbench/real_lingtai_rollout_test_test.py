#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("real_lingtai_rollout_test.py")
SPEC = importlib.util.spec_from_file_location("real_lingtai_rollout_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RaisingProcess:
    pid = 424242

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.poll_count = 0
        self.wait_count = 0

    def poll(self) -> int | None:
        self.calls.append("poll")
        self.poll_count += 1
        return None if self.poll_count == 1 else 1

    def wait(self, *, timeout: float) -> int:
        self.calls.append(f"wait:{timeout:g}")
        self.wait_count += 1
        if self.wait_count == 1:
            raise subprocess.TimeoutExpired("fake-server", timeout)
        raise OSError("wait boom")

class RaisingLog:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def flush(self) -> None:
        self.calls.append("flush")
        raise OSError("flush boom")

    def close(self) -> None:
        self.calls.append("close")
        raise OSError("close boom")


class RealLingtaiRolloutRunnerTest(unittest.TestCase):
    def assert_processes_exit(self, pids: list[int]) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(pid_exists(pid) for pid in pids):
            time.sleep(0.05)
        self.assertFalse(
            [pid for pid in pids if pid_exists(pid)],
            "process-group cleanup left a child or descendant alive",
        )

    @staticmethod
    def kill_remaining(pids: list[int]) -> None:
        for pid in pids:
            if pid_exists(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_state_dir_is_canonical_across_symlinked_temp_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical_root = root / "private-var"
            canonical_state = canonical_root / "state"
            canonical_state.mkdir(parents=True)
            alias_root = root / "var"
            alias_root.symlink_to(canonical_root, target_is_directory=True)

            with mock.patch.object(
                MODULE.tempfile,
                "mkdtemp",
                return_value=str(alias_root / canonical_state.name),
            ):
                state_dir = MODULE.make_state_dir()

            self.assertEqual(state_dir, canonical_state.resolve())
            self.assertFalse(str(state_dir).startswith(str(alias_root)))

    def test_timeout_terminates_child_and_grandchild_and_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pids"
            child = """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)",
    ],
)
Path(sys.argv[1]).write_text(
    f"{os.getpid()} {grandchild.pid}", encoding="utf-8"
)
print("timeout-output-marker", flush=True)
time.sleep(60)
"""
            observed_pids: list[int] = []
            try:
                with self.assertRaisesRegex(
                    MODULE.AcceptanceError, "timed out after 1s"
                ) as raised:
                    MODULE.run_command(
                        [sys.executable, "-c", child, str(pid_file)],
                        cwd=root,
                        env=os.environ.copy(),
                        timeout=1,
                        cleanup_timeout=0.2,
                        label="spawn-tree fixture",
                    )
                self.assertIn("timeout-output-marker", str(raised.exception))
                observed_pids = [
                    int(value) for value in pid_file.read_text(encoding="utf-8").split()
                ]
                self.assertEqual(len(observed_pids), 2)
                self.assert_processes_exit(observed_pids)
            finally:
                self.kill_remaining(observed_pids)

    def test_timeout_kills_descendant_after_leader_exits_and_pipes_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pids"
            ready_file = root / "descendant-ready"
            leader = """
import os
import subprocess
import sys
import time
from pathlib import Path

descendant = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import os,signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(60)",
        sys.argv[2],
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 5
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
Path(sys.argv[1]).write_text(
    f"{os.getpid()} {descendant.pid}", encoding="utf-8"
)
print("leader-pipes-closed-marker", flush=True)
time.sleep(60)
"""
            observed_pids: list[int] = []
            try:
                with self.assertRaisesRegex(
                    MODULE.AcceptanceError, "timed out after 1s"
                ) as raised:
                    MODULE.run_command(
                        [
                            sys.executable,
                            "-c",
                            leader,
                            str(pid_file),
                            str(ready_file),
                        ],
                        cwd=root,
                        env=os.environ.copy(),
                        timeout=1,
                        cleanup_timeout=0.2,
                        label="closed-pipes process-group fixture",
                    )
                self.assertIn("leader-pipes-closed-marker", str(raised.exception))
                observed_pids = [
                    int(value) for value in pid_file.read_text(encoding="utf-8").split()
                ]
                self.assertEqual(len(observed_pids), 2)
                self.assert_processes_exit(observed_pids)
            finally:
                self.kill_remaining(observed_pids)

    def test_stop_server_kills_descendant_process_group_and_closes_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pids"
            ready_file = root / "server-child-ready"
            log_path = root / "server.log"
            leader = """
import os
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import os,signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(60)",
        sys.argv[2],
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 5
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
time.sleep(60)
"""
            observed_pids: list[int] = []
            log = log_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    leader,
                    str(pid_file),
                    str(ready_file),
                ],
                cwd=root,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                observed_pids = [
                    int(value) for value in pid_file.read_text(encoding="utf-8").split()
                ]
                error = MODULE.stop_server(process, log, terminate_timeout=0.2)
                self.assertIsNone(error)
                self.assertTrue(log.closed)
                self.assert_processes_exit(observed_pids)
            finally:
                if not log.closed:
                    log.close()
                self.kill_remaining(observed_pids)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_keyboard_interrupt_cleans_process_group_and_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_path = root / "interrupt-child.py"
            wrapper_path = root / "interrupt-wrapper.py"
            pid_file = root / "pids"
            child_path.write_text(
                """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)",
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(sys.argv[1]).write_text(
    f"{os.getpid()} {grandchild.pid}", encoding="utf-8"
)
print("ctrl-c-child-output", flush=True)
time.sleep(60)
""",
                encoding="utf-8",
            )
            wrapper_path.write_text(
                """
import importlib.util
import os
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("rollout_runner", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    module.run_command(
        [sys.executable, sys.argv[2], sys.argv[3]],
        cwd=Path(sys.argv[4]),
        env=os.environ.copy(),
        timeout=60,
        cleanup_timeout=0.2,
        label="keyboard-interrupt fixture",
    )
except KeyboardInterrupt as error:
    print("wrapper-keyboard-interrupt", file=sys.stderr)
    print(getattr(error, "rollout_command_detail", ""), file=sys.stderr)
    cleanup_error = getattr(error, "rollout_cleanup_error", None)
    if cleanup_error:
        print(cleanup_error, file=sys.stderr)
    raise SystemExit(130)
raise SystemExit(2)
""",
                encoding="utf-8",
            )
            wrapper = subprocess.Popen(
                [
                    sys.executable,
                    str(wrapper_path),
                    str(SCRIPT),
                    str(child_path),
                    str(pid_file),
                    str(root),
                ],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            observed_pids: list[int] = []
            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    if wrapper.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertTrue(pid_file.exists(), "interrupt child did not start")
                observed_pids = [
                    int(value) for value in pid_file.read_text(encoding="utf-8").split()
                ]
                os.kill(wrapper.pid, signal.SIGINT)
                stdout, stderr = wrapper.communicate(timeout=10)
                self.assertEqual(wrapper.returncode, 130, stdout + stderr)
                self.assertIn("wrapper-keyboard-interrupt", stderr)
                self.assertIn("ctrl-c-child-output", stderr)
                self.assert_processes_exit(observed_pids)
            finally:
                if not observed_pids and pid_file.exists():
                    observed_pids = [
                        int(value)
                        for value in pid_file.read_text(encoding="utf-8").split()
                    ]
                self.kill_remaining(observed_pids)
                if wrapper.poll() is None:
                    try:
                        os.killpg(wrapper.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    wrapper.wait(timeout=5)

    def test_nonzero_leader_cleans_descendant_after_pipes_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pids"
            ready_file = root / "descendant-ready"
            leader = """
import os
import subprocess
import sys
import time
from pathlib import Path

descendant = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import os,signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(60)",
        sys.argv[2],
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 5
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
Path(sys.argv[1]).write_text(
    f"{os.getpid()} {descendant.pid}", encoding="utf-8"
)
print("nonzero-leader-output", flush=True)
raise SystemExit(7)
"""
            observed_pids: list[int] = []
            try:
                with self.assertRaisesRegex(
                    MODULE.AcceptanceError, "exited with 7"
                ) as raised:
                    MODULE.run_command(
                        [
                            sys.executable,
                            "-c",
                            leader,
                            str(pid_file),
                            str(ready_file),
                        ],
                        cwd=root,
                        env=os.environ.copy(),
                        timeout=5,
                        cleanup_timeout=0.2,
                        label="nonzero process-group fixture",
                    )
                self.assertIn("nonzero-leader-output", str(raised.exception))
                observed_pids = [
                    int(value) for value in pid_file.read_text(encoding="utf-8").split()
                ]
                self.assert_processes_exit(observed_pids)
            finally:
                self.kill_remaining(observed_pids)

    def test_success_does_not_enter_process_group_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(MODULE, "collect_after_timeout") as cleanup:
                result = MODULE.run_command(
                    [sys.executable, "-c", "print('success-marker')"],
                    cwd=Path(tmp),
                    env=os.environ.copy(),
                    timeout=5,
                    label="successful fixture",
                )
        self.assertEqual(result.returncode, 0)
        self.assertIn("success-marker", result.stdout)
        cleanup.assert_not_called()

    def test_server_cleanup_aggregates_every_error_and_closes_log(self) -> None:
        process = RaisingProcess()
        log = RaisingLog()

        with (
            mock.patch.object(
                MODULE,
                "process_group_status",
                return_value=(True, None),
            ),
            mock.patch.object(
                MODULE,
                "signal_process_group",
                side_effect=("terminate boom", "kill boom"),
            ) as group_signal,
            mock.patch.object(
                MODULE,
                "wait_process_group_exit",
                side_effect=(
                    (False, "term probe boom"),
                    (False, "kill probe boom"),
                ),
            ),
        ):
            error = MODULE.stop_server(process, log, terminate_timeout=0.01)

        self.assertIsNotNone(error)
        assert error is not None
        for marker in (
            "terminate boom",
            "kill boom",
            "wait boom",
            "term probe boom",
            "kill probe boom",
            "flush boom",
            "close boom",
        ):
            self.assertIn(marker, error)
        self.assertEqual(group_signal.call_count, 2)
        self.assertEqual(log.calls, ["flush", "close"])


if __name__ == "__main__":
    unittest.main()
