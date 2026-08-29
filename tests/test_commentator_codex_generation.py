"""Unit tests for Commentator Codex reply generation.

No real Codex, browser, Telegram, or LinkedIn calls.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import textwrap
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


class CommentatorCodexGenerationTest(unittest.TestCase):
    def make_script(self, body: str) -> str:
        fd, path = tempfile.mkstemp(prefix="fake-codex-", suffix=".sh")
        os.close(fd)
        Path(path).write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
        os.chmod(path, 0o755)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        return path

    def test_fake_command_reply_returns_codex_generator(self) -> None:
        runtime = import_commentator()
        script = self.make_script("cat >/dev/null\nprintf '%s\\n' 'Согласен, тут важна честная граница роли.'\n")

        with patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_CODEX_COMMAND": script}, clear=False):
            reply, source = runtime.codex_reply("Ivan", "Что думаешь про DevOps и код?", "post", "question")

        self.assertEqual("Согласен, тут важна честная граница роли.", reply)
        self.assertEqual("codex", source)

    def test_fake_command_exit_1_falls_back_with_error_class(self) -> None:
        runtime = import_commentator()
        script = self.make_script("cat >/dev/null\necho 'boom with maybe-secret-token-1234567890abcdef' >&2\nexit 1\n")

        with patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_CODEX_COMMAND": script}, clear=False):
            reply, source = runtime.codex_reply("Ivan", "Что думаешь?", "post", "question")

        self.assertIn("хороший вопрос", reply)
        self.assertEqual("fallback_codex_failed:exit_1", source)

    def test_empty_output_has_specific_fallback_reason(self) -> None:
        runtime = import_commentator()
        script = self.make_script("cat >/dev/null\n")

        with patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_CODEX_COMMAND": script}, clear=False):
            _reply, source = runtime.codex_reply("Ivan", "Что думаешь?", "post", "question")

        self.assertEqual("fallback_codex_empty", source)

    def test_timeout_has_specific_fallback_reason(self) -> None:
        runtime = import_commentator()
        script = self.make_script("cat >/dev/null\nsleep 2\nprintf 'late'\n")

        with patch.dict(
            os.environ,
            {"LINKEDIN_COMMENTATOR_CODEX_COMMAND": script, "LINKEDIN_COMMENTATOR_CODEX_TIMEOUT_SECONDS": "1"},
            clear=False,
        ):
            _reply, source = runtime.codex_reply("Ivan", "Что думаешь?", "post", "question")

        self.assertEqual("fallback_codex_timeout", source)

    def test_explanatory_output_is_sanitized_to_final_comment(self) -> None:
        runtime = import_commentator()
        script = self.make_script(
            "cat >/dev/null\n"
            + "cat <<'OUT'\n"
            + "Конечно, вот короткий комментарий:\n\"Да, тут я бы сначала договорился о границах роли, а потом уже спорил про инструменты.\"\nOUT\n"
        )

        with patch.dict(os.environ, {"LINKEDIN_COMMENTATOR_CODEX_COMMAND": script}, clear=False):
            reply, source = runtime.codex_reply("Ivan", "Что думаешь?", "post", "question")

        self.assertEqual("codex", source)
        self.assertEqual("Да, тут я бы сначала договорился о границах роли, а потом уже спорил про инструменты.", reply)


if __name__ == "__main__":
    unittest.main()
