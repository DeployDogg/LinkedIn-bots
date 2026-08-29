"""Unit tests for LinkedIn Commentator extraction quality and report dedupe.

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


class CommentatorExtractionQualityTest(unittest.TestCase):
    def test_clean_post_excerpt_removes_linkedin_chrome_and_preserves_russian_post(self) -> None:
        runtime = import_commentator()
        raw = """
        Feed detail update
        Andrew Anashkin
        Andrew Anashkin DevOps / SRE / Platform engineer
        View my services
        Visible to anyone on or off LinkedIn
        Я тут поймал себя на простой мысли: DevOps не должен быть бесплатной заменой backend-разработчика.
        Инфраструктурный код — да. Закрывать продуктовые фичи за команду — нет.
        Show translation
        Activate to view larger image
        47 reactions 12 comments 3 reposts View analytics
        Reactions Like Comment Repost Send
        Add a comment…
        Open Emoji Keyboard
        Current selected sort order is Most relevant
        Most relevant comments section
        """

        clean = runtime.clean_post_excerpt(raw)

        self.assertIn("DevOps не должен быть бесплатной заменой backend-разработчика", clean)
        self.assertIn("Инфраструктурный код", clean)
        for chrome in [
            "Feed detail update",
            "Andrew Anashkin DevOps / SRE / Platform engineer",
            "View my services",
            "Visible to anyone",
            "Show translation",
            "Activate to view",
            "View analytics",
            "Reactions Like Comment Repost Send",
            "Add a comment",
            "Open Emoji Keyboard",
            "Current selected sort order",
            "Most relevant",
        ]:
            self.assertNotIn(chrome, clean)

    def test_clean_comment_text_removes_more_controls_and_embedded_post_text(self) -> None:
        runtime = import_commentator()
        post = "Я тут поймал себя на простой мысли: DevOps не должен быть бесплатной заменой backend-разработчика."
        raw = (
            "Jane Doe 2nd Senior Recruiter 3h "
            "А где тогда проходит граница между автоматизацией и продуктовой разработкой? …more "
            "Like Reply Translate Report "
            + post
        )

        clean = runtime.clean_comment_text(raw, author="Jane Doe", post_excerpt=post)

        self.assertEqual("А где тогда проходит граница между автоматизацией и продуктовой разработкой?", clean)

    def test_clean_post_excerpt_removes_collapsed_age_visibility_and_reaction_chrome(self) -> None:
        runtime = import_commentator()
        cases = [
            (
                "2w • 2 weeks ago • on or off LinkedIn Реальный смысловой текст поста larger image, larger image, 128 You and 127 others",
                "Реальный смысловой текст поста",
            ),
            (
                "3d • 3 days ago • Visible to anyone on or off LinkedIn Другой смысловой текст поста 12 comments 128 reactions",
                "Другой смысловой текст поста",
            ),
        ]

        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(expected, runtime.clean_post_excerpt(raw))

    def test_clean_comment_text_removes_degree_and_headline_prefix_without_eating_body(self) -> None:
        runtime = import_commentator()
        raw = "• 2nd Head of Development | 10+years | ... AWS А по-моему..."

        self.assertEqual("А по-моему...", runtime.clean_comment_text(raw))
        self.assertEqual("А по-моему...", runtime.extract_body_from_comment_node(raw, "unknown"))

    def test_collect_visible_post_comments_rejects_messaging_overlay_rows(self) -> None:
        runtime = import_commentator()

        class FakePage:
            script = ""

            def evaluate(self, script: str):
                self.script = script
                return [
                    {"text": "MONDAY Aiganym Alibek sent the following message...", "authorHref": "", "identifierText": ""},
                    {"text": "Scroll quick replies left", "authorHref": "", "identifierText": ""},
                    {"text": "Reply to conversation with No problem", "authorHref": "", "identifierText": ""},
                    {
                        "text": "Jane Doe • 2nd Platform Engineer 3h Реальный комментарий? Like Reply",
                        "authorHref": "https://www.linkedin.com/in/jane-doe/",
                        "identifierText": "urn:li:comment:(activity:1,2)",
                    },
                ]

        page = FakePage()
        comments = runtime.collect_visible_post_comments(page)

        self.assertEqual(1, len(comments))
        self.assertIn("Реальный комментарий?", comments[0]["text"])
        self.assertNotIn("'article li'", page.script)
        self.assertNotIn("'li'", page.script)
        self.assertIn("comments-comment-entity", page.script)
        self.assertIn("sent the following message", page.script)
        self.assertIn("reply to conversation with", page.script)
        self.assertIn("scroll quick replies", page.script)

    def test_duplicate_notification_and_post_scan_without_comment_id_writes_one_draft(self) -> None:
        runtime = import_commentator()
        post_url = "https://www.linkedin.com/feed/update/urn:li:activity:7345678901234567890/"
        notification = runtime.candidate_from_metadata(
            source="notifications",
            post_url=post_url,
            author="Jane Doe",
            author_profile="",
            comment_text="Could you explain the DevOps boundary here?",
            post_excerpt="notification card",
            reply="reply from notification",
            reason="question;generator=test",
        )
        candidates = {notification.id: notification}
        signature_index = {runtime.candidate_dedupe_signature_dict(vars(notification)): notification.id}

        metadata = runtime.candidate_metadata(
            key_url=post_url,
            post_url=post_url,
            author="Jane Doe",
            author_profile="https://www.linkedin.com/in/jane-doe/",
            comment_text="Could you explain the DevOps boundary here?",
            post_excerpt="clean post scan",
        )
        existing_id = runtime.find_duplicate_candidate_id(candidates, metadata, signature_index)
        self.assertEqual(notification.id, existing_id)
        runtime.merge_existing_state_item(candidates[existing_id], "my_posts", metadata)

        state = {"items": {notification.id: {**vars(candidates[notification.id]), "decision": "proposed"}}}
        status = {"started_at": "2026-01-01T10:00:00-03:00"}
        out = ROOT / "tmp" / "test_commentator_drafts.md"
        with patch.object(runtime, "DRAFTS_PATH", out):
            runtime.write_drafts(status, state)
        text = out.read_text(encoding="utf-8")

        self.assertEqual(1, text.count("## Jane Doe ·"))
        self.assertIn("source_seen: notifications,my_posts", text)

    def test_owner_self_comments_are_skipped(self) -> None:
        runtime = import_commentator()

        ok, reason = runtime.should_reply("Andrew Anashkin", "А где тут граница между DevOps и backend?")

        self.assertFalse(ok)
        self.assertEqual("own_or_unknown_author", reason)

    def test_low_value_emoji_and_short_agree_comments_are_skipped(self) -> None:
        runtime = import_commentator()

        self.assertEqual((False, "low_value_emoji_or_reaction"), runtime.should_reply("Jane Doe", "🔥🔥👏"))
        self.assertEqual((False, "short_confirmation"), runtime.should_reply("Jane Doe", "Agree, thanks!"))


if __name__ == "__main__":
    unittest.main()
