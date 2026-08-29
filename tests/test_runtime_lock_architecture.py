"""Static architecture contract for the cross-container LinkedIn runtime lock.

No Docker run, no browser startup, no LinkedIn/network calls.
"""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
WORKERS = {
    "jobseeker": "JobSeeker",
    "connectman": "ConnectMan",
    "sendman": "SendMan",
    "postliker": "PostLiker",
    "commentator": "Commentator",
}
HELPER_REL = Path("scripts/linkedin_runtime_lock.py")
EXPECTED_CONTAINER_HELPER = "/app/scripts/linkedin_runtime_lock.py"
EXPECTED_LOCK_ENV = "LINKEDIN_RUNTIME_LOCK_PATH"
EXPECTED_LOCK_DEFAULT = "/shared/runtime/linkedin-workers.lock"
EXPECTED_RUNTIME_VOLUME_NAME = "linkedin_runtime"
EXPECTED_RUNTIME_TARGET = "/shared/runtime"


class RuntimeLockArchitectureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with COMPOSE_PATH.open("r", encoding="utf-8") as fh:
            cls.compose: dict[str, Any] = yaml.safe_load(fh)
        cls.services: dict[str, dict[str, Any]] = cls.compose.get("services", {})

    def test_single_root_helper_exists_as_source_contract(self) -> None:
        helper = ROOT / HELPER_REL
        self.assertTrue(helper.is_file(), "repo must include scripts/linkedin_runtime_lock.py")
        text = helper.read_text(encoding="utf-8")
        self.assertIn("fcntl.flock", text)
        self.assertIn("subprocess.Popen", text)

    def test_all_worker_helper_copies_are_identical_to_root_contract(self) -> None:
        root_helper = ROOT / HELPER_REL
        expected_sha = self._sha256(root_helper)
        actual: dict[str, str | None] = {}
        for _, service_dir in WORKERS.items():
            copy_path = ROOT / "services" / service_dir / "scripts" / "linkedin_runtime_lock.py"
            actual[service_dir] = self._sha256(copy_path) if copy_path.is_file() else None

        self.assertEqual(
            {service_dir: expected_sha for service_dir in WORKERS.values()},
            actual,
            "all five worker helper copies must be byte-identical to root helper contract",
        )

    def test_all_worker_run_wrappers_are_thin_lock_wrappers(self) -> None:
        offenders: dict[str, list[str]] = {}
        for _, service_dir in WORKERS.items():
            run_sh = ROOT / "services" / service_dir / "scripts" / "run.sh"
            unlocked_sh = ROOT / "services" / service_dir / "scripts" / "run_unlocked.sh"
            text = run_sh.read_text(encoding="utf-8", errors="ignore") if run_sh.exists() else ""
            problems: list[str] = []
            if not unlocked_sh.is_file():
                problems.append("missing run_unlocked.sh")
            if EXPECTED_CONTAINER_HELPER not in text:
                problems.append("run.sh does not invoke runtime lock helper")
            if EXPECTED_LOCK_DEFAULT not in text:
                problems.append("run.sh does not define shared default lock path")
            if "run_unlocked.sh" not in text:
                problems.append("run.sh does not delegate to run_unlocked.sh")
            if re.search(r"linkedin_(?:worker|outreach|message|like|comment|extractor)\.py", text):
                problems.append("run.sh still contains worker Python business invocation")
            if re.search(r"\bmkdir\s+\$?\{?LINKEDIN_LOCK_DIR|/tmp/[^\s]*\.lock", text):
                problems.append("run.sh still uses old per-container lock")
            if problems:
                offenders[service_dir] = problems

        self.assertEqual({}, offenders)

    def test_production_crontabs_continue_to_call_run_sh_not_unlocked_or_helper_directly(self) -> None:
        offenders: dict[str, str] = {}
        for _, service_dir in WORKERS.items():
            crontab = ROOT / "services" / service_dir / "crontab"
            text = crontab.read_text(encoding="utf-8", errors="ignore")
            if "run_unlocked.sh" in text or "linkedin_runtime_lock.py" in text or "/app/scripts/run.sh" not in text:
                offenders[str(crontab.relative_to(ROOT))] = text

        self.assertEqual({}, offenders, "production crontab files must continue invoking /app/scripts/run.sh")

    def test_compose_mounts_one_named_runtime_volume_into_all_workers(self) -> None:
        volumes = self.compose.get("volumes") or {}
        self.assertIn(EXPECTED_RUNTIME_VOLUME_NAME, volumes)
        mounts: dict[str, str | None] = {}
        for service_name in WORKERS:
            mounts[service_name] = self._named_volume_source_for_target(
                self.services.get(service_name, {}).get("volumes") or [], EXPECTED_RUNTIME_TARGET
            )

        self.assertEqual({service_name: EXPECTED_RUNTIME_VOLUME_NAME for service_name in WORKERS}, mounts)

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _named_volume_source_for_target(volumes: list[Any], target: str) -> str | None:
        for volume in volumes:
            if isinstance(volume, dict):
                if volume.get("type") == "volume" and volume.get("target") == target:
                    source = volume.get("source")
                    return str(source) if source else None
                continue
            parts = str(volume).split(":")
            if len(parts) >= 2 and parts[1] == target:
                source = parts[0]
                if source and not source.startswith((".", "/", "~")):
                    return source
        return None


if __name__ == "__main__":
    unittest.main()
