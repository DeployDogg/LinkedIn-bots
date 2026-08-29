"""Architecture contract tests for the LinkedIn central-browser migration.

These tests are intentionally RED against the pre-migration architecture.  They
perform only static checks: no Docker, no browser startup, no LinkedIn/network
calls.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
WORKER_SERVICES = {
    "jobseeker": "JobSeeker",
    "connectman": "ConnectMan",
    "sendman": "SendMan",
    "postliker": "PostLiker",
    "commentator": "Commentator",
}
CENTRAL_BROWSER_SERVICE = "linkedin-browser"
EXPECTED_CDP_ENDPOINT = "http://linkedin-browser:9222"
EXPECTED_RUNTIME_LOCK_HELPER = "/app/scripts/linkedin_runtime_lock.py"
EXPECTED_RUNTIME_LOCK_ENV = "LINKEDIN_RUNTIME_LOCK_PATH"
EXPECTED_RUNTIME_LOCK_DEFAULT = "/shared/runtime/linkedin-workers.lock"
EXPECTED_RUNTIME_VOLUME_TARGET = "/shared/runtime"


class CentralBrowserArchitectureContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with COMPOSE_PATH.open("r", encoding="utf-8") as fh:
            cls.compose: dict[str, Any] = yaml.safe_load(fh)
        cls.services: dict[str, dict[str, Any]] = cls.compose.get("services", {})

    def test_compose_defines_exactly_one_central_linkedin_browser_service(self) -> None:
        browser_services = [
            name for name in self.services if name == CENTRAL_BROWSER_SERVICE
        ]

        self.assertEqual(
            [CENTRAL_BROWSER_SERVICE],
            browser_services,
            "docker-compose.yml must define exactly one central linkedin-browser service",
        )

    def test_only_central_browser_owns_persistent_profile_volume(self) -> None:
        profile_volume_pattern = re.compile(
            r"linkedin_(?:chromium|browser|chrome)_profile|/ms-playwright-profile|/profile",
            re.IGNORECASE,
        )
        profile_volume_owners: dict[str, list[str]] = {}
        for service_name, service in self.services.items():
            volumes = service.get("volumes") or []
            matching_volumes = [
                str(volume) for volume in volumes if profile_volume_pattern.search(str(volume))
            ]
            if matching_volumes:
                profile_volume_owners[service_name] = matching_volumes

        self.assertEqual(
            {CENTRAL_BROWSER_SERVICE},
            set(profile_volume_owners),
            "only linkedin-browser may mount the persistent Chromium profile volume",
        )

    def test_only_central_browser_contains_login_or_auth_code(self) -> None:
        offenders: dict[str, list[str]] = {}
        for service_name, service_dir in WORKER_SERVICES.items():
            worker_root = ROOT / "services" / service_dir
            matches = []
            for path in worker_root.rglob("*"):
                if "tests" in path.relative_to(worker_root).parts:
                    continue
                if path.is_file() and path.suffix in {".py", ".sh"}:
                    if path.name == "linkedin_auth.py":
                        matches.append(f"{path.relative_to(ROOT)}: dedicated credential-login auth module")
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    for line_number, line in enumerate(text.splitlines(), start=1):
                        if self._is_worker_owned_auth_code(path, line):
                            matches.append(
                                f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}"
                            )
            if matches:
                offenders[service_name] = sorted(matches)

        self.assertEqual(
            {},
            offenders,
            "worker services must not contain login/auth/credential code; only linkedin-browser may own it",
        )

    def test_all_workers_use_central_cdp_endpoint(self) -> None:
        bad_env: dict[str, Any] = {}
        for service_name in WORKER_SERVICES:
            service = self.services.get(service_name, {})
            env = self._environment_dict(service.get("environment"))
            actual = env.get("LINKEDIN_CDP_ENDPOINT")
            if actual != EXPECTED_CDP_ENDPOINT:
                bad_env[service_name] = actual

        self.assertEqual(
            {},
            bad_env,
            f"all workers must set LINKEDIN_CDP_ENDPOINT={EXPECTED_CDP_ENDPOINT}",
        )

    def test_workers_do_not_launch_chromium_or_write_shared_storage_state(self) -> None:
        forbidden_runtime_patterns = re.compile(
            r"(chromium\.(?:launch|launch_persistent_context)\s*\(|storage_state\s*\()"
        )
        offenders: dict[str, list[str]] = {}
        for service_name, service_dir in WORKER_SERVICES.items():
            worker_root = ROOT / "services" / service_dir
            matches = []
            for path in worker_root.rglob("*.py"):
                if "tests" in path.relative_to(worker_root).parts:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if forbidden_runtime_patterns.search(line):
                        matches.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
            if matches:
                offenders[service_name] = matches

        self.assertEqual(
            {},
            offenders,
            "workers must connect over CDP only; they must not launch Chromium or write shared storage_state",
        )

    def test_worker_images_do_not_install_or_point_to_local_chromium_runtime(self) -> None:
        forbidden_image_patterns = re.compile(
            r"\b(?:chromium|chromium-browser|google-chrome|xvfb|xauth)\b|PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
            re.IGNORECASE,
        )
        forbidden_base_image_patterns = re.compile(
            r"^\s*FROM\s+(?:mcr\.microsoft\.com/playwright|.*(?:playwright|browser|chromium|chrome).*)\b",
            re.IGNORECASE,
        )
        self.assertRegex(
            "RUN apt-get install chromium",
            forbidden_image_patterns,
            "forbidden image regex must use real word boundaries and catch Chromium packages",
        )
        self.assertRegex(
            "FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy",
            forbidden_base_image_patterns,
            "forbidden base-image regex must catch browser-bundled Playwright images",
        )
        offenders: dict[str, list[str]] = {}
        for service_name, service_dir in WORKER_SERVICES.items():
            dockerfile = ROOT / "services" / service_dir / "Dockerfile"
            matches = []
            lines = dockerfile.read_text(encoding="utf-8").splitlines()
            from_lines = [line.strip() for line in lines if line.strip().upper().startswith("FROM ")]
            if from_lines != ["FROM python:3.11-slim-bookworm"]:
                matches.append(
                    f"{dockerfile.relative_to(ROOT)}:1: worker images must stay on python:3.11-slim-bookworm, got {from_lines}"
                )
            for line_number, line in enumerate(lines, start=1):
                if forbidden_image_patterns.search(line) or forbidden_base_image_patterns.search(line):
                    matches.append(f"{dockerfile.relative_to(ROOT)}:{line_number}: {line.strip()}")
            if matches:
                offenders[service_name] = matches

        self.assertEqual(
            {},
            offenders,
            "worker images are CDP clients only; only linkedin-browser may install or configure a local Chromium/Xvfb runtime",
        )

    def test_all_worker_wrappers_delegate_through_same_python_runtime_lock_helper(self) -> None:
        wrapper_helpers: dict[str, bool] = {}
        for service_name, service_dir in WORKER_SERVICES.items():
            run_sh = ROOT / "services" / service_dir / "scripts" / "run.sh"
            text = run_sh.read_text(encoding="utf-8", errors="ignore")
            wrapper_helpers[service_name] = EXPECTED_RUNTIME_LOCK_HELPER in text

        self.assertTrue(
            all(wrapper_helpers.values()),
            f"every worker run.sh wrapper must delegate through {EXPECTED_RUNTIME_LOCK_HELPER}: {wrapper_helpers}",
        )

    def test_all_worker_wrappers_use_same_runtime_lock_path_default(self) -> None:
        wrapper_lock_values: dict[str, str | None] = {}
        lock_default_pattern = re.compile(
            rf"{EXPECTED_RUNTIME_LOCK_ENV}[^\n]*:-([^}}\"']+)",
            re.IGNORECASE,
        )
        for service_name, service_dir in WORKER_SERVICES.items():
            run_sh = ROOT / "services" / service_dir / "scripts" / "run.sh"
            text = run_sh.read_text(encoding="utf-8", errors="ignore")
            match = lock_default_pattern.search(text)
            wrapper_lock_values[service_name] = match.group(1).strip() if match else None

        unique_lock_paths = {value for value in wrapper_lock_values.values() if value}
        self.assertEqual(
            {EXPECTED_RUNTIME_LOCK_DEFAULT},
            unique_lock_paths,
            f"all worker run.sh wrappers must use the same default {EXPECTED_RUNTIME_LOCK_ENV} under /shared/runtime",
        )
        self.assertNotIn(
            None,
            set(wrapper_lock_values.values()),
            f"every worker run.sh wrapper must define {EXPECTED_RUNTIME_LOCK_ENV} with a default path",
        )

    def test_compose_mounts_same_named_runtime_volume_for_all_workers(self) -> None:
        runtime_mounts: dict[str, str | None] = {}
        for service_name in WORKER_SERVICES:
            service = self.services.get(service_name, {})
            runtime_mounts[service_name] = self._named_volume_source_for_target(
                service.get("volumes") or [], EXPECTED_RUNTIME_VOLUME_TARGET
            )

        named_sources = {source for source in runtime_mounts.values() if source}
        self.assertEqual(
            1,
            len(named_sources),
            f"all workers must mount the same Docker named runtime volume at {EXPECTED_RUNTIME_VOLUME_TARGET}",
        )
        self.assertNotIn(
            None,
            set(runtime_mounts.values()),
            f"every worker must mount a Docker named runtime volume at {EXPECTED_RUNTIME_VOLUME_TARGET}: {runtime_mounts}",
        )

    def test_central_browser_service_builds_from_expected_directory(self) -> None:
        browser = self.services.get(CENTRAL_BROWSER_SERVICE, {})

        self.assertEqual(
            "./services/LinkedInBrowser",
            browser.get("build"),
            "linkedin-browser must build from services/LinkedInBrowser",
        )
        self.assertEqual(
            "linkedin-browser:local",
            browser.get("image"),
            "linkedin-browser must use a local image tag",
        )
        self.assertEqual(
            "unless-stopped",
            browser.get("restart"),
            "linkedin-browser must restart unless stopped manually",
        )

    def test_central_browser_dockerfile_and_scripts_exist(self) -> None:
        browser_root = ROOT / "services" / "LinkedInBrowser"
        expected_files = [
            browser_root / "Dockerfile",
            browser_root / "entrypoint.sh",
            browser_root / "scripts" / "healthcheck.sh",
        ]

        missing = [str(path.relative_to(ROOT)) for path in expected_files if not path.is_file()]

        self.assertEqual([], missing, "linkedin-browser must include Dockerfile, entrypoint, and healthcheck")

    def test_central_browser_launches_exactly_one_persistent_chromium_under_xvfb(self) -> None:
        entrypoint = ROOT / "services" / "LinkedInBrowser" / "entrypoint.sh"
        self.assertTrue(entrypoint.is_file(), "linkedin-browser entrypoint.sh must exist")
        text = entrypoint.read_text(encoding="utf-8")

        self.assertIn("Xvfb", text, "linkedin-browser entrypoint must start Xvfb for Chromium graphics")
        self.assertEqual(
            1,
            len(re.findall(r"\b(?:chromium|chromium-browser|google-chrome)\b", text)),
            "entrypoint must launch exactly one Chromium process",
        )
        self.assertIn("--user-data-dir=/profile", text)
        self.assertIn("--remote-debugging-address=0.0.0.0", text)
        self.assertIn("--remote-debugging-port=9222", text)

    def test_central_browser_healthcheck_uses_cdp_json_version_and_process_liveness(self) -> None:
        healthcheck = ROOT / "services" / "LinkedInBrowser" / "scripts" / "healthcheck.sh"
        self.assertTrue(healthcheck.is_file(), "linkedin-browser healthcheck.sh must exist")
        text = healthcheck.read_text(encoding="utf-8")

        self.assertIn("/json/version", text)
        self.assertRegex(text, r"pgrep|pidof", "healthcheck must verify Chromium process liveness")

    def test_cdp_port_is_not_published_to_host(self) -> None:
        browser = self.services.get(CENTRAL_BROWSER_SERVICE, {})
        published_ports = [str(port) for port in browser.get("ports") or []]
        cdp_ports = [port for port in published_ports if "9222" in port]

        self.assertEqual(
            [],
            cdp_ports,
            "linkedin-browser CDP port 9222 must stay internal and must not be published to the host",
        )

    def test_novnc_is_bound_to_localhost_only(self) -> None:
        browser = self.services.get(CENTRAL_BROWSER_SERVICE, {})
        published_ports = [str(port) for port in browser.get("ports") or []]
        novnc_ports = [port for port in published_ports if re.search(r"(?:6080|7900)", port)]

        self.assertTrue(
            novnc_ports,
            "linkedin-browser must publish a noVNC port for local inspection",
        )
        self.assertTrue(
            all(port.startswith("127.0.0.1:") for port in novnc_ports),
            f"noVNC ports must bind only to 127.0.0.1, got {novnc_ports}",
        )

    def test_workers_depend_on_healthy_central_browser(self) -> None:
        bad_dependencies: dict[str, Any] = {}
        for service_name in WORKER_SERVICES:
            service = self.services.get(service_name, {})
            depends_on = service.get("depends_on")
            if not self._has_healthy_browser_dependency(depends_on):
                bad_dependencies[service_name] = depends_on

        self.assertEqual(
            {},
            bad_dependencies,
            "each worker must depend_on linkedin-browser with condition: service_healthy",
        )

    @staticmethod
    def _environment_dict(environment: Any) -> dict[str, str]:
        if environment is None:
            return {}
        if isinstance(environment, dict):
            return {str(key): str(value) for key, value in environment.items()}
        if isinstance(environment, list):
            result = {}
            for item in environment:
                key, sep, value = str(item).partition("=")
                if sep:
                    result[key] = value
            return result
        raise TypeError(f"Unsupported compose environment type: {type(environment)!r}")

    @staticmethod
    def _has_healthy_browser_dependency(depends_on: Any) -> bool:
        if isinstance(depends_on, dict):
            browser_dependency = depends_on.get(CENTRAL_BROWSER_SERVICE)
            if isinstance(browser_dependency, dict):
                return browser_dependency.get("condition") == "service_healthy"
            return False
        if isinstance(depends_on, list):
            return False
        return False

    @staticmethod
    def _is_worker_owned_auth_code(path: Path, line: str) -> bool:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return False
        if path.name == "linkedin_auth.py":
            return True
        credential_env_read = re.search(
            r"os\.environ\.(?:get|__getitem__)\(\s*[\"']LINKEDIN_(?:EMAIL|PASSWORD)[\"']",
            stripped,
        )
        explicit_login_api = re.search(r"\.fill\([^\n]*(?:LINKEDIN_EMAIL|LINKEDIN_PASSWORD)", stripped)
        persistent_context = "launch_persistent_context" in stripped
        writes_storage_state = re.search(r"\.storage_state\s*\(\s*path\s*=", stripped)
        return bool(
            credential_env_read
            or explicit_login_api
            or persistent_context
            or writes_storage_state
        )

    @staticmethod
    def _named_volume_source_for_target(volumes: list[Any], target: str) -> str | None:
        for volume in volumes:
            if isinstance(volume, dict):
                if volume.get("target") == target and volume.get("type") == "volume":
                    source = volume.get("source")
                    return str(source) if source else None
                continue

            parts = str(volume).split(":")
            if len(parts) >= 2 and parts[1] == target:
                source = parts[0]
                if source and not source.startswith(('.', '/', '~')):
                    return source
        return None


if __name__ == "__main__":
    unittest.main()
