"""Focused TDD tests for JobSeeker central Chromium usage.

No Docker, no browser startup, no LinkedIn/network calls. Playwright is faked.
"""

from __future__ import annotations

import importlib
import os
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
JOBSEEKER_SCRIPTS = ROOT / "services" / "JobSeeker" / "scripts"
if str(JOBSEEKER_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(JOBSEEKER_SCRIPTS))


class FakePage:
    def __init__(self, *, fail_goto: bool = False) -> None:
        self.closed = False
        self.fail_goto = fail_goto
        self.url = "https://www.linkedin.com/feed/"

    def goto(self, url: str, **kwargs) -> None:
        if self.fail_goto:
            raise RuntimeError("boom")
        self.url = url

    def locator(self, selector: str):
        class Body:
            def inner_text(self, timeout: int = 0) -> str:
                return "LinkedIn home"

        return Body()

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, *, fail_new_page: bool = False, fail_goto: bool = False) -> None:
        self.closed = False
        self.fail_new_page = fail_new_page
        self.fail_goto = fail_goto
        self.pages: list[FakePage] = []

    def new_page(self) -> FakePage:
        if self.fail_new_page:
            raise RuntimeError("new_page boom")
        page = FakePage(fail_goto=self.fail_goto)
        self.pages.append(page)
        return page

    def close(self) -> None:  # must never be called by JobSeeker runtime
        self.closed = True

    def storage_state(self, *args, **kwargs) -> None:  # must never be called
        raise AssertionError("storage_state must not be used")


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts
        self.closed = False

    def close(self) -> None:  # must never be called by JobSeeker runtime
        self.closed = True

    def new_context(self, *args, **kwargs):  # must never be called
        raise AssertionError("browser.new_context must not be used")


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.connected_endpoints: list[str] = []

    def connect_over_cdp(self, endpoint: str) -> FakeBrowser:
        self.connected_endpoints.append(endpoint)
        return self.browser

    def launch(self, *args, **kwargs):  # must never be called
        raise AssertionError("chromium.launch must not be used")

    def launch_persistent_context(self, *args, **kwargs):  # must never be called
        raise AssertionError("launch_persistent_context must not be used")


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)


class JobSeekerCentralBrowserUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        sys.modules.pop("linkedin_central_browser", None)

    def test_uses_linkedin_cdp_endpoint_and_existing_default_context(self) -> None:
        context = FakeContext()
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)

        with patch.dict(os.environ, {"LINKEDIN_CDP_ENDPOINT": "http://central.test:9222"}, clear=False):
            runtime = importlib.import_module("linkedin_central_browser")
            lease = runtime.open_central_page(playwright)

        self.assertEqual(["http://central.test:9222"], playwright.chromium.connected_endpoints)
        self.assertIs(browser, lease.browser)
        self.assertIs(context, lease.context)
        self.assertEqual(1, len(context.pages))
        self.assertIs(context.pages[0], lease.page)

    def test_default_cdp_endpoint_is_linkedin_browser_9222(self) -> None:
        context = FakeContext()
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)

        with patch.dict(os.environ, {}, clear=True):
            runtime = importlib.import_module("linkedin_central_browser")
            runtime.open_central_page(playwright)

        self.assertEqual(["http://linkedin-browser:9222"], playwright.chromium.connected_endpoints)

    def test_page_only_close_even_when_body_raises(self) -> None:
        context = FakeContext()
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)
        runtime = importlib.import_module("linkedin_central_browser")

        with self.assertRaisesRegex(RuntimeError, "body boom"):
            with runtime.central_page(playwright) as lease:
                self.assertFalse(lease.page.closed)
                raise RuntimeError("body boom")

        self.assertTrue(context.pages[0].closed)
        self.assertFalse(context.closed, "shared persistent context must stay open")
        self.assertFalse(browser.closed, "shared CDP browser must stay open")

    def test_page_only_close_even_when_page_operation_raises(self) -> None:
        context = FakeContext(fail_goto=True)
        browser = FakeBrowser([context])
        playwright = FakePlaywright(browser)
        runtime = importlib.import_module("linkedin_central_browser")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with runtime.central_page(playwright) as lease:
                lease.page.goto("https://www.linkedin.com/jobs/")

        self.assertTrue(context.pages[0].closed)
        self.assertFalse(context.closed)
        self.assertFalse(browser.closed)

    def test_fail_closed_on_zero_or_multiple_contexts(self) -> None:
        runtime = importlib.import_module("linkedin_central_browser")
        for contexts in ([], [FakeContext(), FakeContext()]):
            with self.subTest(context_count=len(contexts)):
                browser = FakeBrowser(contexts)
                playwright = FakePlaywright(browser)
                with self.assertRaisesRegex(RuntimeError, "expected exactly one persistent default context"):
                    runtime.open_central_page(playwright)
                self.assertFalse(browser.closed)
                self.assertTrue(all(not ctx.closed for ctx in contexts))


class JobSeekerCentralBrowserStaticTest(unittest.TestCase):
    TARGETS = [
        JOBSEEKER_SCRIPTS / "linkedin_extractor.py",
        JOBSEEKER_SCRIPTS / "linkedin_worker.py",
        JOBSEEKER_SCRIPTS / "linkedin_central_browser.py",
    ]

    def test_jobseeker_python_uses_cdp_not_forbidden_browser_apis_or_storage_state(self) -> None:
        forbidden = {
            "chromium.launch(": "must not launch private Chromium",
            "launch_persistent_context(": "must not create persistent browser profile",
            "browser.new_context(": "must not create an isolated context",
            ".new_context(": "must not create an isolated context",
            "storage_state": "must not read/write storage_state or depend on session json",
            "linkedin_session.json": "must not depend on legacy session file",
        }
        offenders: list[str] = []
        for path in self.TARGETS:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            for needle, reason in forbidden.items():
                if needle in text:
                    offenders.append(f"{path.relative_to(ROOT)} contains {needle!r}: {reason}")
            if path.name in {"linkedin_extractor.py", "linkedin_worker.py"}:
                self.assertIn("connect_over_cdp", text, f"{path.name} must connect to central Chromium over CDP")
        self.assertEqual([], offenders)

    def test_run_unlocked_has_no_session_gate_and_worker_has_no_xvfb(self) -> None:
        text = (JOBSEEKER_SCRIPTS / "run_unlocked.sh").read_text(encoding="utf-8")
        self.assertNotIn("linkedin_session.json", text)
        self.assertNotIn("xvfb-run", text)
        self.assertIn("python3 linkedin_worker.py", text)


if __name__ == "__main__":
    unittest.main()
