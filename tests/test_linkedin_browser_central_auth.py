"""Focused TDD tests for linkedin-browser-owned auth snapshot export.

No Docker, no real browser startup, no LinkedIn/network calls. Playwright is faked.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
BROWSER_ROOT = ROOT / "services" / "LinkedInBrowser"
CENTRAL_AUTH_PATH = BROWSER_ROOT / "scripts" / "central_auth.py"
COMPOSE_PATH = ROOT / "docker-compose.yml"
SESSION_BACKUP_TARGET = "/session-backup"
SESSION_BACKUP_FILE = "linkedin_session.json"


def import_central_auth():
    sys.modules.pop("central_auth", None)
    spec = importlib.util.spec_from_file_location("central_auth", CENTRAL_AUTH_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["central_auth"] = module
    spec.loader.exec_module(module)
    return module


class FakePage:
    def __init__(self, *, url: str = "https://www.linkedin.com/feed/", body: str = "LinkedIn Feed") -> None:
        self.url = url
        self.body = body
        self.closed = False
        self.goto_calls: list[tuple[str, dict]] = []

    def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append((url, kwargs))
        self.url = url

    def wait_for_load_state(self, *args, **kwargs) -> None:
        return None

    def locator(self, selector: str):
        page = self

        class Locator:
            def inner_text(self, timeout: int = 0) -> str:
                return page.body

        return Locator()

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, pages: list[FakePage] | None = None, *, next_page: FakePage | None = None) -> None:
        self.pages = pages or []
        self.next_page = next_page
        self.storage_state_paths: list[str] = []
        self.closed = False

    def new_page(self) -> FakePage:
        page = self.next_page or FakePage()
        self.pages.append(page)
        return page

    def storage_state(self, *, path: str) -> None:
        self.storage_state_paths.append(path)
        Path(path).write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.connected_endpoints: list[str] = []

    def connect_over_cdp(self, endpoint: str) -> FakeBrowser:
        self.connected_endpoints.append(endpoint)
        return self.browser

    def launch(self, *args, **kwargs):
        raise AssertionError("central_auth must not launch Chromium")

    def launch_persistent_context(self, *args, **kwargs):
        raise AssertionError("central_auth must not launch another persistent Chromium")


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)


class CentralAuthUnitTests(unittest.TestCase):
    def test_success_connects_to_local_cdp_requires_one_context_exports_atomically_and_closes_owned_page_only(self) -> None:
        runtime = import_central_auth()
        existing = FakePage(url="https://www.linkedin.com/feed/", body="LinkedIn Feed")
        context = FakeContext([existing])
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)

        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / SESSION_BACKUP_FILE
            with patch.dict(
                os.environ,
                {
                    "LINKEDIN_CDP_ENDPOINT": "http://evil.example:9222",
                    "LINKEDIN_FEED_URL": "https://www.linkedin.com/feed/",
                    "LINKEDIN_SESSION_BACKUP_PATH": str(backup),
                },
                clear=False,
            ):
                code = runtime.run(playwright)

            self.assertEqual(0, code)
            self.assertEqual(["http://127.0.0.1:9222"], playwright.chromium.connected_endpoints)
            self.assertEqual([str(Path(tmp) / f".{SESSION_BACKUP_FILE}.tmp")], context.storage_state_paths)
            self.assertTrue(backup.exists())
            self.assertFalse((Path(tmp) / f".{SESSION_BACKUP_FILE}.tmp").exists())
            self.assertFalse(existing.closed, "pre-existing persistent-context pages must remain open")
            self.assertTrue(context.pages[-1].closed, "central_auth must close only its owned page")
            self.assertFalse(context.closed)
            self.assertFalse(browser.closed)

    def test_fail_closed_when_context_count_is_not_exactly_one(self) -> None:
        runtime = import_central_auth()
        for contexts in ([], [FakeContext(), FakeContext()]):
            with self.subTest(context_count=len(contexts)):
                browser = FakeBrowser(contexts)
                playwright = FakePlaywright(browser)
                code = runtime.run(playwright)
                self.assertEqual(12, code)
                self.assertFalse(browser.closed)
                self.assertTrue(all(not ctx.closed for ctx in contexts))

    def test_authwall_or_login_exits_11_and_does_not_export(self) -> None:
        runtime = import_central_auth()
        context = FakeContext([FakePage()], next_page=FakePage(body="Sign in Join now"))
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"LINKEDIN_SESSION_BACKUP_PATH": str(Path(tmp) / SESSION_BACKUP_FILE)},
            clear=False,
        ):
            code = runtime.run(playwright)

        self.assertEqual(11, code)
        self.assertEqual([], context.storage_state_paths)
        self.assertTrue(context.pages[-1].closed)

    def test_generic_security_footer_word_on_feed_is_not_a_security_blocker(self) -> None:
        runtime = import_central_auth()

        code = runtime.classify_page(
            "https://www.linkedin.com/feed/",
            "LinkedIn Feed\nSecurity\nPrivacy & Terms\nBusiness Services",
        )

        self.assertEqual(0, code)

    def test_checkpoint_captcha_security_rate_limit_patterns_exit_12_and_do_not_export(self) -> None:
        runtime = import_central_auth()
        blockers = [
            "captcha verification required",
            "checkpoint challenge",
            "security verification",
            "rate-limit safeguard limit reached",
        ]
        for body in blockers:
            with self.subTest(body=body):
                context = FakeContext([FakePage()], next_page=FakePage(body=body))
                browser = FakeBrowser([context])
                playwright = FakePlaywright(browser)
                code = runtime.run(playwright)
                self.assertEqual(12, code)
                self.assertEqual([], context.storage_state_paths)
                self.assertTrue(context.pages[-1].closed)


class CentralAuthStaticOwnerTests(unittest.TestCase):
    def test_only_linkedin_browser_central_auth_writes_playwright_storage_state_path(self) -> None:
        offenders: list[str] = []
        pattern = re.compile(r"\.storage_state\s*\(\s*path\s*=")
        runtime_roots = [
            ROOT / "services" / service
            for service in ("JobSeeker", "ConnectMan", "SendMan", "PostLiker", "Commentator", "LinkedInBrowser")
        ]
        for runtime_root in runtime_roots:
            for path in runtime_root.rglob("*.py"):
                if "tests" in path.relative_to(runtime_root).parts:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if pattern.search(text) and path.resolve() != CENTRAL_AUTH_PATH.resolve():
                    offenders.append(str(path.relative_to(ROOT)))

        self.assertEqual([], sorted(offenders))

    def test_out_of_scope_hirehi_and_shared_legacy_storage_state_behavior_is_untouched(self) -> None:
        telegram_login = (ROOT / "hirehi" / "telegram_persistent_login.py").read_text(encoding="utf-8")
        self.assertIn("await context.storage_state(path=str(STORAGE_STATE))", telegram_login)
        self.assertIn('print("LOGIN_OK already_authenticated", flush=True)', telegram_login)
        self.assertIn('print(f"LOGIN_OK storage_state={STORAGE_STATE}", flush=True)', telegram_login)

        telegram_preflight = (ROOT / "hirehi" / "telegram_preflight_hirehi.py").read_text(encoding="utf-8")
        self.assertIn("ctx.storage_state(path=str(STORAGE))", telegram_preflight)

        legacy_worker = (ROOT / "shared" / "legacy_state" / "linkedin_worker.py").read_text(encoding="utf-8")
        self.assertIn("existing Playwright session produced by linkedin_auth.py", legacy_worker)
        self.assertIn("session/login problem; run linkedin_auth.py.", legacy_worker)
        self.assertIn('Run linkedin_auth.py first.', legacy_worker)
        self.assertIn("context.storage_state(path=str(SESSION_PATH))", legacy_worker)

        legacy_like_posts = (ROOT / "shared" / "legacy_state" / "linkedin_like_posts.py").read_text(encoding="utf-8")
        self.assertIn("context.storage_state(path=str(SESSION_PATH))", legacy_like_posts)
    def test_jobseeker_auth_helper_removed_and_safe_mode_compile_no_longer_references_it(self) -> None:
        self.assertFalse((ROOT / "services" / "JobSeeker" / "scripts" / "linkedin_auth.py").exists())
        run_unlocked = (ROOT / "services" / "JobSeeker" / "scripts" / "run_unlocked.sh").read_text(encoding="utf-8")
        safe_mode_block = run_unlocked.split("if [ \"${SAFE_MODE:-0}\" = \"1\" ]; then", 1)[1].split("fi", 1)[0]
        self.assertNotIn("linkedin_auth.py", safe_mode_block)
        self.assertIn("linkedin_central_browser.py", safe_mode_block)
        self.assertIn("linkedin_extractor.py", safe_mode_block)
        self.assertIn("linkedin_worker.py", safe_mode_block)

    def test_browser_compose_mounts_session_backup_only_on_linkedin_browser(self) -> None:
        compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        services = compose["services"]
        owners = []
        for name, service in services.items():
            for volume in service.get("volumes") or []:
                if SESSION_BACKUP_TARGET in str(volume):
                    owners.append(name)
        self.assertEqual(["linkedin-browser"], owners)
        self.assertTrue(
            any("./shared/browser_session_backup:/session-backup" == str(v) for v in services["linkedin-browser"].get("volumes") or [])
        )

    def test_browser_image_has_python_playwright_client_without_installing_second_chromium(self) -> None:
        dockerfile = (BROWSER_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"python3|python3-pip")
        self.assertIn("playwright", dockerfile)
        self.assertIn("cdp_proxy.py", dockerfile)
        self.assertNotIn("socat", dockerfile)
        self.assertIn("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1", dockerfile)
        self.assertNotIn("playwright install", dockerfile)

    def test_browser_entrypoint_forwards_cdp_to_compose_peers_without_changing_browser_port(self) -> None:
        entrypoint = (BROWSER_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("cdp_proxy.py", entrypoint)
        self.assertIn("CDP_PROXY_LISTEN_HOST=\"$BROWSER_CONTAINER_IP\"", entrypoint)
        self.assertIn("CDP_PROXY_UPSTREAM_HOST=127.0.0.1", entrypoint)
        self.assertIn("--remote-debugging-port=9222", entrypoint)
        self.assertIn("--remote-allow-origins=*", entrypoint)
        self.assertNotIn("socat", entrypoint)

    def test_browser_entrypoint_opens_configurable_linkedin_feed_by_default(self) -> None:
        entrypoint = (BROWSER_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("LINKEDIN_BROWSER_START_URL", entrypoint)
        self.assertIn("https://www.linkedin.com/feed/", entrypoint)
        self.assertNotIn("about:blank", entrypoint)

    def test_readme_documents_manual_browser_owned_auth_flow(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:6080/vnc.html", readme)
        self.assertIn("docker compose exec linkedin-browser python3 /app/scripts/central_auth.py", readme)
        self.assertIn("persistent Chromium", readme)
        self.assertNotRegex(readme, r"LINKEDIN_(EMAIL|PASSWORD)")


if __name__ == "__main__":
    unittest.main()
