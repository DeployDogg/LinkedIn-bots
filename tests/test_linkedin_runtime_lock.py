"""Focused behavior tests for linkedin_runtime_lock.py.

No Docker, no browser startup, no LinkedIn/network calls. These tests exercise the
real helper via subprocesses so flock semantics, child lifecycle, and exit codes
are verified by the OS.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "linkedin_runtime_lock.py"


class LinkedinRuntimeLockBehaviorTest(unittest.TestCase):
    def test_busy_lock_exits_zero_without_running_second_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lock_path = tmp_path / "workers.lock"
            first_marker = tmp_path / "first-started"
            second_marker = tmp_path / "second-ran"

            first = subprocess.Popen(
                [
                    sys.executable,
                    str(HELPER),
                    "--lock-path",
                    str(lock_path),
                    "--wait-timeout",
                    "5",
                    "--",
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        f"""
                        from pathlib import Path
                        import time
                        Path({str(first_marker)!r}).write_text('started')
                        time.sleep(2)
                        """
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self._wait_for_path(first_marker)
                second = subprocess.run(
                    [
                        sys.executable,
                        str(HELPER),
                        "--lock-path",
                        str(lock_path),
                        "--wait-timeout",
                        "0.2",
                        "--",
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(second_marker)!r}).write_text('ran')",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=5,
                )

                self.assertEqual(0, second.returncode, second.stderr + second.stdout)
                self.assertFalse(second_marker.exists(), "busy lock must not start second child")
                self.assertIn("busy", (second.stdout + second.stderr).lower())
            finally:
                first.terminate()
                first.communicate(timeout=5)

    def test_child_exit_code_is_propagated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(HELPER),
                    "--lock-path",
                    str(Path(tmp) / "workers.lock"),
                    "--wait-timeout",
                    "1",
                    "--",
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(37)",
                ],
                text=True,
                capture_output=True,
                timeout=5,
            )

        self.assertEqual(37, result.returncode, result.stderr + result.stdout)

    def test_lock_is_reacquirable_after_lock_owner_process_dies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lock_path = tmp_path / "workers.lock"
            started_marker = tmp_path / "started"
            child_pid_path = tmp_path / "child.pid"
            reacquired_marker = tmp_path / "reacquired"
            owner: subprocess.Popen[bytes] | None = None

            try:
                owner = subprocess.Popen(
                    [
                        sys.executable,
                        str(HELPER),
                        "--lock-path",
                        str(lock_path),
                        "--wait-timeout",
                        "1",
                        "--",
                        sys.executable,
                        "-c",
                        textwrap.dedent(
                            f"""
                            from pathlib import Path
                            import os
                            import time
                            Path({str(child_pid_path)!r}).write_text(str(os.getpid()))
                            Path({str(started_marker)!r}).write_text('started')
                            time.sleep(30)
                            """
                        ),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._wait_for_path(started_marker)
                owner.kill()
                owner.wait(timeout=5)

                reacquire = subprocess.run(
                    [
                        sys.executable,
                        str(HELPER),
                        "--lock-path",
                        str(lock_path),
                        "--wait-timeout",
                        "2",
                        "--",
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(reacquired_marker)!r}).write_text('ok')",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=5,
                )

                self.assertEqual(0, reacquire.returncode, reacquire.stderr + reacquire.stdout)
                self.assertTrue(reacquired_marker.exists(), "flock must not stale-block after owner death")
            finally:
                if owner is not None and owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
                if child_pid_path.exists():
                    self._terminate_pid(int(child_pid_path.read_text()))

    def test_sigterm_is_forwarded_to_child_and_helper_exits_like_terminated_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lock_path = tmp_path / "workers.lock"
            ready_marker = tmp_path / "ready"
            term_marker = tmp_path / "term-received"
            child_code = textwrap.dedent(
                f"""
                import signal
                import sys
                import time
                from pathlib import Path

                def handle_term(signum, frame):
                    Path({str(term_marker)!r}).write_text(str(signum))
                    sys.exit(42)

                signal.signal(signal.SIGTERM, handle_term)
                signal.signal(signal.SIGINT, handle_term)
                Path({str(ready_marker)!r}).write_text('ready')
                while True:
                    time.sleep(0.1)
                """
            )
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(HELPER),
                    "--lock-path",
                    str(lock_path),
                    "--wait-timeout",
                    "1",
                    "--",
                    sys.executable,
                    "-c",
                    child_code,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._wait_for_path(ready_marker)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=5)

            self.assertEqual(42, proc.returncode, stderr + stdout)
            self.assertTrue(term_marker.exists(), "child must observe forwarded SIGTERM")

    def test_sigterm_reaches_shell_descendant_process_and_does_not_leave_it_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lock_path = tmp_path / "workers.lock"
            descendant_pid_path = tmp_path / "descendant.pid"
            descendant_ready = tmp_path / "descendant.ready"
            descendant_term = tmp_path / "descendant.term"
            descendant_code = textwrap.dedent(
                f"""
                import os
                import signal
                import sys
                import time
                from pathlib import Path

                def handle_term(signum, frame):
                    Path({str(descendant_term)!r}).write_text(str(signum))
                    sys.exit(0)

                signal.signal(signal.SIGTERM, handle_term)
                signal.signal(signal.SIGINT, handle_term)
                Path({str(descendant_pid_path)!r}).write_text(str(os.getpid()))
                Path({str(descendant_ready)!r}).write_text('ready')
                while True:
                    time.sleep(0.1)
                """
            )
            descendant_script = tmp_path / "descendant.py"
            descendant_script.write_text(descendant_code, encoding="utf-8")
            shell_script = textwrap.dedent(
                f"""
                {sys.executable} {descendant_script} &
                wait
                """
            )
            proc: subprocess.Popen[str] | None = None
            descendant_pid: int | None = None
            try:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(HELPER),
                        "--lock-path",
                        str(lock_path),
                        "--wait-timeout",
                        "1",
                        "--",
                        "bash",
                        "-c",
                        shell_script,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                self._wait_for_path(descendant_ready)
                descendant_pid = int(descendant_pid_path.read_text())
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)

                self.assertTrue(descendant_term.exists())
                self.assertFalse(
                    self._pid_is_alive(descendant_pid),
                    f"descendant pid {descendant_pid} remained alive after helper SIGTERM",
                )
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if descendant_pid is not None:
                    self._terminate_pid(descendant_pid)

    @staticmethod
    def _wait_for_path(path: Path, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {path}")

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _terminate_pid(cls, pid: int) -> None:
        if not cls._pid_is_alive(pid):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not cls._pid_is_alive(pid):
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


if __name__ == "__main__":
    unittest.main()
