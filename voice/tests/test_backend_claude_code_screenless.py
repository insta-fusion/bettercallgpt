"""claude_code without a screen: ownership from ancestry + the launch in the session's own
transcript, so a plain terminal or the Claude desktop app can host the daemon.

DESIGN.md §Backend split. What changes: no dialog is ever observed (dialogs=False, terminal
consent wording). What does not: the registry identity check and owner-loss fencing."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from voice.backend.claude_code import pane as paneg
from voice.tests.test_backend_claude_code_pane import OWNER, REGISTRY

NONCE = "vvnonce-7c2e91a4"
START = 1_790_000_000.0                      # the owning claude process's start (epoch)
AFTER = "2026-09-21T20:00:00.000Z"          # > START
BEFORE = "2026-09-20T00:00:00.000Z"         # < START


def _transcript(lines: list[dict]) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / "s1.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    return p


def _tool_use(command: str, *, name="Bash", ts=AFTER, sidechain=False, session="s1") -> dict:
    return {"type": "assistant", "timestamp": ts, "isSidechain": sidechain, "sessionId": session,
            "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": name,
                                     "input": {"command": command}}]}}


LAUNCH_CMD = f"NONCE={NONCE} vibe-voice --nonce {NONCE} start"
LAUNCH = _tool_use(LAUNCH_CMD)


def _found(rows, nonce=NONCE):
    return paneg.launch_in_transcript(_transcript(rows), nonce, session_id="s1",
                                      not_before=START)


class LaunchInTranscript(unittest.TestCase):
    """The transcript proves the session's agent ISSUED the launch; what the command does is
    proven by the running process itself (environment + argv), never parsed from shell text."""

    def test_the_launch_is_found(self):
        for cmd in (LAUNCH_CMD,
                    f"cd /repo && NONCE={NONCE} uv run --with x python -m voice.app.daemon "
                    f"--nonce {NONCE} start"):
            self.assertTrue(_found([_tool_use(cmd)]), cmd)

    def test_everything_that_is_not_this_launch_refuses(self):
        chat = {"type": "user", "timestamp": AFTER, "message": {"content": f"run NONCE={NONCE}"}}
        result = {"type": "user", "timestamp": AFTER, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": LAUNCH_CMD}]}}
        cases = {
            "chat line": [chat],
            "tool result": [result],
            "not Bash": [_tool_use(LAUNCH_CMD, name="Task")],
            "sidechain": [_tool_use(LAUNCH_CMD, sidechain=True)],
            "other session": [_tool_use(LAUNCH_CMD, session="s2")],
            "older than the claude process": [_tool_use(LAUNCH_CMD, ts=BEFORE)],
            "no timestamp": [{**LAUNCH, "timestamp": None}],
            "two carriers (ambiguous)": [LAUNCH, _tool_use(f"echo {NONCE}")],
        }
        for why, rows in cases.items():
            self.assertFalse(_found(rows), why)
        # a longer, older nonce that CONTAINS this one is a different nonce
        self.assertTrue(_found([_tool_use(f"echo {NONCE}X"), LAUNCH]))
        # a call from an earlier claude process mentioning the nonce does not compete
        self.assertTrue(_found([_tool_use(f"echo {NONCE}", ts=BEFORE), LAUNCH]))
        self.assertFalse(paneg.launch_in_transcript("/nonexistent/s1.jsonl", NONCE,
                                                    session_id="s1", not_before=START))


class BindWithoutScreen(unittest.TestCase):
    def _bind(self, *, transcript, registry=REGISTRY, owner=OWNER, nonce=NONCE,
              env_nonce=NONCE):
        with mock.patch.object(paneg, "owner_fingerprint", return_value=dict(owner or {})), \
             mock.patch.object(paneg, "registry_epoch", return_value=START), \
             mock.patch.object(paneg, "lstart_epoch", return_value=START):
            return paneg.bind_without_screen(
                nonce=nonce, session_id="s1", ancestor_pid=OWNER["pid"], registry=registry,
                transcript=transcript, ps_timeout=1.0, start_granularity=1.0, now=lambda: 7.0,
                env_nonce=env_nonce)

    def test_bound_with_no_handle_and_no_dialogs(self):
        b = self._bind(transcript=_transcript([LAUNCH]))
        self.assertTrue(b["bound"], b)
        self.assertEqual((b["handle"], b["dialogs"], b["proof"]), ("", False, "transcript"))
        self.assertEqual(b["claude"]["session_id"], "s1")
        self.assertEqual(b["bound_at"], 7.0)

    def test_every_missing_proof_refuses(self):
        t = _transcript([LAUNCH])
        self.assertEqual(self._bind(transcript=t, nonce="bad nonce")["refusal"],
                         paneg.REFUSE_BAD_NONCE)
        self.assertEqual(self._bind(transcript=t, owner={})["refusal"], paneg.REFUSE_NO_OWNER)
        self.assertEqual(self._bind(transcript=t, registry={**REGISTRY, "sessionId": "x"})
                         ["refusal"], paneg.REFUSE_REGISTRY)
        self.assertEqual(self._bind(transcript=_transcript([]))["refusal"],
                         paneg.REFUSE_NO_LAUNCH)
        # this process was not started by a NONCE=<n> command: not our launch
        for env in (None, "", "vvnonce-other0001"):
            self.assertEqual(self._bind(transcript=t, env_nonce=env)["refusal"],
                             paneg.REFUSE_NONCE_NOT_OURS, env)


class RealClock(unittest.TestCase):
    def test_a_real_format_timestamp_against_the_real_registry_start(self):
        # No mocked epochs: procStart as the registry writes it, the row as Claude Code does.
        start = paneg.registry_epoch(REGISTRY["procStart"])
        after = time.strftime("%Y-%m-%dT%H:%M:%S.123Z", time.gmtime(start + 30))
        before = time.strftime("%Y-%m-%dT%H:%M:%S.123Z", time.gmtime(start - 3600))
        for ts, want in ((after, True), (before, False)):
            row = _tool_use(LAUNCH_CMD, ts=ts)
            self.assertEqual(paneg.launch_in_transcript(
                _transcript([row]), NONCE, session_id="s1", not_before=start), want, ts)


class OwnerFingerprintParsesRealRows(unittest.TestCase):
    """`ps -o pid=,lstart=,tty=,comm=` rows as macOS prints them (captured 2026-09-24)."""

    def test_desktop_app_claude_path_with_spaces(self):
        row = ("66229 Thu Sep 24 02:06:41 2026     ??       /Volumes/Mac HD/Library/Application "
               "Support/Claude/claude-code/2.1.280/claude.app/Contents/MacOS/claude\n")
        fp = paneg.owner_fingerprint(66229, ps_timeout=1, ps_runner=lambda argv: row)
        self.assertEqual(fp["start"], "Thu Sep 24 02:06:41 2026")
        self.assertEqual(fp["tty"], "??")
        self.assertTrue(fp["comm"].endswith("/MacOS/claude"))
        self.assertIsNotNone(paneg.lstart_epoch(fp["start"]))

    def test_cli_claude_row(self):
        row = "10135 Wed Sep 23 18:34:24 2026     ttys003  claude\n"
        fp = paneg.owner_fingerprint(10135, ps_timeout=1, ps_runner=lambda argv: row)
        self.assertEqual((fp["start"], fp["tty"], fp["comm"]),
                         ("Wed Sep 23 18:34:24 2026", "ttys003", "claude"))

    def test_ps_runs_in_the_c_locale(self):
        # zh_CN prints lstart as "三  9月/23 18:34:24 2026": four tokens, unparseable.
        seen = {}

        def run(argv, **kw):
            seen.update(kw.get("env") or {})
            return mock.Mock(stdout="")
        with mock.patch.dict("os.environ", {"LC_ALL": "zh_CN.UTF-8", "LANG": "zh_CN.UTF-8"}), \
             mock.patch.object(paneg.subprocess, "run", side_effect=run), \
             mock.patch.object(paneg.shutil, "which", return_value="/bin/ps"):
            paneg.owner_fingerprint(1, ps_timeout=1)
        self.assertEqual(seen.get("LC_ALL"), "C")

    def test_a_short_row_is_no_fingerprint(self):
        self.assertEqual(paneg.owner_fingerprint(1, ps_timeout=1,
                                                 ps_runner=lambda argv: "1 Thu Sep\n"), {})


class ScreenlessPaneObserves(unittest.TestCase):
    def test_owner_alive_means_ok_and_never_a_dialog(self):
        binding = {"owner": dict(OWNER)}
        with mock.patch.object(paneg, "owner_fingerprint", return_value=dict(OWNER)):
            r = asyncio.run(paneg.ScreenlessPane(ps_timeout=1.0).observe(binding=binding))
        self.assertEqual(r, {"ok": True, "lost": None, "classification": None})

    def test_owner_gone_is_fenced(self):
        with mock.patch.object(paneg, "owner_fingerprint", return_value={}):
            r = asyncio.run(paneg.ScreenlessPane(ps_timeout=1.0).observe(
                binding={"owner": dict(OWNER)}))
        self.assertEqual(r["lost"], paneg.LOST_PID_GONE)


class PromptFollowsTheBinding(unittest.TestCase):
    def test_no_dialogs_means_terminal_consent_wording(self):
        from voice.prompts import compose, read
        with_screen = compose("voice_live", "claude_code")
        without = compose("voice_live", "claude_code", dialogs=False)
        self.assertNotEqual(with_screen, without)
        terminal_line = [l for l in read("backends/terminal.md").splitlines()
                         if l and not l.startswith("<!--")][0]
        self.assertIn(terminal_line, without)
        self.assertNotIn(terminal_line, with_screen)


class HandshakeChoosesTheProof(unittest.TestCase):
    def test_no_terminal_uses_the_transcript_proof_and_each_nonce_binds_once(self):
        import argparse
        import os
        from voice.app import daemon
        t = _transcript([LAUNCH])
        args = argparse.Namespace(nonce=NONCE, terminal="")
        with tempfile.TemporaryDirectory() as state, \
             mock.patch.dict(os.environ, {"VOICE_LISTEN_STATE_DIR": state, "NONCE": NONCE}), \
             mock.patch("pathlib.Path.home", return_value=Path(state)), \
             mock.patch.object(daemon, "find_claude_ancestor", return_value=OWNER["pid"]), \
             mock.patch.object(daemon, "resolve_transcript", return_value=t), \
             mock.patch.object(paneg, "session_registry", return_value=dict(REGISTRY)), \
             mock.patch.object(paneg, "owner_fingerprint", return_value=dict(OWNER)), \
             mock.patch.object(paneg, "registry_epoch", return_value=START), \
             mock.patch.object(paneg, "lstart_epoch", return_value=START):
            proof = asyncio.run(daemon._handshake("s1", args))
            self.assertIsInstance(proof["pane"], paneg.ScreenlessPane)
            self.assertTrue(proof["binding"]["bound"])
            self.assertFalse(proof["binding"]["dialogs"])
            with self.assertRaisesRegex(RuntimeError, paneg.REFUSE_NONCE_USED):   # replay
                asyncio.run(daemon._handshake("s1", args))
            self.assertTrue((Path(state) / ".local" / "state" / "voice-launch-claims" / "s1"
                             / NONCE).is_file())
            # another launcher / state root cannot reuse the nonce: the claim root is fixed
            with mock.patch.dict(os.environ, {"VOICE_LISTEN_STATE_DIR": state + "-other"}):
                with self.assertRaisesRegex(RuntimeError, paneg.REFUSE_NONCE_USED):
                    asyncio.run(daemon._handshake("s1", args))
            # a refused launch never blocks a fresh one
            fresh = "vvnonce-fresh0001"
            t.write_text(t.read_text() + json.dumps(_tool_use(
                f"NONCE={fresh} vibe-voice --nonce {fresh} start")) + "\n")
            with mock.patch.dict(os.environ, {"NONCE": fresh}):
                again = asyncio.run(daemon._handshake("s1", argparse.Namespace(nonce=fresh,
                                                                                terminal="")))
            self.assertTrue(again["binding"]["bound"])
            # a claim that cannot be recorded refuses (never binds, never a traceback)
            other = "vvnonce-unrec0001"
            t.write_text(t.read_text() + json.dumps(_tool_use(
                f"NONCE={other} vibe-voice --nonce {other} start")) + "\n")
            with mock.patch.object(daemon.voice_platform, "private_dir",
                                   side_effect=PermissionError("denied")), \
                 mock.patch.dict(os.environ, {"NONCE": other}):
                with self.assertRaisesRegex(RuntimeError, paneg.REFUSE_NONCE_UNRECORDABLE):
                    asyncio.run(daemon._handshake("s1", argparse.Namespace(nonce=other,
                                                                            terminal="")))


class Hygiene(unittest.TestCase):
    def test_overlapping_secrets_mask_the_longer_one_whole(self):
        import os
        from voice import config as voice_config
        env = {"A_API_KEY": "sk-shared-prefix", "B_API_KEY": "sk-shared-prefix-sensitive-tail"}
        with mock.patch.dict(os.environ, env):
            out = voice_config.redact_text("x sk-shared-prefix-sensitive-tail y")
        self.assertNotIn("sensitive-tail", out)

    def test_a_dialog_is_masked_before_it_is_clipped(self):
        import os
        secret = "sk-dialog-SECRET-0123456789"
        with mock.patch.dict(os.environ, {"X_API_KEY": secret}):
            record = paneg.dialog_identity("q" * 390 + " " + secret,
                                           options=((1, "Yes"), (2, "No")), previous=None)
        self.assertNotIn(secret[:6], record["question"])     # no unmaskable prefix survives

    def test_missing_parents_of_the_state_dir_are_private(self):
        import os
        import stat
        from voice import platform as voice_platform
        if os.name == "nt":
            self.skipTest("POSIX modes")
        with tempfile.TemporaryDirectory() as d:
            leaf = voice_platform.private_dir(Path(d) / "root" / "session")
            self.assertEqual(stat.S_IMODE(os.stat(leaf.parent).st_mode), 0o700)

    def test_preflight_without_terminal_says_no_dialogs(self):
        import argparse
        import contextlib
        import io
        from voice.app import daemon
        out = io.StringIO()
        args = argparse.Namespace(backend="claude_code", terminal="", pretty=False)
        with mock.patch.object(daemon.voice_platform, "PLATFORM", "darwin"), \
             contextlib.redirect_stdout(out):
            daemon.cmd_preflight(args)
        report = json.loads(out.getvalue())
        self.assertFalse(report["backend"]["dialogs"])


if __name__ == "__main__":
    unittest.main()
