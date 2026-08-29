#!/usr/bin/env python3
"""Cross-container runtime lock wrapper for LinkedIn workers.

Uses fcntl.flock on a shared Docker volume. The lock is tied to this process'
open file descriptor, so process death releases it without stale lock cleanup.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

DEFAULT_LOCK_PATH = "/shared/runtime/linkedin-workers.lock"
DEFAULT_WAIT_TIMEOUT = 0.0

_child: subprocess.Popen[bytes] | None = None
_child_pgid: int | None = None
_pending_signal: int | None = None


def _log(message: str) -> None:
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    print(f"[{stamp}] linkedin_runtime_lock: {message}", flush=True)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a child under an exclusive flock runtime lock")
    parser.add_argument(
        "--lock-path",
        default=os.environ.get("LINKEDIN_RUNTIME_LOCK_PATH", DEFAULT_LOCK_PATH),
        help=f"lock file path (default: env LINKEDIN_RUNTIME_LOCK_PATH or {DEFAULT_LOCK_PATH})",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=float(os.environ.get("LINKEDIN_RUNTIME_LOCK_WAIT_TIMEOUT", DEFAULT_WAIT_TIMEOUT)),
        help="seconds to wait for a busy lock before exiting 0 without running child",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="child command after --")
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("child command is required after --")
    if args.wait_timeout < 0:
        parser.error("--wait-timeout must be >= 0")
    return args


def _acquire_lock(lock_file, lock_path: Path, wait_timeout: float) -> bool:
    deadline = time.monotonic() + wait_timeout
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if wait_timeout <= 0 or time.monotonic() >= deadline:
                _log(f"busy lock={lock_path}; child not started; exiting 0")
                return False
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def _write_owner_metadata(lock_file) -> None:
    metadata = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "hostname": socket.gethostname(),
        "argv": sys.argv,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(json.dumps(metadata, sort_keys=True) + "\n")
    lock_file.flush()
    os.fsync(lock_file.fileno())


def _forward_signal(signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
    global _pending_signal
    _pending_signal = signum
    if _child is not None and _child.poll() is None:
        try:
            if _child_pgid is not None and _child_pgid != os.getpgrp():
                os.killpg(_child_pgid, signum)
                _log(f"forwarded signal {signum} to child process group pgid={_child_pgid}")
            else:
                _child.send_signal(signum)
                _log(f"forwarded signal {signum} to child pid={_child.pid}")
        except ProcessLookupError:
            pass


def _run_child(command: list[str]) -> int:
    global _child, _child_pgid, _pending_signal
    previous_handlers = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }
    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)
    try:
        _child = subprocess.Popen(command, start_new_session=True)
        _child_pgid = os.getpgid(_child.pid)
        if _child_pgid == os.getpgrp():
            raise RuntimeError("child process group unexpectedly matches helper process group")
        if _pending_signal is not None:
            _forward_signal(_pending_signal, None)
        return _child.wait()
    finally:
        _child = None
        _child_pgid = None
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    lock_path = Path(args.lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if not _acquire_lock(lock_file, lock_path, args.wait_timeout):
            return 0
        _write_owner_metadata(lock_file)
        _log(f"acquired lock={lock_path}; starting child: {' '.join(args.command)}")
        code = _run_child(list(args.command))
        _log(f"child exited code={code}; releasing lock={lock_path}")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
