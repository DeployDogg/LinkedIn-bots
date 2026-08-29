"""Worker env-file isolation tests.

Static/subprocess checks only: no Docker start, no browser, no LinkedIn calls.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "generate_worker_env.py"
COMPOSE_PATH = ROOT / "docker-compose.yml"
WORKER_SERVICES = {
    "jobseeker",
    "connectman",
    "sendman",
    "postliker",
    "commentator",
}
CENTRAL_BROWSER_SERVICE = "linkedin-browser"
WORKER_ENV_FILE = ".env.workers"
EXPECTED_CDP_ENDPOINT = "http://linkedin-browser:9222"
EXCLUDED_KEYS = {
    "LINKEDIN_EMAIL",
    "LINKEDIN_PASSWORD",
    "LINKEDIN_SESSION_PATH",
    "LINKEDIN_CHROMIUM_PROFILE_DIR",
    "LINKEDIN_CONNECT_USE_CDP",
    "LINKEDIN_LOGIN_URL",
    "LINKEDIN_CDP_ENDPOINT",
}


class WorkerEnvGeneratorTest(unittest.TestCase):
    def _load_generator(self):
        spec = importlib.util.spec_from_file_location("generate_worker_env", SCRIPT_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module

    def test_generator_filters_exact_keys_preserves_comments_order_values_and_writes_0600(self) -> None:
        module = self._load_generator()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / ".env"
            target = tmp_path / ".env.workers"
            source.write_text(
                "# header\n"
                "TZ=America/Argentina/Buenos_Aires\n"
                "LINKEDIN_EMAIL=secret@example.test\n"
                "LINKEDIN_PASSWORD=secret-password\n"
                "LINKEDIN_SESSION_PATH=/secret/session.json\n"
                "LINKEDIN_CHROMIUM_PROFILE_DIR=/secret/profile\n"
                "LINKEDIN_CONNECT_USE_CDP=0\n"
                "LINKEDIN_LOGIN_URL=https://www.linkedin.com/login\n"
                "LINKEDIN_CDP_ENDPOINT=http://localhost:9222\n"
                "LINKEDIN_EMAIL_BACKUP=must-stay\n"
                "QUOTED='keeps # literal value'\n"
                "\n"
                "SAFE_MODE=0\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = module.generate_worker_env(source, target)

            output = stdout.getvalue()
            rendered = target.read_text(encoding="utf-8")
            self.assertEqual(
                "# header\n"
                "TZ=America/Argentina/Buenos_Aires\n"
                "LINKEDIN_EMAIL_BACKUP=must-stay\n"
                "QUOTED='keeps # literal value'\n"
                "\n"
                "SAFE_MODE=0\n",
                rendered,
            )
            self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))
            self.assertEqual(11, result.total_keys)
            self.assertEqual(len(EXCLUDED_KEYS), result.excluded_count)
            self.assertEqual(EXCLUDED_KEYS, set(result.excluded_keys))
            self.assertIn(str(target), output)
            self.assertIn("total_keys=11", output)
            self.assertIn("excluded_count=7", output)
            for key in sorted(EXCLUDED_KEYS):
                self.assertIn(key, output)
            self.assertNotIn("secret@example.test", output)
            self.assertNotIn("secret-password", output)
            self.assertNotIn("http://localhost:9222", output)

    def test_generator_replaces_existing_target_atomically_without_leaving_temp_file(self) -> None:
        module = self._load_generator()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / ".env"
            target = tmp_path / ".env.workers"
            target.write_text("OLD=1\n", encoding="utf-8")
            os.chmod(target, 0o644)
            source.write_text("A=1\nLINKEDIN_PASSWORD=redacted\nB=2\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                module.generate_worker_env(source, target)

            self.assertEqual("A=1\nB=2\n", target.read_text(encoding="utf-8"))
            self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))
            leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".env.workers.")]
            self.assertEqual([], leftovers)


class WorkerComposeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw_compose: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        cls.raw_services: dict[str, dict[str, Any]] = cls.raw_compose.get("services", {})
        completed = subprocess.run(
            ["docker", "compose", "config"],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        cls.config: dict[str, Any] = yaml.safe_load(completed.stdout)
        cls.services: dict[str, dict[str, Any]] = cls.config.get("services", {})

    def test_all_workers_use_worker_env_file_and_browser_has_no_env_file(self) -> None:
        offenders: dict[str, Any] = {}
        for service_name in WORKER_SERVICES:
            env_file = self.raw_services.get(service_name, {}).get("env_file")
            if env_file != WORKER_ENV_FILE:
                offenders[service_name] = env_file
        browser_env_file = self.raw_services.get(CENTRAL_BROWSER_SERVICE, {}).get("env_file")

        self.assertEqual({}, offenders)
        self.assertIn(browser_env_file, (None, []), "browser must not receive operator .env or worker env_file")

    def test_no_worker_or_browser_environment_contains_credentials_or_excluded_legacy_keys(self) -> None:
        offenders: dict[str, list[str]] = {}
        for service_name in WORKER_SERVICES | {CENTRAL_BROWSER_SERVICE}:
            env = self.services.get(service_name, {}).get("environment") or {}
            leaked = sorted(key for key in EXCLUDED_KEYS if key in env)
            if service_name in WORKER_SERVICES:
                leaked = [key for key in leaked if key != "LINKEDIN_CDP_ENDPOINT"]
            if leaked:
                offenders[service_name] = leaked

        self.assertEqual({}, offenders)

    def test_workers_have_internal_cdp_endpoint_and_healthy_browser_dependency(self) -> None:
        offenders: dict[str, Any] = {}
        for service_name in WORKER_SERVICES:
            service = self.services.get(service_name, {})
            env = service.get("environment") or {}
            depends_on = service.get("depends_on") or {}
            browser_dep = depends_on.get(CENTRAL_BROWSER_SERVICE) if isinstance(depends_on, dict) else None
            if env.get("LINKEDIN_CDP_ENDPOINT") != EXPECTED_CDP_ENDPOINT or browser_dep.get("condition") != "service_healthy":
                offenders[service_name] = {
                    "cdp": env.get("LINKEDIN_CDP_ENDPOINT"),
                    "depends_on": browser_dep,
                }

        self.assertEqual({}, offenders)

    def test_only_browser_mounts_profile_and_session_backup_cdp_not_published_novnc_localhost(self) -> None:
        profile_or_backup_mount_owners: dict[str, list[str]] = {}
        for service_name, service in self.services.items():
            mounts = [str(volume) for volume in service.get("volumes") or []]
            matches = [volume for volume in mounts if "/profile" in volume or "/session-backup" in volume]
            if matches:
                profile_or_backup_mount_owners[service_name] = matches

        browser = self.services.get(CENTRAL_BROWSER_SERVICE, {})
        ports = browser.get("ports") or []
        cdp_ports = [port for port in ports if "9222" in str(port) or self._port_target(port) == 9222]
        novnc_ports = [port for port in ports if "6080" in str(port) or "7900" in str(port) or self._port_target(port) in {6080, 7900}]

        self.assertEqual({CENTRAL_BROWSER_SERVICE}, set(profile_or_backup_mount_owners))
        self.assertEqual([], cdp_ports)
        self.assertTrue(novnc_ports)
        self.assertTrue(all(self._port_host_ip(port) == "127.0.0.1" for port in novnc_ports), novnc_ports)

    def test_gitignore_excludes_operator_env_worker_env_and_migration_backups(self) -> None:
        gitignore = ROOT / ".gitignore"
        self.assertTrue(gitignore.is_file())
        lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
        self.assertTrue({".env", WORKER_ENV_FILE, ".migration-backups/"}.issubset(lines))

    def test_commentator_only_mounts_stats_bot_env_read_only_at_default_path(self) -> None:
        expected_target = "/Users/deploydog-ai/LinkedIn/.hermes_stats_bot.env"
        expected_source = str(ROOT / ".hermes_stats_bot.env")
        owners: dict[str, list[Any]] = {}
        for service_name, service in self.services.items():
            matches = []
            for mount in service.get("volumes") or []:
                if not isinstance(mount, dict):
                    continue
                source = str(mount.get("source") or "")
                target = str(mount.get("target") or "")
                if ".hermes_stats_bot.env" in source or ".hermes_stats_bot.env" in target:
                    matches.append(mount)
            if matches:
                owners[service_name] = matches

        self.assertEqual({"commentator"}, set(owners), owners)
        [mount] = owners["commentator"]
        self.assertEqual("bind", mount.get("type"))
        self.assertEqual(expected_source, mount.get("source"))
        self.assertEqual(expected_target, mount.get("target"))
        self.assertTrue(mount.get("read_only"), mount)

    def test_sendman_authentication_check_is_not_duplicated(self) -> None:
        sendman = ROOT / "services" / "SendMan" / "scripts" / "linkedin_message_outreach.py"
        text = sendman.read_text(encoding="utf-8")
        self.assertNotIn(
            "if not is_authenticated(page):\n        if not is_authenticated(page):",
            text,
        )

    @staticmethod
    def _port_target(port: Any) -> int | None:
        if isinstance(port, dict):
            target = port.get("target")
            return int(target) if target is not None else None
        parts = str(port).split(":")
        try:
            return int(parts[-1].split("/")[0])
        except ValueError:
            return None

    @staticmethod
    def _port_host_ip(port: Any) -> str | None:
        if isinstance(port, dict):
            return port.get("host_ip")
        parts = str(port).split(":")
        return parts[0] if len(parts) >= 3 else None


if __name__ == "__main__":
    unittest.main()
