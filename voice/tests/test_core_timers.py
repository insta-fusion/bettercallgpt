"""DESIGN.md §Acceptance B3 — no timer participates in a semantic decision.

This is a static test because the property is static: a grep that finds a duration constant,
a sleep, a clock read or an unbounded-turned-bounded wait in the decision path is a design
violation whether or not any test exercises it.

**Exemption is by MARKER, not by path.** An earlier version exempted the whole of
`voice/app/`, which meant the ask session, the MCP surface and every future module under
that directory could grow a semantic timer unseen — and two of them had. Now a file is
scanned unless the line itself sits inside a region opened by the marker comment below, so
adding a bound anywhere means writing down, in the code, that it is a lifecycle port.

    # b3: lifecycle-ports begin
    ...declared bounds...
    # b3: lifecycle-ports end

The permitted bounds (DESIGN.md §Timers) are the connection open/close/reconnect bounds, the
AudioLock bound and its retry cadence, and one session-liveness bound that may only trigger
a reconnect. A bound that belongs to the OPERATOR (`VOICE_SESSION_MAX_MINUTES`) is not this
layer's to declare: it arrives injected, with no default in any module body. The one exception
is the idle close (`VOICE_IDLE_MINUTES`), whose default lives inside the daemon's marker
region because an unset default is what would leave a forgotten session running.
"""
from __future__ import annotations

import pathlib
import re
import unittest

VOICE = pathlib.Path(__file__).resolve().parent.parent

# Tests may sleep and may hold durations: they decide nothing and drive no microphone.
EXEMPT = (VOICE / "tests",)

MARKER_BEGIN = re.compile(r"#\s*b3:\s*lifecycle-ports begin")
MARKER_END = re.compile(r"#\s*b3:\s*lifecycle-ports end")

# One region exemption survives as a marker too: `audio/io.py` holds BOTH the audio epoch
# (a decision path, scanned) and the AudioLock, whose cadence the caller now injects.

FORBIDDEN = (
    (re.compile(r"^\s*_?[A-Z][A-Z0-9_]*_S\s*="), "a duration constant (`*_S =`)"),
    # A lowercase `_s` DEFAULT in a signature is the same thing wearing a parameter's
    # clothes: `def f(*, bound_s: float = 3.0)` declares a duration this module chose.
    (re.compile(r"[a-z_]+_s\s*:\s*float\s*=\s*[0-9]"), "a defaulted duration parameter"),
    (re.compile(r"[a-z_]+_s\s*=\s*[0-9]+\.?[0-9]*\s*[,)]"), "a defaulted duration parameter"),
    (re.compile(r"\bsleep\s*\("), "a sleep"),
    (re.compile(r"\bwait_for\s*\("), "a bounded wait (`wait_for`)"),
    (re.compile(r"\basyncio\.wait\s*\([^)]*timeout\s*="), "a bounded wait (`asyncio.wait`)"),
    (re.compile(r"\btimeout\s*=\s*[0-9]"), "a literal timeout"),
    (re.compile(r"\btime\.time\s*\("), "a wall clock read"),
    (re.compile(r"\bmonotonic\s*\("), "a monotonic clock read"),
    # `*_SECONDS = 240` is `*_S = 240` spelled out; the first version matched only the
    # abbreviation, so a renamed constant walked straight through.
    # `[1-9]` not `[0-9]`: zero is not a duration, it is "no value" — the only number a
    # module may hold about time, because it names an absence rather than a length.
    (re.compile(r"^\s*_?[A-Z][A-Z0-9_]*_(SECONDS|SEC|MS)\s*=\s*(?!0\b)[0-9]"),
     "a duration constant (`*_SECONDS =`)"),
    # A defaulted duration in a SIGNATURE — `max_seconds: int = 240` — is the same literal
    # wearing a parameter's clothes, and it is how the MCP default became invisible.
    (re.compile(r"[a-z_]*(seconds|sec|_s|_ms)\s*:\s*(int|float)\s*=\s*(?!0\b)[0-9]"),
     "a defaulted duration parameter"),
    # A clamp on a duration name is this layer altering someone else's number in silence.
    # Either order: `max(30, min(900, max_seconds))` puts the literals first, which a
    # pattern anchored on the NAME misses entirely.
    (re.compile(r"\b(min|max)\s*\((?=[^)]*[a-z_]*(?:seconds|_s|sec)\b)[^)]*[0-9]"),
     "a clamp literal on a duration"),
)


def _is_exempt(path: pathlib.Path) -> bool:
    return any(path == exempt or exempt in path.parents for exempt in EXEMPT)


def _scanned_files():
    for path in sorted(VOICE.rglob("*.py")):
        if "__pycache__" in path.parts or _is_exempt(path):
            continue
        yield path


def scan(path: pathlib.Path) -> list[str]:
    """Every forbidden construct outside a declared lifecycle-ports region."""
    hits: list[str] = []
    inside = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if MARKER_BEGIN.search(line):
            inside = True
            continue
        if MARKER_END.search(line):
            inside = False
            continue
        if inside:
            continue
        code = line.split("#", 1)[0]
        for pattern, description in FORBIDDEN:
            if pattern.search(code):
                hits.append(f"{path.relative_to(VOICE)}:{number}: {description} — {line.strip()}")
    return hits


class B3NoTimersTests(unittest.TestCase):
    def test_B3_no_timers(self):
        hits: list[str] = []
        for path in _scanned_files():
            hits.extend(scan(path))
        self.assertEqual(hits, [], "timers in the decision path:\n" + "\n".join(hits))

    def test_B3_the_scan_actually_covers_every_production_module(self):
        """A grep test that greps nothing passes for the wrong reason.

        `voice/app/` is listed explicitly because it used to be exempt wholesale, which is
        how a bounded wait and two invented defaults reached a live microphone unseen.
        """
        scanned = {path.relative_to(VOICE).as_posix() for path in _scanned_files()}
        for required in ("agent/loop.py", "agent/broker.py", "conversation/log.py", "config.py",
                         "backend/claude_code/adapter.py", "backend/claude_code/transcript.py",
                         "backend/claude_code/pane.py", "backend/claude_code/relay.py",
                         "live/realtime.py", "live/strategy_function.py", "audio/io.py",
                         "app/daemon.py", "platform.py", "live/gpt_live.py"):
            self.assertIn(required, scanned)

    def test_B3_only_the_permitted_files_declare_lifecycle_ports(self):
        """DESIGN.md §Timers names where each permitted bound lives; nothing else may declare one.

        Four files, and the list is the point: `app/daemon.py` holds the injected ports,
        `live/realtime.py` the connection open/close bounds it is handed, `audio/io.py` the
        AudioLock's acquisition machinery whose cadence is also injected, and `platform.py`
        the portable file watch whose poll cadence the daemon injects (Linux/Windows have no
        kqueue). A marker appearing in a fifth file means someone gave a decision path a clock.
        """
        declaring = sorted(path.relative_to(VOICE).as_posix() for path in _scanned_files()
                           if MARKER_BEGIN.search(path.read_text(encoding="utf-8")))
        self.assertEqual(declaring, ["app/daemon.py", "audio/io.py", "live/realtime.py",
                                     "platform.py"],
                         "a lifecycle-ports marker outside the four permitted files")

    def test_B3_every_marker_region_is_balanced(self):
        """An unclosed region would silently exempt the rest of its file."""
        for path in _scanned_files():
            text = path.read_text(encoding="utf-8")
            begins = len(MARKER_BEGIN.findall(text))
            if not begins:
                continue
            with self.subTest(path=path.relative_to(VOICE).as_posix()):
                self.assertEqual(begins, len(MARKER_END.findall(text)),
                                 "unbalanced lifecycle-ports markers")

    def test_B3_the_daemons_declared_ports_are_inside_its_region(self):
        """The bounds the daemon injects must be the ones the marker actually covers."""
        text = (VOICE / "app" / "daemon.py").read_text(encoding="utf-8")
        begin = text.index("b3: lifecycle-ports begin")
        end = text.index("b3: lifecycle-ports end")
        region = text[begin:end]
        for name in ("CONNECT_BOUND_S", "CLOSE_BOUND_S", "RECONNECT_BOUND_S",
                     "AUDIO_LOCK_BOUND_S", "AUDIO_LOCK_POLL_S", "PANE_READ_BOUND_S",
                     "RELAY_CONNECT_BOUND_S", "PS_PROBE_BOUND_S", "PS_START_GRANULARITY_S",
                     "JOBS_LONG_POLL_MAX_SEC", "WATCH_POLL_S"):
            self.assertIn(name, region, f"{name} is declared outside the marked block")

    def test_B3_the_scan_would_catch_a_violation(self):
        """The patterns are load-bearing, so prove each one fires."""
        samples = ("_GRACE_S = 0.5", "await asyncio.sleep(1)", "now = time.time()",
                   "t = monotonic()", "await asyncio.wait_for(x, 5)",
                   "await asyncio.wait({a}, timeout=3)", "sock.settimeout(timeout=2)",
                   "def f(*, grace_s: float = 0.5):", "f(bound_s=3.0)",
                   # The three the second review found walking through the old patterns.
                   "DEFAULT_MAX_SECONDS = 240",
                   "def speak(max_seconds: int = 240):",
                   "return max(30, min(900, max_seconds))")
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(any(pattern.search(sample) for pattern, _d in FORBIDDEN),
                                sample)

    def test_B3_an_injected_bound_without_a_default_is_allowed(self):
        """The rule is about DEFAULTS, not about naming a duration a caller supplies."""
        for sample in ("def f(*, bound_s: float) -> None:", "f(bound_s=caller_value)",
                       "timeout=self.max_seconds"):
            with self.subTest(sample=sample):
                self.assertFalse(any(pattern.search(sample) for pattern, _d in FORBIDDEN),
                                 sample)


class PurityTests(unittest.TestCase):
    """The core's other static promise: the pure files are pure."""

    def test_the_pure_core_has_no_async_and_no_io(self):
        for name in ("conversation/log.py", "agent/broker.py"):
            with self.subTest(name=name):
                text = (VOICE / name).read_text(encoding="utf-8")
                code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
                self.assertNotIn("async def", code)
                self.assertNotIn("await ", code)
                self.assertNotIn("import asyncio", code)
                self.assertNotIn("open(", code)


if __name__ == "__main__":
    unittest.main()
