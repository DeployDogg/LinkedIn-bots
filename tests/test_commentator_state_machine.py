"""Unit tests for LinkedIn Commentator production-safety state machine.

No browser, no Telegram, no LinkedIn/network/actions.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import tempfile
import unittest
from argparse import Namespace
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


class CommentatorStateMachineTest(unittest.TestCase):
    def test_terminal_states_do_not_return_to_approved_from_callbacks(self) -> None:
        runtime = import_commentator()
        published = {"id": "c1", "decision": "published", "published_at": "2026-01-01T10:00:00-03:00"}
        rejected = {"id": "c2", "decision": "rejected", "decided_at": "2026-01-01T10:00:00-03:00"}

        self.assertFalse(runtime.apply_callback_decision(published, "approve", "cb1")[0])
        self.assertFalse(runtime.apply_callback_decision(rejected, "approve", "cb2")[0])

        self.assertEqual("published", published["decision"])
        self.assertEqual("rejected", rejected["decision"])

    def test_double_approve_is_noop_and_does_not_create_publish_attempt(self) -> None:
        runtime = import_commentator()
        item = {"id": "c1", "decision": "sent_to_telegram"}

        changed1, reason1 = runtime.apply_callback_decision(item, "approve", "cb1")
        changed2, reason2 = runtime.apply_callback_decision(item, "approve", "cb1-duplicate")

        self.assertTrue(changed1)
        self.assertEqual("approved", item["decision"])
        self.assertFalse(changed2)
        self.assertEqual("noop_already_approved", reason2)
        self.assertEqual([], item.get("publish_attempts", []))

    def test_fresh_publishing_lock_skips_second_publish(self) -> None:
        runtime = import_commentator()
        item = {
            "id": "c1",
            "decision": "publishing",
            "publishing_started_at": "2026-01-01T10:00:00-03:00",
            "publishing_run_id": "run-1",
        }

        acquired, reason = runtime.acquire_publish_lock(item, "run-2", now="2026-01-01T10:30:00-03:00", stale_minutes=60, max_attempts=3)

        self.assertFalse(acquired)
        self.assertEqual("publishing_lock_fresh", reason)
        self.assertEqual("publishing", item["decision"])
        self.assertEqual("run-1", item["publishing_run_id"])

    def test_stale_publishing_lock_recovers_and_acquires_new_lock(self) -> None:
        runtime = import_commentator()
        item = {
            "id": "c1",
            "decision": "publishing",
            "publishing_started_at": "2026-01-01T10:00:00-03:00",
            "publishing_run_id": "run-1",
            "publish_attempts": [{"run_id": "run-1", "started_at": "2026-01-01T10:00:00-03:00"}],
        }

        acquired, reason = runtime.acquire_publish_lock(item, "run-2", now="2026-01-01T11:01:00-03:00", stale_minutes=60, max_attempts=3)

        self.assertTrue(acquired)
        self.assertEqual("publishing_lock_acquired", reason)
        self.assertEqual("publishing", item["decision"])
        self.assertEqual("run-2", item["publishing_run_id"])
        self.assertEqual(False, item["publish_attempts"][0]["ok"])
        self.assertEqual("stale_publishing_recovered", item["publish_attempts"][0]["reason"])
        self.assertEqual(2, len(item["publish_attempts"]))

    def test_max_publish_attempts_marks_manual_review_and_stops_retry(self) -> None:
        runtime = import_commentator()
        item = {
            "id": "c1",
            "decision": "approved",
            "publish_attempts": [
                {"ok": False, "reason": "reply_button_not_found"},
                {"ok": False, "reason": "reply_button_not_found"},
                {"ok": False, "reason": "reply_button_not_found"},
            ],
        }

        acquired, reason = runtime.acquire_publish_lock(item, "run-4", now="2026-01-01T10:00:00-03:00", stale_minutes=60, max_attempts=3)

        self.assertFalse(acquired)
        self.assertEqual("max_publish_attempts_reached", reason)
        self.assertEqual("publish_failed", item["decision"])
        self.assertTrue(item["requires_manual_review"])

    def test_reject_is_ignored_while_publishing_or_published(self) -> None:
        runtime = import_commentator()
        publishing = {"id": "c1", "decision": "publishing", "publishing_started_at": "2026-01-01T10:00:00-03:00"}
        published = {"id": "c2", "decision": "published", "published_at": "2026-01-01T10:00:00-03:00"}

        self.assertFalse(runtime.apply_callback_decision(publishing, "reject", "cb1")[0])
        self.assertFalse(runtime.apply_callback_decision(published, "reject", "cb2")[0])

        self.assertEqual("publishing", publishing["decision"])
        self.assertEqual("published", published["decision"])

    def test_poll_approvals_ignores_unknown_item_without_crash(self) -> None:
        runtime = import_commentator()
        state = {"items": {}, "telegram_update_offset": 0}

        def fake_api(token, method, payload, timeout=25):
            if method == "getUpdates":
                return {"result": [{"update_id": 7, "callback_query": {"id": "cb1", "data": "li_comment:approve:missing"}}]}
            if method == "answerCallbackQuery":
                return {"ok": True}
            raise AssertionError(method)

        with patch.object(runtime, "telegram_config", return_value=("token", "chat")), patch.object(runtime, "telegram_api", side_effect=fake_api):
            events = runtime.poll_approvals(state, dry_run=False)

        self.assertEqual(8, state["telegram_update_offset"])
        self.assertEqual([{"id": "missing", "action": "approve", "changed": "false", "reason": "unknown_item_ignored"}], events)

    def test_telegram_send_is_not_repeated_when_message_id_exists(self) -> None:
        runtime = import_commentator()
        item = {"id": "c1", "decision": "sent_to_telegram", "telegram_message_id": "123"}

        self.assertFalse(runtime.should_send_telegram_approval(item))

    def _proposed_item(self, idx: int, telegram_status: str, *, created_at: str, reason: str = "ok;generator=fallback", message_id: str | None = None, decision: str = "proposed") -> dict[str, object]:
        item: dict[str, object] = {
            "id": f"c{idx:02d}",
            "comment_key": f"c{idx:02d}",
            "post_id": f"p{idx:02d}",
            "comment_id": f"comment{idx:02d}",
            "reply_id": None,
            "source_seen": ["notifications"],
            "post_url": f"https://www.linkedin.com/feed/update/urn:li:activity:{idx}/",
            "author": f"Author {idx}",
            "author_profile": f"https://www.linkedin.com/in/author-{idx}/",
            "comment_text": f"comment {idx}",
            "post_excerpt": f"post {idx}",
            "reply": f"reply {idx}",
            "reason": reason,
            "decision": decision,
            "created_at": created_at,
            "telegram_status": telegram_status,
        }
        if message_id is not None:
            item["telegram_message_id"] = message_id
        return item

    def test_callback_token_is_short_deterministic_unique_and_handles_colon_heavy_80_byte_ids(self) -> None:
        runtime = import_commentator()
        long_id = "urn:li:comment:(activity:1234567890123456789,comment:abc:def:ghi:jkl:mno:pqr:stu)"
        other_id = long_id + ":other"

        token1 = runtime.telegram_callback_token_for_id(long_id)
        token2 = runtime.telegram_callback_token_for_id(long_id)
        token3 = runtime.telegram_callback_token_for_id(other_id)

        self.assertEqual(token1, token2)
        self.assertNotEqual(token1, token3)
        self.assertRegex(token1, r"^ct_[a-f0-9]{24}$")
        self.assertLessEqual(len(f"li_comment:approve:{token1}".encode("utf-8")), 64)
        self.assertLessEqual(len(f"li_comment:reject:{token1}".encode("utf-8")), 64)

    def test_poll_approvals_resolves_short_token_to_exact_long_state_item_and_preserves_legacy_ids(self) -> None:
        runtime = import_commentator()
        long_id = "urn:li:comment:(activity:1234567890123456789,comment:abc:def:ghi:jkl:mno:pqr:stu)"
        token = runtime.telegram_callback_token_for_id(long_id)
        state = {"items": {
            long_id: {"id": long_id, "decision": "sent_to_telegram", "telegram_callback_token": token},
            "legacy-short": {"id": "legacy-short", "decision": "sent_to_telegram"},
        }, "telegram_update_offset": 0}

        def fake_api(token_value, method, payload, timeout=25):
            if method == "getUpdates":
                return {"result": [
                    {"update_id": 7, "callback_query": {"id": "cb-long", "data": f"li_comment:approve:{token}"}},
                    {"update_id": 8, "callback_query": {"id": "cb-legacy", "data": "li_comment:reject:legacy-short"}},
                ]}
            if method == "answerCallbackQuery":
                return {"ok": True}
            raise AssertionError(method)

        with patch.object(runtime, "telegram_config", return_value=("token", "chat")), patch.object(runtime, "telegram_api", side_effect=fake_api):
            events = runtime.poll_approvals(state, dry_run=False)

        self.assertEqual("approved", state["items"][long_id]["decision"])
        self.assertEqual("rejected", state["items"]["legacy-short"]["decision"])
        self.assertEqual(long_id, events[0]["id"])
        self.assertEqual("legacy-short", events[1]["id"])

    def test_duplicate_callback_id_is_idempotent_without_second_transition_event(self) -> None:
        runtime = import_commentator()
        item = {"id": "c1", "decision": "sent_to_telegram"}

        changed1, reason1 = runtime.apply_callback_decision(item, "approve", "same-callback-id")
        changed2, reason2 = runtime.apply_callback_decision(item, "reject", "same-callback-id")

        self.assertTrue(changed1)
        self.assertEqual("approved", reason1)
        self.assertFalse(changed2)
        self.assertEqual("duplicate_callback_ignored", reason2)
        self.assertEqual("approved", item["decision"])
        events = [e for e in item.get("state_events", []) if e.get("callback_id") == "same-callback-id"]
        self.assertEqual(1, len(events))

    def test_send_approval_uses_short_callback_data_and_catches_http400_without_body_leakage(self) -> None:
        runtime = import_commentator()
        long_id = "urn:li:comment:(activity:1234567890123456789,comment:abc:def:ghi:jkl:mno:pqr:stu)"
        token = runtime.telegram_callback_token_for_id(long_id)
        candidate = runtime.Candidate(
            id=long_id,
            comment_key=long_id,
            post_id="post",
            comment_id="comment",
            reply_id=None,
            source_seen=["notifications"],
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:1/",
            author="Author",
            author_profile="https://www.linkedin.com/in/author/",
            comment_text="comment",
            post_excerpt="post",
            reply="reply",
            reason="safe;generator=codex",
        )
        payloads: list[dict[str, object]] = []

        def fake_api(token_value, method, payload, timeout=25):
            payloads.append(payload)
            raise RuntimeError("HTTP Error 400: Bad Request; body contains callback_data too long and SECRET_TOKEN")

        with patch.object(runtime, "telegram_config", return_value=("secret-token", "chat")), patch.object(runtime, "telegram_api", side_effect=fake_api):
            status, msg_id = runtime.send_approval(candidate, dry_run=False, send_enabled=True, callback_token=token)

        self.assertEqual("telegram_send_failed_http_400", status)
        self.assertIsNone(msg_id)
        keyboard = payloads[0]["reply_markup"]
        for button in keyboard["inline_keyboard"][0]:
            self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)
            self.assertIn(token, button["callback_data"])
            self.assertNotIn(long_id, button["callback_data"])
        self.assertNotIn("SECRET_TOKEN", status)
        self.assertNotIn("callback_data too long", status)

    def test_existing_pending_retry_continues_after_http400_and_persists_retryable_error(self) -> None:
        runtime = import_commentator()
        args = Namespace(dry_run=False, send_telegram=True, publish_approved=False, max_items=3, max_posts=100, scan_posts=True, no_delay=True)
        state = {"items": {
            "first": self._proposed_item(1, "telegram_config_missing", created_at="2026-07-28T12:00:00-03:00"),
            "second": self._proposed_item(2, "telegram_config_missing", created_at="2026-07-28T11:00:00-03:00"),
            "third": self._proposed_item(3, "telegram_config_missing", created_at="2026-07-28T10:00:00-03:00"),
        }, "telegram_update_offset": 0}
        calls: list[str] = []

        def fake_send(candidate, dry_run: bool, send_enabled: bool, callback_token: str | None = None):
            calls.append(candidate.id)
            self.assertIsNotNone(callback_token)
            if candidate.id == "c01":
                return "telegram_send_failed_http_400", None
            return "sent", f"msg-{candidate.id}"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            state_path.write_text(runtime.json.dumps(state), encoding="utf-8")
            with patch.object(runtime, "STATUS_PATH", tmp_path / "status.json"), \
                 patch.object(runtime, "STATE_PATH", state_path), \
                 patch.object(runtime, "DRAFTS_PATH", tmp_path / "drafts.md"), \
                 patch.object(runtime, "STATE_DIR", tmp_path), \
                 patch.object(runtime, "scan_candidates", return_value=[]), \
                 patch.object(runtime, "poll_approvals", return_value=[]), \
                 patch.object(runtime, "send_approval", side_effect=fake_send):
                status = runtime.run(args)

            self.assertEqual(["c01", "c02", "c03"], calls)
            self.assertEqual(2, status["telegram_sent"])
            persisted = runtime.load_json(state_path, {})
            self.assertEqual("telegram_send_failed_http_400", persisted["items"]["first"]["telegram_status"])
            self.assertIn("telegram_callback_token", persisted["items"]["first"])
            self.assertEqual("sent", persisted["items"]["second"]["telegram_status"])
            self.assertEqual("sent", persisted["items"]["third"]["telegram_status"])

    def test_existing_pending_proposed_items_retry_capped_prioritized_and_persisted(self) -> None:
        runtime = import_commentator()
        args = Namespace(
            dry_run=False,
            send_telegram=True,
            publish_approved=False,
            max_items=20,
            max_posts=100,
            scan_posts=True,
            no_delay=True,
        )
        items: dict[str, dict[str, object]] = {}
        # 20 config-missing items should win over newer dry-run-disabled items.
        for i in range(20):
            items[f"missing-{i:02d}"] = self._proposed_item(
                i, "telegram_config_missing", created_at=f"2026-07-28T10:{i:02d}:00-03:00", reason="safe;generator=fallback"
            )
        for i in range(20, 40):
            items[f"dry-{i:02d}"] = self._proposed_item(
                i, "not_sent_dry_run_or_disabled", created_at=f"2026-07-28T11:{i-20:02d}:00-03:00", reason="safe;generator=codex"
            )
        state = {"items": items, "telegram_update_offset": 0}
        sent_ids: list[str] = []

        def fake_send(candidate, dry_run: bool, send_enabled: bool, callback_token: str | None = None):
            self.assertFalse(dry_run)
            self.assertTrue(send_enabled)
            sent_ids.append(candidate.id)
            return "sent", f"msg-{candidate.id}"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_path = tmp_path / "status.json"
            state_path = tmp_path / "state.json"
            state_path.write_text(runtime.json.dumps(state), encoding="utf-8")
            with patch.object(runtime, "STATUS_PATH", status_path), \
                 patch.object(runtime, "STATE_PATH", state_path), \
                 patch.object(runtime, "DRAFTS_PATH", tmp_path / "drafts.md"), \
                 patch.object(runtime, "STATE_DIR", tmp_path), \
                 patch.object(runtime, "scan_candidates", return_value=[]), \
                 patch.object(runtime, "poll_approvals", return_value=[]), \
                 patch.object(runtime, "send_approval", side_effect=fake_send):
                status = runtime.run(args)

            self.assertEqual(20, status["telegram_sent"])
            self.assertEqual(20, len(sent_ids))
            self.assertEqual(len(set(sent_ids)), len(sent_ids))
            self.assertTrue(all(sid.startswith("c") for sid in sent_ids))
            self.assertEqual({f"c{i:02d}" for i in range(20)}, set(sent_ids))
            persisted_state = runtime.load_json(state_path, {})
            for key in [f"missing-{i:02d}" for i in range(20)]:
                self.assertEqual("sent", persisted_state["items"][key]["telegram_status"])
                self.assertTrue(str(persisted_state["items"][key]["telegram_message_id"]).startswith("msg-"))
            for key in [f"dry-{i:02d}" for i in range(20, 40)]:
                self.assertEqual("not_sent_dry_run_or_disabled", persisted_state["items"][key]["telegram_status"])

    def test_existing_pending_retry_priority_newest_then_codex_when_status_ties(self) -> None:
        runtime = import_commentator()
        args = Namespace(dry_run=False, send_telegram=True, publish_approved=False, max_items=3, max_posts=100, scan_posts=True, no_delay=True)
        state = {"items": {
            "old-codex": self._proposed_item(1, "telegram_config_missing", created_at="2026-07-28T10:00:00-03:00", reason="safe;generator=codex"),
            "new-fallback": self._proposed_item(2, "telegram_config_missing", created_at="2026-07-28T12:00:00-03:00", reason="safe;generator=fallback"),
            "tie-fallback": self._proposed_item(3, "telegram_config_missing", created_at="2026-07-28T11:00:00-03:00", reason="safe;generator=fallback"),
            "tie-codex": self._proposed_item(4, "telegram_config_missing", created_at="2026-07-28T11:00:00-03:00", reason="safe;generator=codex"),
        }, "telegram_update_offset": 0}
        order: list[str] = []

        def fake_send(candidate, dry_run: bool, send_enabled: bool, callback_token: str | None = None):
            order.append(candidate.id)
            return "sent", f"msg-{candidate.id}"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            state_path.write_text(runtime.json.dumps(state), encoding="utf-8")
            with patch.object(runtime, "STATUS_PATH", tmp_path / "status.json"), \
                 patch.object(runtime, "STATE_PATH", state_path), \
                 patch.object(runtime, "DRAFTS_PATH", tmp_path / "drafts.md"), \
                 patch.object(runtime, "STATE_DIR", tmp_path), \
                 patch.object(runtime, "scan_candidates", return_value=[]), \
                 patch.object(runtime, "poll_approvals", return_value=[]), \
                 patch.object(runtime, "send_approval", side_effect=fake_send):
                runtime.run(args)

        self.assertEqual(["c02", "c04", "c03"], order)

    def test_existing_pending_retry_skips_message_ids_terminal_and_failed_status_persists_for_future_retry(self) -> None:
        runtime = import_commentator()
        args = Namespace(dry_run=False, send_telegram=True, publish_approved=False, max_items=20, max_posts=100, scan_posts=True, no_delay=True)
        state = {"items": {
            "retry": self._proposed_item(1, "telegram_config_missing", created_at="2026-07-28T10:00:00-03:00"),
            "already": self._proposed_item(2, "telegram_config_missing", created_at="2026-07-28T11:00:00-03:00", message_id="123"),
            "published": self._proposed_item(3, "telegram_config_missing", created_at="2026-07-28T12:00:00-03:00", decision="published"),
            "rejected": self._proposed_item(4, "telegram_config_missing", created_at="2026-07-28T13:00:00-03:00", decision="rejected"),
        }, "telegram_update_offset": 0}

        def fake_send(candidate, dry_run: bool, send_enabled: bool, callback_token: str | None = None):
            return "telegram_config_missing", None

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            state_path.write_text(runtime.json.dumps(state), encoding="utf-8")
            with patch.object(runtime, "STATUS_PATH", tmp_path / "status.json"), \
                 patch.object(runtime, "STATE_PATH", state_path), \
                 patch.object(runtime, "DRAFTS_PATH", tmp_path / "drafts.md"), \
                 patch.object(runtime, "STATE_DIR", tmp_path), \
                 patch.object(runtime, "scan_candidates", return_value=[]), \
                 patch.object(runtime, "poll_approvals", return_value=[]), \
                 patch.object(runtime, "send_approval", side_effect=fake_send) as send_mock:
                status = runtime.run(args)

            self.assertEqual(0, status["telegram_sent"])
            self.assertEqual(1, send_mock.call_count)
            persisted = runtime.load_json(state_path, {})
            self.assertEqual("telegram_config_missing", persisted["items"]["retry"]["telegram_status"])
            self.assertEqual("123", persisted["items"]["already"]["telegram_message_id"])
            self.assertEqual("published", persisted["items"]["published"]["decision"])
            self.assertEqual("rejected", persisted["items"]["rejected"]["decision"])

    def test_existing_pending_retry_disabled_in_dry_run_or_send_disabled(self) -> None:
        runtime = import_commentator()
        for args in (
            Namespace(dry_run=True, send_telegram=True, publish_approved=False, max_items=20, max_posts=100, scan_posts=True, no_delay=True),
            Namespace(dry_run=False, send_telegram=False, publish_approved=False, max_items=20, max_posts=100, scan_posts=True, no_delay=True),
        ):
            with self.subTest(dry_run=args.dry_run, send_telegram=args.send_telegram):
                state = {"items": {"retry": self._proposed_item(1, "telegram_config_missing", created_at="2026-07-28T10:00:00-03:00")}, "telegram_update_offset": 0}
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    state_path = tmp_path / "state.json"
                    state_path.write_text(runtime.json.dumps(state), encoding="utf-8")
                    with patch.object(runtime, "STATUS_PATH", tmp_path / "status.json"), \
                         patch.object(runtime, "STATE_PATH", state_path), \
                         patch.object(runtime, "DRAFTS_PATH", tmp_path / "drafts.md"), \
                         patch.object(runtime, "STATE_DIR", tmp_path), \
                         patch.object(runtime, "scan_candidates", return_value=[]), \
                         patch.object(runtime, "poll_approvals", return_value=[]), \
                         patch.object(runtime, "send_approval") as send_mock:
                        runtime.run(args)
                    self.assertEqual(0, send_mock.call_count)

    def test_connect_browser_exit11_persists_exact_reason_and_finished_at(self) -> None:
        runtime = import_commentator()
        exact_reason = "cdp_connect_failed:RuntimeError('central browser unavailable')"
        args = Namespace(
            dry_run=True,
            send_telegram=False,
            publish_approved=False,
            max_items=20,
            max_posts=100,
            scan_posts=True,
            no_delay=True,
        )

        class FakeSyncPlaywright:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, tb):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_path = tmp_path / "status.json"
            state_path = tmp_path / "state.json"
            drafts_path = tmp_path / "drafts.md"
            with patch.object(runtime, "STATUS_PATH", status_path), \
                 patch.object(runtime, "STATE_PATH", state_path), \
                 patch.object(runtime, "DRAFTS_PATH", drafts_path), \
                 patch.object(runtime, "STATE_DIR", tmp_path), \
                 patch.object(runtime, "sync_playwright", return_value=FakeSyncPlaywright()), \
                 patch.object(runtime, "connect_browser", return_value=(None, None, None, exact_reason)):
                with self.assertRaises(SystemExit) as raised:
                    runtime.run(args)

            self.assertEqual(11, raised.exception.code)
            persisted = runtime.load_json(status_path, {})
            self.assertEqual(exact_reason, persisted.get("stop_reason"))
            self.assertIn("finished_at", persisted)
            self.assertRegex(persisted["finished_at"], r"^\d{4}-\d{2}-\d{2}T")
            self.assertNotIn("token", str(persisted).lower())
            self.assertNotIn("chat_id", str(persisted).lower())


if __name__ == "__main__":
    unittest.main()
