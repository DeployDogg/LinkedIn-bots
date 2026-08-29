"""Local-only production-readiness tests for Commentator.

These tests must never open a browser, call Telegram, or publish LinkedIn content.
"""
from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import tempfile
import types
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


def args(**overrides):
    values = dict(
        dry_run=True,
        send_telegram=False,
        publish_approved=False,
        max_items=2,
        max_posts=2,
        scan_posts=True,
        no_delay=True,
    )
    values.update(overrides)
    return Namespace(**values)


class CommentatorProductionReadinessTest(unittest.TestCase):
    def test_pause_all_env_exits_before_state_browser_or_telegram(self) -> None:
        runtime = import_commentator()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_PAUSE_ALL": "1"}, clear=False), \
                 patch.object(runtime, "STATE_DIR", base), \
                 patch.object(runtime, "STATUS_PATH", base / "status.json"), \
                 patch.object(runtime, "STATE_PATH", base / "state.json"), \
                 patch.object(runtime, "DRAFTS_PATH", base / "drafts.md"), \
                 patch.object(runtime, "scan_candidates") as scan, \
                 patch.object(runtime, "poll_approvals") as poll, \
                 patch.object(runtime, "publish_reply_for_item") as publish:
                status = runtime.run(args(dry_run=False, send_telegram=True, publish_approved=True))

            self.assertEqual("paused_all", status["stop_reason"])
            self.assertTrue(status["pause_all"])
            scan.assert_not_called()
            poll.assert_not_called()
            publish.assert_not_called()

    def test_pause_all_file_exits_before_browser(self) -> None:
        runtime = import_commentator()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "PAUSE_ALL").write_text("pause\n", encoding="utf-8")
            with patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_PAUSE_ALL": "0"}, clear=False), \
                 patch.object(runtime, "STATE_DIR", base), \
                 patch.object(runtime, "STATUS_PATH", base / "status.json"), \
                 patch.object(runtime, "STATE_PATH", base / "state.json"), \
                 patch.object(runtime, "DRAFTS_PATH", base / "drafts.md"), \
                 patch.object(runtime, "scan_candidates") as scan:
                status = runtime.run(args())
            self.assertEqual("paused_all_file", status["stop_reason"])
            scan.assert_not_called()

    def test_pause_publishing_env_and_file_block_defensively_even_when_approved(self) -> None:
        runtime = import_commentator()
        approved = {"id": "c1", "decision": "approved", "post_url": "https://example.invalid", "reply": "reply"}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.object(runtime, "STATE_DIR", base), \
                 patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_PAUSE_PUBLISHING": "1"}, clear=False), \
                 patch.object(runtime, "sync_playwright") as browser:
                ok, reason = runtime.publish_reply_for_item(approved, args(dry_run=False, publish_approved=True))
            self.assertFalse(ok)
            self.assertEqual("publishing_paused_env", reason)
            browser.assert_not_called()

            (base / "PAUSE_PUBLISHING").write_text("pause\n", encoding="utf-8")
            with patch.object(runtime, "STATE_DIR", base), \
                 patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_PAUSE_PUBLISHING": "0"}, clear=False), \
                 patch.object(runtime, "sync_playwright") as browser:
                ok, reason = runtime.publish_reply_for_item(approved, args(dry_run=False, publish_approved=True))
            self.assertFalse(ok)
            self.assertEqual("publishing_paused_file", reason)
            browser.assert_not_called()

    def test_atomic_save_keeps_backup_and_corrupt_production_state_fails_closed(self) -> None:
        runtime = import_commentator()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = base / "state.json"
            old = {"items": {"old": {"decision": "published"}}}
            new = {"items": {"new": {"decision": "proposed"}}}
            path.write_text(json.dumps(old), encoding="utf-8")
            with patch.object(runtime, "STATE_DIR", base):
                runtime.save_json(path, new, backup=True)
            self.assertEqual(new, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(old, json.loads((base / "state.json.bak").read_text(encoding="utf-8")))
            self.assertFalse((base / "state.json.tmp").exists())

            path.write_text("{truncated", encoding="utf-8")
            with patch.object(runtime, "STATE_PATH", path):
                with self.assertRaises(runtime.StateCorruptionError):
                    runtime.load_state()
                with self.assertRaises(runtime.StateCorruptionError):
                    runtime.load_json(path, {"items": {}})
            self.assertEqual("{truncated", path.read_text(encoding="utf-8"))

    def test_state_normalization_preserves_terminal_and_legacy_history_without_rekeying(self) -> None:
        runtime = import_commentator()
        state = {"items": {
            "legacy-published-key": {
                "id": "legacy-id",
                "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:123/",
                "author": "Jane Doe",
                "comment_text": "Useful substantive comment",
                "published": True,
                "published_at": "2026-01-01T00:00:00-03:00",
                "sent_to_telegram": True,
            },
            "legacy-rejected-key": {
                "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:456/",
                "author": "John Doe",
                "comment_text": "Another substantive comment",
                "rejected": True,
            },
        }}
        normalized, changed = runtime.normalize_state(state)
        self.assertTrue(changed)
        self.assertEqual(set(state["items"]), set(normalized["items"]))
        self.assertEqual("published", normalized["items"]["legacy-published-key"]["decision"])
        self.assertEqual("rejected", normalized["items"]["legacy-rejected-key"]["decision"])
        self.assertTrue(normalized["items"]["legacy-published-key"]["sent_to_telegram"])
        self.assertTrue(normalized["items"]["legacy-published-key"]["comment_key"])
        self.assertIsInstance(normalized["items"]["legacy-published-key"]["source_seen"], list)
        self.assertNotIn("comment_key", state["items"]["legacy-published-key"])

    def test_preflight_is_machine_readable_secret_safe_and_tg_only_hard_when_requested(self) -> None:
        runtime = import_commentator()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state = base / "state.json"
            state.write_text(json.dumps({"items": {}}), encoding="utf-8")
            command = base / "codex-local"
            command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            command.chmod(command.stat().st_mode | stat.S_IXUSR)
            cron = base / "crontab"
            cron.write_text("17 10 * * * /app/scripts/run.sh\n", encoding="utf-8")
            secret_token = "TOKEN_MUST_NOT_LEAK_123"
            secret_chat = "CHAT_MUST_NOT_LEAK_456"
            env = {
                "LINKEDIN_COMMENTATOR_CODEX_COMMAND": str(command),
                "LINKEDIN_CDP_ENDPOINT": "http://linkedin-browser:9222",
                "TELEGRAM_BOT_TOKEN": secret_token,
                "TELEGRAM_CHAT_ID": secret_chat,
                "LINKEDIN_COMMENTATOR_CRONTAB_PATH": str(cron),
            }
            with patch.dict(os.environ, env, clear=False), \
                 patch.object(runtime, "STATE_DIR", base), \
                 patch.object(runtime, "STATE_PATH", state), \
                 patch.object(runtime, "STATUS_PATH", base / "status.json"), \
                 patch.object(runtime, "DRAFTS_PATH", base / "drafts.md"), \
                 patch.object(runtime, "SCREENSHOT_DIR", base / "screens"), \
                 patch.object(runtime, "TG_ENV_PATH", base / "missing-telegram.env"):
                report = runtime.production_preflight(args(send_telegram=False))
            encoded = json.dumps(report, sort_keys=True)
            self.assertTrue(report["ready"])
            self.assertTrue(report["telegram"]["token_present"])
            self.assertTrue(report["telegram"]["chat_id_present"])
            self.assertNotIn(secret_token, encoded)
            self.assertNotIn(secret_chat, encoded)

            with patch.dict(os.environ, {**env, "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False), \
                 patch.object(runtime, "STATE_DIR", base), patch.object(runtime, "STATE_PATH", state), \
                 patch.object(runtime, "STATUS_PATH", base / "status.json"), patch.object(runtime, "DRAFTS_PATH", base / "drafts.md"), \
                 patch.object(runtime, "SCREENSHOT_DIR", base / "screens"), patch.object(runtime, "TG_ENV_PATH", base / "missing-telegram.env"):
                disabled = runtime.production_preflight(args(send_telegram=False))
                requested = runtime.production_preflight(args(send_telegram=True))
            self.assertTrue(disabled["ready"])
            self.assertFalse(requested["ready"])

    def test_alert_artifact_is_atomic_for_blocker_and_state_corruption(self) -> None:
        runtime = import_commentator()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            alert_path = base / "linkedin_commentator_alert.json"
            with patch.object(runtime, "STATE_DIR", base), patch.object(runtime, "ALERT_PATH", alert_path):
                runtime.write_alert("linkedin_blocker", "captcha")
            alert = json.loads(alert_path.read_text(encoding="utf-8"))
            self.assertEqual("linkedin_blocker", alert["category"])
            self.assertEqual("captcha", alert["reason"])
            self.assertFalse((base / "linkedin_commentator_alert.json.tmp").exists())

    def test_corrupt_state_run_fails_closed_and_writes_alert_without_browser(self) -> None:
        runtime = import_commentator()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_path = base / "state.json"
            alert_path = base / "alert.json"
            state_path.write_text("{broken", encoding="utf-8")
            with patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_PAUSE_ALL": "0"}, clear=False), \
                 patch.object(runtime, "STATE_DIR", base), patch.object(runtime, "CONTROL_DIR", base / "control"), \
                 patch.object(runtime, "STATE_PATH", state_path), patch.object(runtime, "STATUS_PATH", base / "status.json"), \
                 patch.object(runtime, "DRAFTS_PATH", base / "drafts.md"), patch.object(runtime, "ALERT_PATH", alert_path), \
                 patch.object(runtime, "scan_candidates") as scan:
                with self.assertRaises(SystemExit) as raised:
                    runtime.run(args())
            self.assertEqual(13, raised.exception.code)
            scan.assert_not_called()
            self.assertEqual("state_corruption", json.loads(alert_path.read_text(encoding="utf-8"))["category"])
            self.assertEqual("{broken", state_path.read_text(encoding="utf-8"))

    def test_run_pause_publishing_keeps_approved_item_and_never_calls_publish(self) -> None:
        runtime = import_commentator()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_path = base / "state.json"
            state_path.write_text(json.dumps({"items": {"approved": {"id": "approved", "comment_key": "approved", "decision": "approved", "source_seen": []}}}), encoding="utf-8")
            with patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_PAUSE_PUBLISHING": "1", "LINKEDIN_COMMENTATOR_PAUSE_ALL": "0"}, clear=False), \
                 patch.object(runtime, "STATE_DIR", base), patch.object(runtime, "CONTROL_DIR", base / "control"), \
                 patch.object(runtime, "STATE_PATH", state_path), patch.object(runtime, "STATUS_PATH", base / "status.json"), \
                 patch.object(runtime, "DRAFTS_PATH", base / "drafts.md"), patch.object(runtime, "ALERT_PATH", base / "alert.json"), \
                 patch.object(runtime, "scan_candidates", return_value=[]), patch.object(runtime, "poll_approvals", return_value=[]), \
                 patch.object(runtime, "publish_reply_for_item") as publish:
                status = runtime.run(args(dry_run=False, publish_approved=True))
            publish.assert_not_called()
            self.assertEqual("approved", json.loads(state_path.read_text(encoding="utf-8"))["items"]["approved"]["decision"])
            self.assertEqual("publishing_paused_env", status["publish_skipped"][0]["reason"])

    def test_health_summary_has_required_operational_fields(self) -> None:
        runtime = import_commentator()
        state = {"items": {
            "p": {"decision": "proposed", "reason": "x;generator=codex", "telegram_status": "not_sent_dry_run_or_disabled"},
            "a": {"decision": "approved"},
            "r": {"decision": "rejected"},
            "m": {"decision": "publish_failed", "requires_manual_review": True},
            "done": {"decision": "published", "published_at": "2026-01-01T00:00:00-03:00"},
        }}
        status = {
            "candidates_new": 1, "telegram_sent": 0, "published": 0, "stop_reason": None,
            "skipped": [{"reason": "duplicate_report_signature"}, {"reason": "short_confirmation"}],
            "generator_counts": {"codex": 1, "fallback": 0}, "codex_failures": 0,
        }
        summary = runtime.build_health_summary(state, status, previous_status={"last_success_at": "2025-01-01T00:00:00-03:00"})
        for key in ("last_success_at", "generator_counts", "codex_failures", "candidates_new", "telegram_sent", "published", "stop_reason", "state_machine_counts", "duplicate_skips", "pending_approvals", "manual_review"):
            self.assertIn(key, summary)
        self.assertEqual(1, summary["pending_approvals"])
        self.assertEqual(1, summary["manual_review"])
        self.assertEqual(1, summary["duplicate_skips"])


if __name__ == "__main__":
    unittest.main()
