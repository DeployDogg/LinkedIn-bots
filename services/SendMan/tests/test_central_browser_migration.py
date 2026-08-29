#!/usr/bin/env python3
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "linkedin_message_outreach.py"
spec = importlib.util.spec_from_file_location("sendman_outreach", SCRIPT_PATH)
outreach = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = outreach
spec.loader.exec_module(outreach)


class FakePage:
    def __init__(self, url="https://www.linkedin.com/feed/"):
        self.url = url
        self.closed = False
        self.default_timeout = None

    def close(self):
        self.closed = True

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def locator(self, selector):
        return FakeLocator("")


class FakeLocator:
    def __init__(self, text):
        self.text = text

    def inner_text(self, timeout=None):
        return self.text


class FakeContext:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.created_pages = []
        self.closed = False

    def new_page(self):
        page = FakePage()
        self.created_pages.append(page)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, contexts=None):
        self.contexts = list(contexts or [])
        self.closed = False

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser=None, error=None):
        self.browser = browser
        self.error = error
        self.cdp_calls = []

    def connect_over_cdp(self, endpoint, timeout=None):
        self.cdp_calls.append((endpoint, timeout))
        if self.error:
            raise self.error
        return self.browser


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


class FakeSyncPlaywright:
    def __init__(self, playwright):
        self.playwright = playwright

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, tb):
        return False


class Args:
    dry_run = True
    max_messages = 1
    max_per_job = 1
    max_pages = 1
    job = "DevOps"
    self_test = False
    skip_dialog_scan = True
    no_delay = True
    delay_base = 0
    delay_jitter = 0
    dialog_scrolls = 1
    headful = False


class CentralBrowserMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.status_path = Path(self.tmp.name) / "status.json"
        self.state_path = Path(self.tmp.name) / "state.json"
        self.dialog_path = Path(self.tmp.name) / "dialogs.json"
        self.log_dir = Path(self.tmp.name) / "logs"
        patches = [
            patch.object(outreach, "STATUS_PATH", self.status_path),
            patch.object(outreach, "STATE_PATH", self.state_path),
            patch.object(outreach, "DIALOG_STOPLIST_PATH", self.dialog_path),
            patch.object(outreach, "STATE_DIR", Path(self.tmp.name)),
            patch.object(outreach, "SCREENSHOT_DIR", Path(self.tmp.name) / "screens"),
            patch.object(outreach, "LOG_DIR", self.log_dir),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_default_cdp_endpoint_points_to_central_browser_service(self):
        self.assertEqual(outreach.CDP_ENDPOINT, "http://linkedin-browser:9222")

    def test_open_linkedin_context_uses_cdp_existing_context_and_creates_one_owned_page(self):
        shared_page = FakePage()
        context = FakeContext(pages=[shared_page])
        browser = FakeBrowser(contexts=[context])
        chromium = FakeChromium(browser=browser)
        status = {}

        browser_handle, selected_context, page, mode = outreach.open_linkedin_context(FakePlaywright(chromium), status)

        self.assertIs(browser_handle, browser)
        self.assertIs(selected_context, context)
        self.assertIs(page, context.created_pages[0])
        self.assertIsNot(page, shared_page)
        self.assertEqual(len(context.created_pages), 1)
        self.assertEqual(mode, "cdp")
        self.assertEqual(chromium.cdp_calls, [("http://linkedin-browser:9222", 45000)])
        self.assertFalse(browser.closed)
        self.assertFalse(context.closed)

    def test_open_linkedin_context_fails_closed_without_exactly_one_existing_context(self):
        for contexts in ([], [FakeContext(), FakeContext()]):
            with self.subTest(context_count=len(contexts)):
                chromium = FakeChromium(browser=FakeBrowser(contexts=contexts))
                status = {}
                with self.assertRaises(SystemExit) as cm:
                    outreach.open_linkedin_context(FakePlaywright(chromium), status)
                self.assertEqual(cm.exception.code, 11)
                self.assertIn("context_count", status.get("stop_reason", ""))

    def test_open_linkedin_context_fails_closed_on_cdp_connect_error(self):
        chromium = FakeChromium(error=RuntimeError("cdp down"))
        status = {}
        with self.assertRaises(SystemExit) as cm:
            outreach.open_linkedin_context(FakePlaywright(chromium), status)
        self.assertEqual(cm.exception.code, 11)
        self.assertIn("cdp_connect_failed", status.get("stop_reason", ""))

    def test_run_closes_only_owned_page_and_leaves_shared_browser_context_open(self):
        context = FakeContext(pages=[FakePage()])
        browser = FakeBrowser(contexts=[context])
        chromium = FakeChromium(browser=browser)
        args = Args()

        with patch.object(outreach, "sync_playwright", return_value=FakeSyncPlaywright(FakePlaywright(chromium))), \
             patch.object(outreach, "scan_dialog_stoplist", return_value=set()), \
             patch.object(outreach, "process_search", return_value=None):
            code = outreach.run(args)

        owned_page = context.created_pages[0]
        self.assertEqual(code, 0)
        self.assertTrue(owned_page.closed)
        self.assertFalse(context.closed)
        self.assertFalse(browser.closed)

    def test_authwall_login_checkpoint_challenge_captcha_security_rate_limit_urls_are_blockers(self):
        blocker_fragments = ["/login", "/authwall", "/checkpoint", "/challenge", "/captcha", "/security", "/rate-limit"]
        for fragment in blocker_fragments:
            with self.subTest(fragment=fragment):
                page = FakePage(url=f"https://www.linkedin.com{fragment}/x")
                reason = outreach.detect_stop(page)
                self.assertIsNotNone(reason)
                self.assertIn(fragment.strip("/"), reason)

    def test_sendman_scripts_do_not_contain_forbidden_local_browser_or_login_fallbacks(self):
        files = [
            SCRIPT_PATH,
            SCRIPT_PATH.parent / "run_unlocked.sh",
        ]
        forbidden = [
            "chromium.launch(",
            "launch_persistent_context",
            "browser.new_context",
            "new_context(storage_state",
            "storage_state",
            "LINKEDIN_SESSION_PATH",
            "LINKEDIN_CHROMIUM_PROFILE_DIR",
            "login_with_env",
            "LINKEDIN_EMAIL",
            "LINKEDIN_PASSWORD",
            "xvfb-run",
        ]
        offenders = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    offenders.append(f"{path.name}:{token}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
