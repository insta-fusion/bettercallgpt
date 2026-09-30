"""Open-source hygiene guards: secrets never leave through the voice or the ledger, the state
directory is private, and the offline suite never spends a live model turn by itself."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice import platform as voice_platform
from voice.agent.ledger import Ledger
from voice.backend import base as backend_base
from voice.live.port import RedactedVoice
from voice.tests.test_core_loop import make_loop

SECRET = "sk-test-SECRET-0123456789abcdef"
ENV = {"VOICE_TEST_API_KEY": SECRET}


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class LedgerMasksAndIsPrivate(unittest.TestCase):
    def test_every_string_field_is_masked(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ENV):
            led = Ledger(str(Path(d) / "ledger.jsonl"), frozenset())
            asyncio.run(led.append({"kind": "observed", "text": f"key={SECRET}",
                                    "nested": {"list": [SECRET, 3]}}))
            led.close()
            raw = (Path(d) / "ledger.jsonl").read_text()
        self.assertNotIn(SECRET, raw)
        self.assertEqual(json.loads(raw.splitlines()[-1])["nested"]["list"][1], 3)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_ledger_is_owner_only_and_an_old_one_is_tightened(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ledger.jsonl"
            path.write_text("")
            os.chmod(path, 0o644)                       # as an older build left it
            Ledger(str(path), frozenset()).close()
            self.assertEqual(_mode(path), 0o600)


@unittest.skipIf(os.name == "nt", "POSIX file modes")
class StateDirIsPrivate(unittest.TestCase):
    def test_new_and_existing_directories(self):
        with tempfile.TemporaryDirectory() as d:
            fresh = voice_platform.private_dir(Path(d) / "a" / "session")
            self.assertEqual(_mode(fresh), 0o700)
            old = Path(d) / "old"
            old.mkdir(mode=0o755)
            os.chmod(old, 0o755)
            voice_platform.private_dir(old)
            self.assertEqual(_mode(old), 0o700)


class _Recorder:
    capabilities = "caps"

    def __init__(self):
        self.calls = []

    async def context(self, text):
        self.calls.append(("context", text))

    async def announce(self, text, origin):
        self.calls.append(("announce", text))

    async def challenge(self, text):
        self.calls.append(("challenge", text))

    async def receipt(self, call_id, output, *, speak):
        self.calls.append(("receipt", json.dumps(output)))

    async def result(self, text, *, answers):
        self.calls.append(("result", text))

    async def cut(self, response_id):
        self.calls.append(("cut", response_id))


class VoiceIsRedacted(unittest.TestCase):
    def test_every_intent_is_masked_and_capabilities_pass_through(self):
        inner = _Recorder()
        voice = RedactedVoice(inner)
        self.assertEqual(voice.capabilities, "caps")

        async def go():
            await voice.context(f"progress {SECRET}")
            await voice.announce(f"hello {SECRET}", "narration")
            await voice.challenge(f"say {SECRET}")
            await voice.receipt("c1", {"note": SECRET, "n": 1}, speak=False)
            await voice.result(f"done {SECRET}", answers=None)
            await voice.cut("r1")

        with mock.patch.dict(os.environ, ENV):
            asyncio.run(go())
        self.assertEqual(len(inner.calls), 6)
        for kind, text in inner.calls:
            self.assertNotIn(SECRET, text, kind)


class LoopRedactsBeforeClipping(unittest.TestCase):
    def test_a_secret_on_the_clip_boundary_leaves_no_prefix(self):
        loop, _session, _backend, ledger, _sink = make_loop()
        text = "x" * 110 + SECRET                      # the 120-char clip cuts the secret
        with mock.patch.dict(os.environ, ENV):
            asyncio.run(loop.handle_observation(backend_base.Observation(
                kind=backend_base.OBS_PROGRESS, turn_id="t1", text=text)))
        observed = [r for r in ledger.appended if r.get("kind") == "observed"][0]
        self.assertNotIn(SECRET[:8], observed["text"])


class OperatorErrorsAreRedacted(unittest.TestCase):
    def test_a_secret_pasted_as_the_provider_never_prints(self):
        from voice.app import daemon
        with mock.patch.dict(os.environ, ENV):
            with self.assertRaises(daemon.MissingCredentials) as cm:
                daemon.check_credentials(SECRET)
        self.assertNotIn(SECRET, str(cm.exception))


class OfflineSuiteNeverSpends(unittest.TestCase):
    def test_codex_smoke_is_opt_in(self):
        from voice.tests import test_backend_process as tbp
        with mock.patch.object(tbp, "LIVE_TESTS", False), \
             mock.patch.object(tbp.subprocess, "run",
                               side_effect=AssertionError("probed codex without opt-in")):
            self.assertFalse(tbp.codex_available())


class NoPrivatePointers(unittest.TestCase):
    def test_voice_names_no_private_path_or_design_doc(self):
        import re
        root = Path(__file__).resolve().parents[1]
        bad = re.compile(r"~/Work/|/Users/[a-z]|runs/\d{4}-|\bPLAN\.md|ACCEPTANCE\.md|"
                         r"BACKEND-SPLIT|SCENARIOS-FINAL")
        hits = [f"{p.relative_to(root)}:{n}" for p in root.rglob("*")
                if p.suffix in (".py", ".md") and p.is_file()
                for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                if bad.search(line) and p.name != "test_oss_hygiene.py"]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
