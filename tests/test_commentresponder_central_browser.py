"""Focused TDD tests for Commentator central Chromium migration.

No Docker, no real browser startup, no LinkedIn/network/actions. Playwright is faked.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
COMMENT_SCRIPTS = ROOT / "services" / "Commentator" / "scripts"
if str(COMMENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(COMMENT_SCRIPTS))


class FakePage:
    def __init__(self, *, body_text: str = "LinkedIn notifications", url: str = "https://www.linkedin.com/notifications/") -> None:
        self.closed = False
        self.url = url
        self.body_text = body_text
        self.goto_urls: list[str] = []
        self.action_attempts: list[str] = []

    def locator(self, selector: str):
        text = self.body_text
        attempts = self.action_attempts

        class Locator:
            def inner_text(self, timeout: int = 0) -> str:
                return text

            def click(self, *args, **kwargs) -> None:
                attempts.append(f"locator.click:{selector}")

        return Locator()

    def goto(self, url: str, **kwargs) -> None:
        self.goto_urls.append(url)
        self.url = url

    def evaluate(self, script: str):
        return []

    def screenshot(self, *args, **kwargs) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, *, page: FakePage | None = None, fail_new_page: bool = False) -> None:
        self.closed = False
        self.fail_new_page = fail_new_page
        self.pages: list[FakePage] = []
        self._page = page

    def new_page(self) -> FakePage:
        if self.fail_new_page:
            raise RuntimeError("new_page boom")
        page = self._page or FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:  # must never be called by Commentator runtime
        self.closed = True

    def storage_state(self, *args, **kwargs) -> None:  # must never be called
        raise AssertionError("storage_state must not be used")


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts
        self.closed = False

    def close(self) -> None:  # must never be called by Commentator runtime
        self.closed = True

    def new_context(self, *args, **kwargs):  # must never be called
        raise AssertionError("browser.new_context must not be used")


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.connected_endpoints: list[str] = []

    def connect_over_cdp(self, endpoint: str, **kwargs) -> FakeBrowser:
        self.connected_endpoints.append(endpoint)
        return self.browser

    def launch(self, *args, **kwargs):  # must never be called
        raise AssertionError("chromium.launch must not be used")

    def launch_persistent_context(self, *args, **kwargs):  # must never be called
        raise AssertionError("launch_persistent_context must not be used")


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)


class FakeSyncPlaywright:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    def __enter__(self) -> FakePlaywright:
        return self.playwright

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def import_commentator():
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: None
    fake_sync_api.TimeoutError = TimeoutError
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    with patch.dict(sys.modules, {"playwright": fake_playwright, "playwright.sync_api": fake_sync_api}):
        sys.modules.pop("linkedin_commentator", None)
        return importlib.import_module("linkedin_commentator")


class CommentatorCentralBrowserUnitTest(unittest.TestCase):
    def test_uses_linkedin_cdp_endpoint_and_existing_default_context(self) -> None:
        context = FakeContext()
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)
        runtime = import_commentator()

        with patch.dict(runtime.os.environ, {"LINKEDIN_CDP_ENDPOINT": "http://central.test:9222"}, clear=False):
            browser_out, context_out, page_out, err = runtime.connect_browser(playwright)

        self.assertIsNone(err)
        self.assertEqual(["http://central.test:9222"], playwright.chromium.connected_endpoints)
        self.assertIs(browser, browser_out)
        self.assertIs(context, context_out)
        self.assertEqual(1, len(context.pages))
        self.assertIs(context.pages[0], page_out)

    def test_default_cdp_endpoint_is_linkedin_browser_9222(self) -> None:
        context = FakeContext()
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)
        runtime = import_commentator()

        with patch.dict(runtime.os.environ, {}, clear=True):
            runtime.connect_browser(playwright)

        self.assertEqual(["http://linkedin-browser:9222"], playwright.chromium.connected_endpoints)

    def test_fail_closed_on_zero_or_multiple_contexts(self) -> None:
        runtime = import_commentator()
        for contexts in ([], [FakeContext(), FakeContext()]):
            with self.subTest(context_count=len(contexts)):
                browser = FakeBrowser(contexts)
                playwright = FakePlaywright(browser)
                browser_out, context_out, page_out, err = runtime.connect_browser(playwright)
                self.assertIs(browser, browser_out)
                self.assertIsNone(context_out)
                self.assertIsNone(page_out)
                self.assertIn("expected_exactly_one_persistent_context", err)
                self.assertFalse(browser.closed)
                self.assertTrue(all(not ctx.closed for ctx in contexts))

    def test_close_commentator_page_closes_page_only_on_exception(self) -> None:
        page = FakePage()
        context = FakeContext(page=page)
        browser = FakeBrowser([context])
        runtime = import_commentator()

        with self.assertRaisesRegex(RuntimeError, "body boom"):
            try:
                raise RuntimeError("body boom")
            finally:
                runtime.close_commentator_page(page)

        self.assertTrue(page.closed)
        self.assertFalse(context.closed, "shared persistent context must stay open")
        self.assertFalse(browser.closed, "shared CDP browser must stay open")

    def test_blocker_detection_covers_authwall_login_security_and_limits(self) -> None:
        runtime = import_commentator()
        cases = [
            (FakePage(body_text="Please sign in to continue"), "sign in"),
            (FakePage(body_text="Authwall blocks this page"), "authwall"),
            (FakePage(body_text="Security verification required"), "security verification"),
            (FakePage(body_text="You hit a rate limit"), "rate limit"),
            (FakePage(body_text="daily-limit safeguard reached"), "daily-limit"),
            (FakePage(body_text="ok", url="https://www.linkedin.com/login"), "url:https://www.linkedin.com/login"),
            (FakePage(body_text="ok", url="https://www.linkedin.com/checkpoint/challenge/foo"), "url:https://www.linkedin.com/checkpoint/challenge/foo"),
        ]
        for page, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, runtime.detect_stop(page))

    def test_run_dry_run_never_attempts_reply_or_submit_and_keeps_shared_browser_open(self) -> None:
        runtime = import_commentator()
        owned_page = FakePage()
        context = FakeContext(page=owned_page)
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)
        args = argparse.Namespace(dry_run=True, max_items=1, lookback_days=1, no_delay=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with patch.object(runtime, "STATE_DIR", tmp), \
                 patch.object(runtime, "STATUS_PATH", tmp / "status.json"), \
                 patch.object(runtime, "DRAFTS_PATH", tmp / "drafts.md"), \
                 patch.object(runtime, "SCREENSHOT_DIR", tmp / "shots"), \
                 patch.object(runtime, "sync_playwright", lambda: FakeSyncPlaywright(playwright)):
                status = runtime.run(args)

        self.assertEqual([], owned_page.action_attempts)
        self.assertEqual(1, len(context.pages))
        self.assertTrue(owned_page.closed)
        self.assertFalse(context.closed)
        self.assertFalse(browser.closed)
        self.assertTrue(status["dry_run"])

    def test_main_returns_nonzero_for_context_error(self) -> None:
        runtime = import_commentator()
        context = FakeContext(fail_new_page=True)
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with patch.object(runtime, "STATE_DIR", tmp), \
                 patch.object(runtime, "STATUS_PATH", tmp / "status.json"), \
                 patch.object(runtime, "DRAFTS_PATH", tmp / "drafts.md"), \
                 patch.object(runtime, "SCREENSHOT_DIR", tmp / "shots"), \
                 patch.object(runtime, "sync_playwright", lambda: FakeSyncPlaywright(playwright)), \
                 patch.object(runtime.sys, "argv", ["linkedin_commentator.py", "--dry-run", "--no-delay", "--max-items", "1"]):
                code = runtime.main()

        self.assertNotEqual(0, code)
        self.assertFalse(browser.closed)
        self.assertFalse(context.closed)


class CommentatorCentralBrowserStaticTest(unittest.TestCase):
    TARGETS = [COMMENT_SCRIPTS / "linkedin_commentator.py"]

    def test_commentator_creates_exactly_one_owned_page(self) -> None:
        text = (COMMENT_SCRIPTS / "linkedin_commentator.py").read_text(encoding="utf-8")
        self.assertEqual(
            1,
            text.count("context.new_page()"),
            "Commentator must create one owned page from the shared persistent context",
        )

    def test_commentator_python_uses_cdp_not_forbidden_browser_apis_storage_or_credentials(self) -> None:
        forbidden = {
            "chromium.launch(": "must not launch private Chromium",
            "launch_persistent_context(": "must not create persistent browser profile",
            "browser.new_context(": "must not create an isolated context",
            ".new_context(": "must not create an isolated context",
            "storage_state": "must not read/write storage_state or depend on session json",
            "linkedin_session.json": "must not depend on legacy session file",
            "LINKEDIN_SESSION_PATH": "must not depend on legacy session file env",
            "LINKEDIN_CHROMIUM_PROFILE_DIR": "must not depend on worker-owned browser profile",
            "LINKEDIN_EMAIL": "must not read credentials",
            "LINKEDIN_PASSWORD": "must not read credentials",
        }
        offenders: list[str] = []
        for path in self.TARGETS:
            text = path.read_text(encoding="utf-8")
            self.assertIn("connect_over_cdp", text, f"{path.name} must connect to central Chromium over CDP")
            for needle, reason in forbidden.items():
                if needle in text:
                    offenders.append(f"{path.relative_to(ROOT)} contains {needle!r}: {reason}")
        self.assertEqual([], offenders)

    def test_run_unlocked_has_no_xvfb(self) -> None:
        text = (COMMENT_SCRIPTS / "run_unlocked.sh").read_text(encoding="utf-8")
        self.assertNotIn("xvfb-run", text)
        self.assertIn("python3 linkedin_commentator.py", text)


if __name__ == "__main__":
    unittest.main()
