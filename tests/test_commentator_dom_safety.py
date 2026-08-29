"""Unit tests for LinkedIn Commentator DOM publish safety net.

No real browser, no LinkedIn/network/actions. Playwright is faked or bypassed.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
COMMENT_SCRIPTS = ROOT / "services" / "Commentator" / "scripts"
if str(COMMENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(COMMENT_SCRIPTS))


def import_commentator():
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: None
    fake_sync_api.TimeoutError = TimeoutError
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    with patch.dict(sys.modules, {"playwright": fake_playwright, "playwright.sync_api": fake_sync_api}):
        sys.modules.pop("linkedin_commentator", None)
        return importlib.import_module("linkedin_commentator")


class FakeLocator:
    def __init__(self, page, *, count: int = 0, text: str = "") -> None:
        self.page = page
        self._count = count
        self._text = text
        self.first = self
        self.last = self

    def count(self) -> int:
        return self._count

    def click(self, *args, **kwargs) -> None:
        self.page.clicks += 1

    def fill(self, text: str, *args, **kwargs) -> None:
        self.page.filled_texts.append(text)

    def inner_text(self, *args, **kwargs) -> str:
        return self._text


class FakePublishPage:
    def __init__(self, evaluations: list[dict]) -> None:
        self.evaluations = list(evaluations)
        self.evaluate_payloads: list[dict] = []
        self.submit_clicks = 0
        self.clicks = 0
        self.filled_texts: list[str] = []
        self.goto_urls: list[str] = []
        self.url = "https://www.linkedin.com/feed/update/urn:li:activity:1/"
        self.closed = False

    def goto(self, url: str, **kwargs) -> None:
        self.goto_urls.append(url)
        self.url = url

    def evaluate(self, script: str, payload: dict):
        self.evaluate_payloads.append(payload)
        if "buttons[0].click()" in script:
            self.clicks += 1
            return True
        if not self.evaluations:
            raise AssertionError("unexpected page.evaluate call")
        return self.evaluations.pop(0)

    def locator(self, selector: str):
        if selector == "body":
            return FakeLocator(self, count=1, text="normal linkedin page")
        if selector == '[contenteditable="true"]':
            return FakeLocator(self, count=1)
        return FakeLocator(self, count=0)

    def get_by_text(self, pattern):
        return FakeLocator(self, count=0)

    def get_by_role(self, role: str, name=None):
        page = self

        class Button(FakeLocator):
            def count(self) -> int:
                return 1

            def click(self, *args, **kwargs) -> None:
                page.submit_clicks += 1

        return Button(self, count=1)

    def close(self) -> None:
        self.closed = True


class FakeSyncPlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class CommentatorDomSafetyTest(unittest.TestCase):
    def test_existing_owner_reply_detected_before_submit_skips_publish(self) -> None:
        runtime = import_commentator()
        page = FakePublishPage([
            {"found": True, "matched_by": "comment_id", "owner_replies": [{"author": "Andrew Anashkin", "text": "Already answered."}]},
        ])
        item = self._item(runtime)

        ok, reason = self._publish(runtime, page, item)

        self.assertTrue(ok)
        self.assertEqual("already_replied_on_linkedin", reason)
        self.assertEqual(0, page.submit_clicks)
        self.assertEqual([], page.filled_texts)
        self.assertEqual("123", page.evaluate_payloads[0]["comment_id"])

    def test_similar_proposed_reply_detected_before_submit_skips_publish(self) -> None:
        runtime = import_commentator()
        page = FakePublishPage([
            {"found": True, "matched_by": "text", "owner_replies": [], "all_replies": [{"author": "Other", "text": "Yep — Kubernetes needs ownership boundaries, not magic."}]},
        ])
        item = self._item(runtime)

        ok, reason = self._publish(runtime, page, item)

        self.assertTrue(ok)
        self.assertEqual("same_reply_exists_on_linkedin", reason)
        self.assertEqual(0, page.submit_clicks)

    def test_verified_after_submit_marks_published_verified_and_submits_once(self) -> None:
        runtime = import_commentator()
        page = FakePublishPage([
            {"found": True, "matched_by": "comment_id", "owner_replies": [], "all_replies": []},
            {"found": True, "matched_by": "comment_id", "owner_replies": [{"author": "Andrew Anashkin", "text": "Yep — Kubernetes needs ownership boundaries, not magic."}], "all_replies": []},
        ])
        item = self._item(runtime)

        ok, reason = self._publish(runtime, page, item)

        self.assertTrue(ok)
        self.assertEqual("published_verified", reason)
        self.assertEqual(1, page.submit_clicks)
        self.assertEqual([item["reply"]], page.filled_texts)

    def test_unverified_after_submit_submits_once_and_state_machine_disables_auto_retry(self) -> None:
        runtime = import_commentator()
        page = FakePublishPage([
            {"found": True, "matched_by": "comment_id", "owner_replies": [], "all_replies": []},
            {"found": True, "matched_by": "comment_id", "owner_replies": [], "all_replies": []},
        ])
        item = self._item(runtime)

        ok, reason = self._publish(runtime, page, item)
        runtime.complete_publish_attempt(item, "run-1", ok, reason, now="2026-01-01T10:00:00-03:00")

        self.assertFalse(ok)
        self.assertEqual("published_unverified", reason)
        self.assertEqual(1, page.submit_clicks)
        self.assertEqual("publish_failed", item["decision"])
        self.assertTrue(item["requires_manual_review"])
        self.assertEqual("published_unverified", item["last_publish_error"])

    def test_target_comment_matching_prefers_comment_id_but_can_fallback_to_author_text(self) -> None:
        runtime = import_commentator()
        page = FakePublishPage([
            {"found": True, "matched_by": "author_text", "owner_replies": [], "all_replies": []},
        ])
        item = self._item(runtime)
        item["comment_id"] = "target-id"

        result = runtime.find_linkedin_comment_thread(page, item)

        self.assertTrue(result["found"])
        self.assertEqual("author_text", result["matched_by"])
        self.assertEqual("target-id", page.evaluate_payloads[0]["comment_id"])
        self.assertIn("Jane Doe", page.evaluate_payloads[0]["author"])
        self.assertIn("Kubernetes", page.evaluate_payloads[0]["comment_text"])

    def test_normalized_reply_matching_handles_case_punctuation_and_whitespace(self) -> None:
        runtime = import_commentator()

        self.assertTrue(runtime.reply_text_matches(" Yep, Kubernetes needs ownership boundaries — not magic! ", "yep kubernetes needs ownership boundaries not magic"))
        self.assertGreaterEqual(runtime.text_similarity("Kubernetes ownership boundaries", "kubernetes ownership boundary"), 0.85)

    def _item(self, runtime):
        return {
            "id": "1:123",
            "comment_key": "1:123",
            "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/?commentUrn=urn%3Ali%3Acomment%3A(activity%3A1%2C123)",
            "comment_id": "123",
            "author": "Jane Doe",
            "comment_text": "What is the Kubernetes boundary here?",
            "reply": "Yep — Kubernetes needs ownership boundaries, not magic.",
            "decision": runtime.DECISION_PUBLISHING,
            "publishing_run_id": "run-1",
            "publishing_started_at": "2026-01-01T09:59:00-03:00",
            "publish_attempts": [{"run_id": "run-1", "started_at": "2026-01-01T09:59:00-03:00", "ok": None}],
        }

    def _publish(self, runtime, page, item):
        args = argparse.Namespace(dry_run=False, publish_approved=True)
        with patch.object(runtime, "sync_playwright", return_value=FakeSyncPlaywright()), \
             patch.object(runtime, "connect_browser", return_value=(object(), object(), page, None)), \
             patch.object(runtime, "page_wait", return_value=None):
            return runtime.publish_reply_for_item(item, args)


if __name__ == "__main__":
    unittest.main()
