"""The env snapshot and redaction — the only two things `voice/config.py` does."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from voice import config


class EnvSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        config._ENV_FILE.clear()
        config._LOADED = False

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        config._ENV_FILE.clear()
        config._LOADED = False

    def _write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        return Path(handle.name)

    def test_values_are_read_and_quotes_and_inline_comments_stripped(self):
        path = self._write('AZURE_OPENAI_ENDPOINT="https://example/"  # the endpoint\nVOICE_NAME=alloy\n')
        config.load_env_file(path, force=True)
        self.assertEqual(config.cfg("AZURE_OPENAI_ENDPOINT"), "https://example/")
        self.assertEqual(config.cfg("VOICE_NAME"), "alloy")

    def test_the_real_environment_always_wins_over_the_file(self):
        os.environ["VOICE_NAME"] = "echo"
        path = self._write("VOICE_NAME=alloy\n")
        config.load_env_file(path, force=True)
        self.assertEqual(config.cfg("VOICE_NAME"), "echo")

    def test_harness_control_names_are_refused_from_the_file(self):
        path = self._write("AGENT_DRIVERS_DISABLE=claude\nVOICE_BUTLER_MODE=off\nOK=yes\n")
        config.load_env_file(path, force=True)
        self.assertEqual(config.cfg("AGENT_DRIVERS_DISABLE"), "")
        self.assertEqual(config.cfg("VOICE_BUTLER_MODE"), "")
        self.assertEqual(config.cfg("OK"), "yes")

    def test_a_missing_file_is_not_an_error(self):
        config.load_env_file(Path("/nonexistent/.env"), force=True)
        self.assertEqual(config.cfg("ANYTHING", "fallback"), "fallback")

    def test_comments_and_blank_lines_are_ignored(self):
        path = self._write("# a comment\n\nA=1\n")
        config.load_env_file(path, force=True)
        self.assertEqual(config.cfg("A"), "1")


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        config._ENV_FILE.clear()
        config._LOADED = True          # do not read a real .env during these tests

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        config._ENV_FILE.clear()
        config._LOADED = False

    def test_a_secret_name_redacts_its_value(self):
        self.assertEqual(config.redact("AZURE_OPENAI_API_KEY", "sk-abc123"), "<redacted>")
        self.assertEqual(config.redact("AZURE_OPENAI_ENDPOINT", "https://x/"), "https://x/")

    def test_secret_names_are_recognized_by_marker(self):
        for name in ("AZURE_OPENAI_KEY", "SOME_SECRET", "AUTH_TOKEN", "DB_PASSWORD"):
            self.assertTrue(config.is_secret_name(name), name)
        self.assertFalse(config.is_secret_name("VOICE_NAME"))

    def test_a_secret_value_is_removed_from_free_text(self):
        os.environ["AZURE_OPENAI_API_KEY"] = "sk-verysecretvalue"
        text = "provider said: bad key sk-verysecretvalue for region x"
        self.assertNotIn("sk-verysecretvalue", config.redact_text(text))
        self.assertIn("<redacted>", config.redact_text(text))

    def test_a_short_value_is_not_substring_replaced(self):
        os.environ["API_KEY"] = "abc"
        self.assertEqual(config.redact_text("abcdef is fine"), "abcdef is fine")

    def test_snapshot_redacts_what_it_prints(self):
        os.environ["AZURE_OPENAI_API_KEY"] = "sk-abc123"
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://x/"
        view = config.snapshot(("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"))
        self.assertEqual(view["AZURE_OPENAI_API_KEY"], "<redacted>")
        self.assertEqual(view["AZURE_OPENAI_ENDPOINT"], "https://x/")


if __name__ == "__main__":
    unittest.main()
