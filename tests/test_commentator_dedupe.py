"""Unit tests for LinkedIn Commentator canonical comment keys and unified dedupe.

No browser, no Telegram, no LinkedIn/network/actions.
"""

from __future__ import annotations

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


class CommentatorCanonicalDedupeTest(unittest.TestCase):
    def test_comment_urn_and_dash_comment_urn_produce_same_key(self) -> None:
        runtime = import_commentator()
        comment_url = (
            "https://www.linkedin.com/feed/update/urn:li:activity:7345678901234567890/"
            "?commentUrn=urn%3Ali%3Acomment%3A(activity%3A7345678901234567890%2C9988776655)"
        )
        dash_url = (
            "https://www.linkedin.com/analytics/post-summary/urn:li:activity:7345678901234567890/"
            "?dashCommentUrn=urn%3Ali%3Afsd_comment%3A(9988776655%2Curn%3Ali%3Aactivity%3A7345678901234567890)"
        )

        self.assertEqual(
            "7345678901234567890:9988776655",
            runtime.canonical_comment_key(comment_url, "Jane Doe", "Great point"),
        )
        self.assertEqual(
            runtime.canonical_comment_key(comment_url, "Jane Doe", "Great point"),
            runtime.canonical_comment_key(dash_url, "Jane Doe", "Great point"),
        )

    def test_notification_and_post_scan_with_comment_id_merge_to_one_candidate_without_second_reply(self) -> None:
        runtime = import_commentator()
        post_url = "https://www.linkedin.com/feed/update/urn:li:activity:7345678901234567890/"
        comment_urn = "urn:li:comment:(activity:7345678901234567890,9988776655)"
        notification_url = f"{post_url}?commentUrn={comment_urn}"
        author = "Jane Doe"
        text = "Could you explain the DevOps boundary here?"
        first = runtime.candidate_from_metadata(
            source="notifications",
            post_url=notification_url,
            author=author,
            author_profile="https://www.linkedin.com/in/jane-doe/?miniProfileUrn=tracking",
            comment_text=text,
            post_excerpt="notification card",
            reply="first reply",
            reason="question;generator=test",
            identifier_url=notification_url,
        )
        candidates = {first.id: first}

        identifier_url = runtime.linkedin_identifier_url(post_url, comment_urn)
        metadata = runtime.candidate_metadata(
            key_url=identifier_url,
            post_url=post_url,
            author=author,
            author_profile="https://www.linkedin.com/in/jane-doe/",
            comment_text=text,
            post_excerpt="post scan excerpt",
        )
        self.assertEqual(first.id, metadata["id"])
        runtime.merge_existing_state_item(candidates[metadata["id"]], "my_posts", metadata)

        self.assertEqual(1, len(candidates))
        self.assertEqual("first reply", candidates[first.id].reply)
        self.assertEqual(["notifications", "my_posts"], candidates[first.id].source_seen)
        self.assertEqual("7345678901234567890", candidates[first.id].post_id)
        self.assertEqual("9988776655", candidates[first.id].comment_id)

    def test_fallback_key_ignores_query_tracking(self) -> None:
        runtime = import_commentator()
        url_a = "https://www.linkedin.com/feed/update/urn:li:activity:7345678901234567890/?utm_source=email&trk=foo"
        url_b = "https://www.linkedin.com/feed/update/urn:li:activity:7345678901234567890/?trackingId=bar&lipi=baz"

        self.assertEqual(
            runtime.canonical_comment_key(url_a, "Jane Doe", "Same comment text", "https://www.linkedin.com/in/jane-doe/?trk=notif"),
            runtime.canonical_comment_key(url_b, "Jane Doe", "Same comment text", "https://www.linkedin.com/in/jane-doe/?miniProfileUrn=abc"),
        )

    def test_reply_urn_keeps_parent_comment_as_primary_key_and_stores_reply_id(self) -> None:
        runtime = import_commentator()
        url = (
            "https://www.linkedin.com/feed/update/urn:li:activity:7345678901234567890/"
            "?commentUrn=urn%3Ali%3Acomment%3A(activity%3A7345678901234567890%2Cparent123)"
            "&replyUrn=urn%3Ali%3Acomment%3A(activity%3A7345678901234567890%2Creply456)"
        )

        ids = runtime.extract_linkedin_comment_identifiers(url)
        self.assertEqual("7345678901234567890:parent123", runtime.canonical_comment_key(url, "Jane", "Text"))
        self.assertEqual("parent123", ids["comment_id"])
        self.assertEqual("reply456", ids["reply_id"])


if __name__ == "__main__":
    unittest.main()
