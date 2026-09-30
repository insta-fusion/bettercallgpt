"""The composed persona: core + provider agency fragment + backend consent fragment.

The full prompts are kept as golden fixtures (`tests/fixtures/voice-prompt-monolith-*.md`) and the
composition must equal them by LINE SET: moving a rule between fragments is free, changing its
wording means updating the golden file in the same change.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from voice import prompts
from voice.backend import registry as backends
from voice.live import providers

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
PROMPTS = Path(prompts.__file__).resolve().parent


def _rule_lines(text: str) -> list[str]:
    """Lines that carry a rule: not blank, not a heading, not a comment or slot marker."""
    return [line for line in text.split("\n")
            if line.strip() and not line.startswith("#") and not line.startswith("<!--")]


def _fragments() -> list[Path]:
    return [PROMPTS / "core.md", *sorted((PROMPTS / "providers").glob("*.md")),
            *sorted((PROMPTS / "backends").glob("*.md"))]


class Compose(unittest.TestCase):

    def test_every_registered_provider_and_backend_composes(self):
        for provider in providers.names():
            for backend in backends.names():
                with self.subTest(provider=provider, backend=backend):
                    text = prompts.compose(provider, backend)
                    self.assertIn("## 身份、语气", text)
                    self.assertIn("## 授权", text)
                    self.assertNotIn("<!--", text, "no slot marker or comment survives")

    def test_every_named_fragment_exists(self):
        for name in providers.names():
            self.assertTrue((PROMPTS / "providers" / providers.get(name).prompt).is_file(), name)
        for name in backends.names():
            self.assertTrue((PROMPTS / "backends" / backends.get(name).prompt).is_file(), name)

    def test_no_rule_line_is_duplicated_across_fragments(self):
        seen: dict[str, str] = {}
        for path in _fragments():
            for line in _rule_lines(path.read_text(encoding="utf-8")):
                # Alternative fragments of the same kind never compose together, so a line
                # may not repeat between core and a fragment, nor within one file.
                self.assertNotIn(line, seen.get(f"{path.parent.name}:{path.name}", ""))
                owner = seen.get(line)
                if owner is not None:
                    same_kind = owner.split("/")[0] == path.parent.name != "prompts"
                    self.assertTrue(same_kind, f"{line[:40]!r} in {owner} and {path.name}")
                seen[line] = f"{path.parent.name}/{path.name}"

    def test_realtime_with_claude_code_equals_the_old_monolith(self):
        old = (FIXTURES / "voice-prompt-monolith-realtime.md").read_text(encoding="utf-8")
        for provider in ("voice_live", "openai"):
            with self.subTest(provider=provider):
                new = prompts.compose(provider, "claude_code")
                self.assertEqual(set(new.split("\n")), set(old.split("\n")))

    def test_gpt_live_equals_its_old_monolith(self):
        old = (FIXTURES / "voice-prompt-monolith-gpt-live.md").read_text(encoding="utf-8")
        for backend in backends.names():
            with self.subTest(backend=backend):
                new = prompts.compose("gpt_live", backend)
                self.assertEqual(set(new.split("\n")), set(old.split("\n")))

    def test_the_consent_paragraph_follows_backend_and_provider(self):
        dialog = "系统会给你一句确认句"
        terminal = "告诉对方去终端按"
        self.assertIn(dialog, prompts.compose("voice_live", "claude_code"))
        self.assertIn(terminal, prompts.compose("voice_live", "process"))
        # No response identity → no delivery evidence for a spoken challenge.
        self.assertIn(terminal, prompts.compose("gpt_live", "claude_code"))
        self.assertNotIn(dialog, prompts.compose("gpt_live", "claude_code"))

    def test_the_reply_language_follows_the_operator(self):
        for provider in providers.names():
            for backend in backends.names():
                with self.subTest(provider=provider, backend=backend):
                    text = prompts.compose(provider, backend)
                    self.assertIn("语言跟着对方走", text)
                    self.assertIn("一开口先说中文", text)
                    self.assertNotIn("中文为主", text)
                    self.assertNotIn("他", text.replace("其他", ""), "address the operator neutrally")
        # A spoken consent sentence is read verbatim, never translated.
        self.assertIn("不翻译", prompts.compose("voice_live", "claude_code"))

    def test_a_fragment_slot_the_core_lacks_is_refused(self):
        with self.assertRaises(ValueError):
            prompts.compose_text("a\n<!-- slot: x -->\n", "<!-- slot: y -->\nlost\n")


if __name__ == "__main__":
    unittest.main()
