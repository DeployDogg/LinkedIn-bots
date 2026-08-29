#!/usr/bin/env python3
"""Generate credential-isolated env_file for LinkedIn worker containers."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

EXCLUDED_KEYS = frozenset(
    {
        "LINKEDIN_EMAIL",
        "LINKEDIN_PASSWORD",
        "LINKEDIN_SESSION_PATH",
        "LINKEDIN_CHROMIUM_PROFILE_DIR",
        "LINKEDIN_CONNECT_USE_CDP",
        "LINKEDIN_LOGIN_URL",
        "LINKEDIN_CDP_ENDPOINT",
    }
)
_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


@dataclass(frozen=True)
class GenerateResult:
    path: Path
    total_keys: int
    kept_keys: int
    excluded_count: int
    excluded_keys: tuple[str, ...]


def _assignment_key(line: str) -> str | None:
    match = _ASSIGNMENT_RE.match(line)
    return match.group(1) if match else None


def _filter_lines(lines: Iterable[str]) -> tuple[list[str], int, list[str]]:
    kept: list[str] = []
    total_keys = 0
    excluded: list[str] = []
    for line in lines:
        key = _assignment_key(line)
        if key is None:
            kept.append(line)
            continue
        total_keys += 1
        if key in EXCLUDED_KEYS:
            excluded.append(key)
            continue
        kept.append(line)
    return kept, total_keys, excluded


def generate_worker_env(source: Path | str = ".env", target: Path | str = ".env.workers") -> GenerateResult:
    source_path = Path(source)
    target_path = Path(target)
    lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept_lines, total_keys, excluded = _filter_lines(lines)
    content = "".join(kept_lines)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target_path)
        os.chmod(target_path, 0o600)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise

    result = GenerateResult(
        path=target_path,
        total_keys=total_keys,
        kept_keys=total_keys - len(excluded),
        excluded_count=len(excluded),
        excluded_keys=tuple(sorted(set(excluded))),
    )
    print(
        "generated "
        f"path={result.path} "
        f"total_keys={result.total_keys} "
        f"kept_keys={result.kept_keys} "
        f"excluded_count={result.excluded_count} "
        f"excluded_keys={','.join(result.excluded_keys)}"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate .env.workers without LinkedIn credentials/session settings.")
    parser.add_argument("--source", default=".env", help="source dotenv path; default: .env")
    parser.add_argument("--target", default=".env.workers", help="target worker dotenv path; default: .env.workers")
    args = parser.parse_args()
    generate_worker_env(Path(args.source), Path(args.target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
