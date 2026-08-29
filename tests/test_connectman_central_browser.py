"""Focused TDD tests for ConnectMan central Chromium migration.

No Docker, no browser startup, no LinkedIn/network calls. Playwright is faked.
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CONNECTMAN_SCRIPTS = ROOT / "services" / "ConnectMan" / "scripts"
if str(CONNECTMAN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CONNECTMAN_SCRIPTS))


class FakePage:
    def __init__(self, *, body_text: str = "LinkedIn people search", url: str = "https://www.linkedin.com/search/results/people/") -> None:
        self.closed = False
        self.url = url
        self.body_text = body_text

    def locator(self, selector: str):
        text = self.body_text

        class Body:
            def inner_text(self, timeout: int = 0) -> str:
                return text

        return Body()

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

    def close(self) -> None:  # must never be called by ConnectMan runtime
        self.closed = True

    def storage_state(self, *args, **kwargs) -> None:  # must never be called
        raise AssertionError("storage_state must not be used")


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts
        self.closed = False

    def close(self) -> None:  # must never be called by ConnectMan runtime
        self.closed = True

    def new_context(self, *args, **kwargs):  # must never be called
        raise AssertionError("browser new_context must not be used")


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.connected_endpoints: list[str] = []

    def connect_over_cdp(self, endpoint: str) -> FakeBrowser:
        self.connected_endpoints.append(endpoint)
        return self.browser

    def launch(self, *args, **kwargs):  # must never be called
        raise AssertionError("chromium launch must not be used")

    def launch_persistent_context(self, *args, **kwargs):  # must never be called
        raise AssertionError("launch_persistent_context must not be used")


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)


def import_connectman():
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: None
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    with patch.dict(sys.modules, {"playwright": fake_playwright, "playwright.sync_api": fake_sync_api}):
        sys.modules.pop("linkedin_outreach", None)
        return importlib.import_module("linkedin_outreach")


class ConnectManCentralBrowserUnitTest(unittest.TestCase):
    def test_uses_linkedin_cdp_endpoint_and_existing_default_context(self) -> None:
        context = FakeContext()
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)
        runtime = import_connectman()

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
        runtime = import_connectman()

        with patch.dict(runtime.os.environ, {}, clear=True):
            runtime.connect_browser(playwright)

        self.assertEqual(["http://linkedin-browser:9222"], playwright.chromium.connected_endpoints)

    def test_fail_closed_on_zero_or_multiple_contexts(self) -> None:
        runtime = import_connectman()
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

    def test_close_connectman_page_closes_page_only_on_exception(self) -> None:
        page = FakePage()
        context = FakeContext(page=page)
        browser = FakeBrowser([context])
        runtime = import_connectman()

        with self.assertRaisesRegex(RuntimeError, "body boom"):
            try:
                raise RuntimeError("body boom")
            finally:
                runtime.close_connectman_page(page)

        self.assertTrue(page.closed)
        self.assertFalse(context.closed, "shared persistent context must stay open")
        self.assertFalse(browser.closed, "shared CDP browser must stay open")

    def test_blocker_detection_covers_authwall_security_and_rate_limit(self) -> None:
        runtime = import_connectman()
        cases = [
            (FakePage(body_text="Please sign in to continue"), "sign in"),
            (FakePage(body_text="Authwall blocks this page"), "authwall"),
            (FakePage(body_text="Security verification required"), "security verification"),
            (FakePage(body_text="You hit a rate limit"), "rate limit"),
            (FakePage(body_text="ok", url="https://www.linkedin.com/checkpoint/challenge/foo"), "url:https://www.linkedin.com/checkpoint/challenge/foo"),
        ]
        for page, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, runtime.detect_stop(page))


class ConnectManCentralBrowserStaticTest(unittest.TestCase):
    TARGETS = [CONNECTMAN_SCRIPTS / "linkedin_outreach.py"]

    def test_connectman_creates_exactly_one_owned_page(self) -> None:
        text = (CONNECTMAN_SCRIPTS / "linkedin_outreach.py").read_text(encoding="utf-8")
        self.assertEqual(
            1,
            text.count("context.new_page()"),
            "ConnectMan must create one owned page from the shared persistent context",
        )

    def test_connectman_python_uses_cdp_not_forbidden_browser_apis_or_storage_state(self) -> None:
        forbidden = {
            "LINKEDIN_CONNECT_USE_CDP": "CDP must be mandatory, not optional",
            "chromium.launch(": "must not launch private Chromium",
            "launch_persistent_context(": "must not create persistent browser profile",
            "browser.new_context(": "must not create an isolated context",
            ".new_context(": "must not create an isolated context",
            "storage_state": "must not read/write storage_state or depend on session json",
            "linkedin_session.json": "must not depend on legacy session file",
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
        text = (CONNECTMAN_SCRIPTS / "run_unlocked.sh").read_text(encoding="utf-8")
        self.assertNotIn("xvfb-run", text)
        self.assertIn("python3 linkedin_outreach.py", text)


if __name__ == "__main__":
    unittest.main()
