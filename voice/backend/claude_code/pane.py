"""The pane — reading one Claude Code terminal, and proving it is ours.

Ported from `voice_listen_pane.py`, bucket (a) only. This module reads a screen and says
what shape it has. It never types, never presses a key, and never retargets: a binding that
stops describing reality becomes a REASON, not a new pane to follow.

Three properties carry it, and each is the answer to a way the old design could have hurt
someone.

**Ownership is proven, with zero keystrokes.** A handle we were given is not proof that it
points at our session. The proof is a nonce we placed on the command line, seen inside a
RUNNING tool-call block — not merely somewhere on screen, because old transcript text, a
previous run's output, or the operator pasting the nonce into chat all match a naive scan and
none of them is our pane. Zero hits is a hard refusal with no retry: there is no keystroke
that could make an unproven pane proven.

**Identity is (pid, start time, tty), never pid alone.** A pid is a reused integer. The Claude
that launched us can exit and something unrelated can inherit its number within seconds, so
the pair is what identifies a process and what makes `owner_pid_reused` detectable.

**A dialog's occurrence is what makes consent unreusable.** Matching wording is not a reusable
authorization. The occurrence counter increments whenever a dialog appears after being absent,
so an approval for occurrence 7 cannot answer occurrence 8 even when the two are byte-identical.

What the split (the backend split (DESIGN.md §Backend split)) removed: the whole keyboard surface. `INSERTABLE_CLASSES`,
`composer`, `draft`, `picker` and their seven helpers existed to answer "is it safe to type
here", and the relay never types. `classify` keeps class, dialog, reason and at.

Design: voice/DESIGN.md §Backend split, §Acceptance E1, E2, E6.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from voice.config import redact_text

# The pane shapes. `unknown` is the safe absorbing state, not an error code: a caller that
# cannot tell what the pane shows must behave exactly as if the pane were hostile, and the only
# way to guarantee that is to make "I don't know" a first-class answer.
CLS_WORKING = "working"
CLS_PROMPT = "prompt"
# ONE dialog class. Splitting `permission` from `question` was a wording judgement — the screen
# draws the same shape for both, and only the prose differed. The broker does not need the
# distinction: it composes its challenge from the verbatim prompt and the rendered options, and
# the confirming phrase names an INDEX, which is meaningful for either kind.
CLS_DIALOG = "dialog"
CLS_UNKNOWN = "unknown"

CLASSES = frozenset({CLS_WORKING, CLS_PROMPT, CLS_DIALOG, CLS_UNKNOWN})

# Binding refusals. Returned, never raised: a refusal is an ordinary outcome the voice layer
# must say out loud, not an exception to log.
REFUSE_UNPROVEN = "bind_unproven"
REFUSE_NO_HANDLE = "bind_no_handle"
REFUSE_READ_FAILED = "bind_read_failed"
REFUSE_NO_OWNER = "bind_no_owner"
REFUSE_BAD_NONCE = "bind_bad_nonce"
REFUSE_REGISTRY = "bind_session_registry_mismatch"
REFUSE_NO_LAUNCH = "bind_launch_not_in_transcript"
REFUSE_NONCE_USED = "bind_nonce_already_used"
REFUSE_NONCE_UNRECORDABLE = "bind_nonce_unrecordable"
REFUSE_NONCE_NOT_OURS = "bind_nonce_not_in_environment"

# Owner-loss reasons, mirrored from the fingerprint fields we can actually check.
LOST_PID_GONE = "owner_pid_gone"
LOST_PID_REUSED = "owner_pid_reused"
LOST_INCARNATION = "owner_incarnation_changed"
LOST_TTY = "owner_tty_changed"
LOST_HANDLE = "owner_handle_changed"


class PaneError(Exception):
    """A programming error in how this module was called (bad argv, absent nonce).

    Deliberately NOT used for refusals: raising there would tempt a caller into a try/except
    that treats a refusal as a transient."""


# ---------------------------------------------------------------- measured markers
#
# Measured on Claude Code 2.1.260. A pattern below is annotated where it is DECLARED rather
# than captured, because an unmeasured pattern must never make a permissive decision.

# MEASURED. A rendered tool call: "  ⎿  $ <command>". Anchors the nonce proof.
_TOOL_CALL_PREFIX = "  ⎿  $ "

# MEASURED. The spinner, e.g. "✢ Whirring… (3m 47s · ↓ 12.2k tokens)". The verb rotates through
# a large vocabulary, so this matches the SHAPE — not a verb list that would silently stop
# matching the day Claude Code adds a word.
_SPINNER_RE = re.compile(r"^\s*\S{0,2}\s*\w[\w'-]*…\s*\(", re.UNICODE)

# MEASURED. The composer prompt glyph, and the rules bracketing the composer. Kept only to
# recognize a plain prompt screen; nothing here measures whether it is safe to type.
_PROMPT_MARK = "❯"
_RULE_RE = re.compile(r"^\s*[─━]{20,}\s*$")

# A DIALOG IS RECOGNIZED BY SHAPE, NEVER BY WORDING. What makes a screen a dialog is that it
# draws a numbered option block with a cursor on one row — a picker. That is a drawing fact,
# true whatever language the harness renders in and whatever it is asking about.
#
# This replaced a table of English phrases ("Do you want to", "wants to run", "Select an
# option", options beginning "Yes"/"No"). Those patterns decided MEANING from wording: they
# sorted a screen into permission-vs-question and, worse, let a label's first word pick which
# key to press. A localized harness, a reworded prompt, or an option list in a different order
# would each have silently changed which key a confirmed approval landed on.
#
# The cursor row is what proves a picker owns the keyboard right now.
_PICKER_RE = re.compile(r"^\s*[❯>]\s*\d{1,2}[.)]\s")

# A dialog needs at least this many options before we will call it a dialog. One stray "1." in
# prose is not a picker, and a half-drawn dialog with one option rendered is not one either: it
# stays `unknown` until it finishes drawing.
_MIN_DIALOG_OPTIONS = 2

_OPTION_RE = re.compile(r"^\s*[❯>]?\s*(\d{1,2})[.)]\s+(.+?)\s*$")

# Dialogs are drawn inside a box, so every option arrives wrapped in border glyphs
# ("│ ❯ 1. Yes │"). Stripping the frame is not cosmetic: without it the option block never
# parses, every real dialog falls to `unknown`, and the classifier silently loses the ability
# to recognize a permission prompt at all.
_BORDER_RE = re.compile(r"^[\s│┃|]+|[\s│┃|]+$")


def _lines(screen: str) -> list[str]:
    return (screen or "").splitlines()


def normalize_dialog_text(text: str) -> str:
    """Collapse a dialog's visible text to a stable comparison key.

    Whitespace and box glyphs are dropped because a dialog re-renders on every resize and
    repaint; keeping them would make the SAME live dialog look new on each redraw, which would
    revoke the operator's confirmation mid-answer. Case is preserved: "Delete" and "delete" are
    different words in a command line.
    """
    stripped = re.sub(r"[│┃|╭╮╰╯─━┌┐└┘┤├❯>]", " ", text or "")
    return " ".join(stripped.split())


def options_in(lines: list[str]) -> tuple[tuple[int, str], ...]:
    """The dialog's numbered options as ordered (index, text) pairs, in screen order.

    THE INDEX IS STRUCTURE, NOT DECORATION. Answering a dialog means pressing a real option
    number observed on THIS occurrence; a flattened display string would have to be re-parsed
    by the component whose next act is pressing a key.

    Order is preserved as rendered because the answer rule is "the lowest-numbered conservative
    match". The measured wording is `1. Yes`, `2. Yes, and don't ask again`, `3. No`: option 2
    grants strictly more than was asked, so order is what keeps a broad standing grant from
    being picked in place of the narrow one the operator confirmed.

    A malformed index is DROPPED, never coerced: an option we cannot number is one nothing could
    press correctly, and dropping it shrinks the block toward the floor, failing closed.
    """
    out: list[tuple[int, str]] = []
    for line in lines:
        match = _OPTION_RE.match(_BORDER_RE.sub("", line))
        if match:
            try:
                index = int(match.group(1))
            except ValueError:      # unreachable via _OPTION_RE; explicit for the reader
                continue
            out.append((index, normalize_dialog_text(match.group(2))))
    return tuple(out)


def _option_hash_key(options: tuple[tuple[int, str], ...]) -> tuple[str, ...]:
    """Options rendered exactly as they are hashed. The hash is the identity half of an
    occurrence, so an unchanged dialog must keep its digest byte for byte."""
    return tuple(f"{index}:{text}" for index, text in options)


def dialog_identity(blob: str, *, options: tuple[tuple[int, str], ...],
                    previous: dict | None, incarnation: str | None = None,
                    tool_id: str | None = None) -> dict:
    """Identity for a dialog now on screen: hash plus occurrence counter.

    THE RULE THIS ENCODES: matching wording is not a reusable authorization. A dialog that
    stays on screen across polls keeps its occurrence, so a confirmation given while it is up
    stays valid until the dialog itself goes away. A dialog that appears after being absent is
    a NEW occurrence even when byte-identical to the last one.
    """
    question = normalize_dialog_text(blob)
    digest = hashlib.sha256(
        " ".join((question, *_option_hash_key(options))).encode("utf-8")
    ).hexdigest()[:16]

    occurrence = 1
    if previous and previous.get("hash") == digest and previous.get("present", True):
        # Same dialog, still up. Continuity — not equality of text — preserves an authorization.
        occurrence = int(previous.get("occurrence", 1))
    elif previous:
        occurrence = int(previous.get("occurrence", 0)) + 1

    return {
        "hash": digest,
        # Redact the WHOLE text first: clipping first could leave a secret's prefix that no
        # later masking can recognise. The hash above is identity only and never leaves.
        "question": redact_text(question)[:400],
        "options": options,
        "occurrence": occurrence,
        "incarnation": incarnation,
        "tool_id": tool_id,
        "present": True,
    }


def _prompt_line_index(lines: list[str]) -> int | None:
    """The composer's prompt row, bottom-up. Only used to recognize a plain prompt screen."""
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip().startswith(_PROMPT_MARK):
            return index
    return None


def classify(screen: str, *, previous: dict | None, at: float) -> dict:
    """Map a pane screen to one of CLASSES, with dialog identity when a dialog is up.

    `previous` is the last classification for THIS binding, and it is what makes the occurrence
    counter meaningful.

    FAILS CLOSED: anything not positively recognized is `unknown`. No branch returns a
    permissive class from a partial match.
    """
    lines = _lines(screen)
    previous = previous if isinstance(previous, dict) else None
    prev_dialog = (previous or {}).get("dialog")

    result: dict[str, Any] = {
        "class": CLS_UNKNOWN,
        "at": float(at),
        "dialog": None,
        "reason": "",
    }

    if not lines:
        result["reason"] = "empty_screen"
        return result

    # -- dialog first ---------------------------------------------------------
    #
    # Order is a safety decision, not a style one. A permission dialog is drawn ABOVE a composer
    # that may still show its prompt marker; checking the prompt first would classify a live
    # dialog as an idle prompt. Dialogs win whenever they are recognized at all.
    blob = "\n".join(lines)
    options = options_in(lines)
    # SHAPE ONLY: a cursor-bearing numbered row means some widget owns the keyboard, and a
    # complete numbered block means it is a picker we can describe. Neither test reads a word.
    has_picker = any(_PICKER_RE.match(_BORDER_RE.sub("", line)) for line in lines)

    if has_picker:
        if len(options) < _MIN_DIALOG_OPTIONS:
            # A cursor row with no complete option block: a dialog still drawing, or a widget we
            # cannot enumerate. Both stay `unknown`, which forbids arming.
            result["reason"] = "dialog_incomplete"
            return result
        result["class"] = CLS_DIALOG
        result["dialog"] = dialog_identity(
            blob,
            options=options,
            # The previous dialog record is ALWAYS handed over, even when the class in between
            # was a prompt or working. That gap is the disappear-then-reappear case; dropping it
            # would restart the counter at 1 and let an old confirmation match the new dialog.
            previous=prev_dialog,
            incarnation=(previous or {}).get("incarnation"),
            tool_id=(previous or {}).get("tool_id"),
        )
        return result

    # -- working --------------------------------------------------------------
    #
    # The spinner is the authoritative "mid-turn" marker; a rendered tool call corroborates it.
    spinner = any(_SPINNER_RE.match(line) for line in lines)
    tool_call = any(line.startswith(_TOOL_CALL_PREFIX) for line in lines)
    if spinner or tool_call:
        result["class"] = CLS_WORKING
        return result

    # -- an idle prompt -------------------------------------------------------
    #
    # A prompt reading is trusted only inside a rendered composer: both rules present with the
    # prompt between them. A single rule means we caught the pane mid-repaint.
    prompt_index = _prompt_line_index(lines)
    if prompt_index is None:
        result["reason"] = "no_prompt_marker"
        return result
    above = any(_RULE_RE.match(line) for line in lines[:prompt_index])
    below = any(_RULE_RE.match(line) for line in lines[prompt_index + 1:])
    if not (above and below):
        result["reason"] = "composer_partial"
        return result
    result["class"] = CLS_PROMPT
    return result


# ---------------------------------------------------------------- Orca's own wait signal
#
# `orca terminal show --json` carries `agentWait`: Orca's own answer to "is the agent in this
# pane waiting on an interactive prompt", taken from its agent hooks, its prompt matcher or the
# terminal title. Absent means Orca did not evaluate it (an older Orca, a pane it cannot
# attribute); null means evaluated, no wait; an object means waiting.
#
# It answers WHETHER, never WHAT: it carries no prompt text and no options, so the words read
# back and the options anything could name still come only from the screen. And it only ADDS: a
# wait Orca reports is heard even when the screen cannot be read as a dialog, but Orca's "no
# wait" never removes a dialog the screen found. A missed prompt leaves the agent stuck in
# silence; a rare false "waiting" costs one sentence. Absent, null or unreadable, the screen
# decides alone, exactly as before.

# The sources Orca publishes. An unknown source is a field we cannot vouch for, so it is read
# as no evaluation at all and the screen decides.
_WAIT_SOURCES = frozenset({"hook", "prompt-text", "title"})


def agent_wait(show: Any) -> dict | None:
    """Orca's wait verdict from a `terminal show` payload, or None when there is none to use.

    `{"waiting": False}` for an evaluated null; `{"waiting": True, "source", "reason", "since"}`
    for a wait. None for absent, malformed, or a show that failed. Only a wait changes anything
    downstream; every other answer leaves the screen to decide alone.
    """
    if not isinstance(show, dict) or "agentWait" not in show:
        return None
    raw = show["agentWait"]
    if raw is None:
        return {"waiting": False}
    if not isinstance(raw, dict) or raw.get("source") not in _WAIT_SOURCES:
        return None
    reason = raw.get("reason")
    since = raw.get("since")
    if reason is not None and not isinstance(reason, str):
        return None
    if since is not None and (isinstance(since, bool) or not isinstance(since, (int, float))):
        return None
    return {"waiting": True, "source": raw["source"], "reason": reason, "since": since}


def apply_agent_wait(classification: dict, wait: dict | None, *,
                     previous: dict | None) -> dict:
    """Add Orca's wait to the screen classification; never take a screen dialog away.

    * no verdict, or Orca's "no wait": the screen classification, unchanged.
    * waiting, screen dialog: the screen's dialog, with the verdict attached.
    * waiting, no enumerable screen dialog: a dialog with NO options, so the operator hears that
      the agent is waiting. Nothing can be named by position, so the broker never arms it.
    """
    out = dict(classification)
    out["agent_wait"] = wait
    if wait is None or not wait["waiting"] or out.get("class") == CLS_DIALOG:
        return out
    previous = previous if isinstance(previous, dict) else None
    # `since` stays out of the identity: the hook may restamp it while the same prompt is up,
    # and an appearance after an absence is already a new occurrence by the counter.
    described = (f"waiting on a prompt in the terminal (Orca agentWait: "
                 f"{wait['reason'] or 'interactive prompt'}, via {wait['source']})")
    record = dialog_identity(described, options=(),
                             previous=(previous or {}).get("dialog"),
                             incarnation=(previous or {}).get("incarnation"),
                             tool_id=(previous or {}).get("tool_id"))
    record["agent_wait"] = wait
    out.update({"class": CLS_DIALOG, "dialog": record, "reason": "agent_wait"})
    return out


# ---------------------------------------------------------------- owner identity

def owner_fingerprint(pid: int, *, ps_timeout: float,
                      ps_runner: Callable[[list[str]], str] | None = None) -> dict:
    """PID plus start time plus TTY for a live process, or {} when it is gone.

    START TIME IS THE POINT. A pid alone is a reused integer; (pid, start) is the pair that
    actually identifies a process, and comparing it is how `owner_pid_reused` is detected
    instead of silently acting on whatever now answers to that number. TTY is captured because
    a session that moved terminals is not the session we bound to, even if the process survived.
    """
    argv = ["ps", "-o", "pid=,lstart=,tty=,comm=", "-p", str(int(pid))]
    try:
        raw = ps_runner(argv) if ps_runner else _ps(argv, timeout=ps_timeout)
    except Exception:
        return {}
    line = (raw or "").strip()
    if not line:
        return {}
    # pid, then lstart (always five tokens: "Thu Sep 24 02:06:41 2026"), then tty, then comm —
    # which is the executable PATH and may itself hold spaces (the Claude desktop app's
    # bundled claude lives under "Application Support"), so it is everything that is left.
    parts = line.split(maxsplit=7)
    if len(parts) < 8:
        return {}
    return {"pid": int(pid), "start": " ".join(parts[1:6]),
            "tty": parts[6], "comm": parts[7]}


def _ps(argv: list[str], *, timeout: float) -> str:
    """The one `ps` call. `timeout` is a SUBPROCESS bound the daemon supplies — no literal lives
    here. It stops a wedged `ps` from holding a send hostage; it releases nothing semantic, and a
    process that does not answer in time reads as evidence UNAVAILABLE, never as proof of death.
    """
    exe = shutil.which(argv[0])
    if not exe:
        return ""
    # The C locale: `lstart` is locale-formatted, and a zh_CN/ja_JP/de_DE date is neither five
    # tokens nor what the ctime parser reads (measured: zh_CN prints "三  9月/23 18:34:24 2026").
    env = {**os.environ, "LC_ALL": "C"}
    proc = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout,
                          env=env)
    return proc.stdout


# ---------------------------------------------------------------- session registry
#
# Claude Code writes `~/.claude/sessions/<pid>.json` for every live process. It is the ONE
# independent source tying a pid to a session id, which turns "the pane we bound" into "the
# session we bound" — a transcript path or an mtime can only veto identity, never establish it.
#
# `procStart` is ctime-formatted UTC while `ps lstart` is local, so the two are never
# byte-equal; both are reduced to epoch seconds here.

_CTIME = "%a %b %d %H:%M:%S %Y"

def same_start(left: float | None, right: float | None, *, granularity: float) -> bool:
    """Whether two renderings describe the SAME process start instant.

    `granularity` is the coarsest resolution either source reports — `ps` start-time
    granularity — and it is injected, never written down here, so the daemon stays the one place
    a number like this lives.

    WHAT IT IS NOT: a clock-skew allowance or a wait. The registry writes `procStart` as UTC
    ctime and `ps` writes `lstart` as local ctime, so the two strings are never byte-equal and
    the comparison must happen on epoch seconds — but both render WHOLE SECONDS of the same
    instant, and every timezone's UTC offset is a whole number of minutes, so two renderings of
    one instant reduce to the same integer. Measured: 410 instants spanning five days, worst
    observed difference 0.0; 0 of the IANA zones have a sub-minute offset in 2026.

    So at the measured granularity this is an equality, and it is written as `<=` only so the
    assumption is stated where it can be changed rather than buried in an `==`. Widening it
    past a second would open a window in which a DIFFERENT process start passes for ours, which
    is the stale-binding bug the whole comparison exists to prevent. Unreadable evidence on
    either side is never a match.
    """
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) <= granularity


def registry_epoch(proc_start: Any) -> float | None:
    """Registry `procStart` (ctime, UTC) → epoch seconds."""
    try:
        return calendar.timegm(time.strptime(str(proc_start).strip(), _CTIME))
    except (ValueError, TypeError):
        return None


def lstart_epoch(lstart: Any) -> float | None:
    """`ps lstart` (ctime, local) → epoch seconds."""
    try:
        return time.mktime(time.strptime(str(lstart).strip(), _CTIME))
    except (ValueError, TypeError):
        return None


def session_registry(pid: int, *, home: Path | str | None = None) -> dict | None:
    """The registry record for `pid`, or None when there is none we can read."""
    path = Path(home or Path.home()) / ".claude" / "sessions" / f"{int(pid)}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def registry_claims(registry: dict | None, *, pid: int, session_id: str,
                    owner: dict, start_granularity: float) -> tuple[dict | None, str | None]:
    """Turn a registry record into the binding's identity, or say why not.

    The record must name THIS pid, THIS session, and a start time agreeing with the live
    fingerprint — a recycled pid whose registry file outlived its owner would otherwise pass on
    pid and session alone.
    """
    if not isinstance(registry, dict):
        return None, "registry_missing"
    if int(registry.get("pid") or 0) != int(pid):
        return None, "registry_pid_mismatch"
    if str(registry.get("sessionId") or "") != str(session_id or "") or not session_id:
        return None, "registry_session_mismatch"
    r_epoch = registry_epoch(registry.get("procStart"))
    o_epoch = lstart_epoch((owner or {}).get("start"))
    if not same_start(r_epoch, o_epoch, granularity=start_granularity):
        return None, "registry_start_mismatch"
    return {
        "pid": int(pid),
        "session_id": str(session_id),
        "cwd": str(registry.get("cwd") or ""),
        "proc_start": str(registry.get("procStart")),
        "start_epoch": r_epoch,
    }, None


def registry_matches(binding: dict, registry: dict | None, *,
                     start_granularity: float) -> str | None:
    """At an effect boundary: does the live registry still describe the bound identity?

    None when it does, otherwise the mismatch. NEVER RETARGETS: a registry now naming another
    session is a reason to stop, not a new session to follow.
    """
    claude = (binding or {}).get("claude") or {}
    if not claude:
        return "no_bound_identity"
    if not isinstance(registry, dict):
        return "registry_missing"
    if int(registry.get("pid") or 0) != int(claude.get("pid") or -1):
        return "registry_pid_mismatch"
    if str(registry.get("sessionId") or "") != str(claude.get("session_id") or ""):
        return "registry_session_mismatch"
    if str(registry.get("cwd") or "") != str(claude.get("cwd") or ""):
        return "registry_cwd_mismatch"
    epoch = registry_epoch(registry.get("procStart"))
    bound = claude.get("start_epoch")
    if not same_start(epoch, bound if isinstance(bound, (int, float)) else None,
                      granularity=start_granularity):
        return "registry_start_mismatch"
    return None


def owner_lost(binding: dict, *, current_owner: dict, current_show: dict) -> str | None:
    """The reason this binding no longer describes reality, or None.

    NEVER RETARGETS. Every branch returns a reason; not one looks for the "new" pane.
    Retargeting is how a daemon that lost its session starts acting on a stranger's — the
    correct response to owner loss is to fence, revoke outstanding challenges, and say so.
    """
    bound_owner = binding.get("owner") or {}
    if not current_owner:
        return LOST_PID_GONE
    if current_owner.get("start") != bound_owner.get("start"):
        return LOST_PID_REUSED
    if current_owner.get("tty") != bound_owner.get("tty"):
        return LOST_TTY

    # Incarnation and handle are checked ONLY when the runtime actually reported them. Measured:
    # `orca terminal show --json` returns these on some calls and None on others, so treating an
    # absent value as "changed" would fence a healthy binding whenever the runtime felt terse.
    now_incarnation = current_show.get("incarnationId")
    bound_incarnation = binding.get("incarnation")
    if bound_incarnation and now_incarnation and now_incarnation != bound_incarnation:
        return LOST_INCARNATION
    now_handle = current_show.get("handle")
    if now_handle and now_handle != binding.get("handle"):
        return LOST_HANDLE
    return None


class Pane:
    """Reads one Orca pane through an injected runner. Sends nothing, ever.

    `run(argv, timeout)` is supplied by the caller. Injected rather than imported so that tests
    never shell out to a real orca, and so there is exactly ONE place in the process that can
    spawn a terminal command.
    """

    def __init__(self, *, terminal: str,
                 run: Callable[[list[str], float], Awaitable[dict]],
                 read_timeout: float, start_granularity: float, ps_timeout: float,
                 now: Callable[[], float] | None = None) -> None:
        self._terminal = str(terminal or "")
        self._run = run
        # A SUBPROCESS BOUND, injected by the daemon: no literal lives in this module. It bounds
        # one `orca terminal read`; nothing semantic is released when it expires.
        self._read_timeout = float(read_timeout)
        # `ps` start-time granularity, injected: the coarsest resolution the two start-time
        # renderings report. See `same_start` for why it is an equality at the measured value.
        self._start_granularity = float(start_granularity)
        # A subprocess bound for the `ps` probe, injected with the others.
        self._ps_timeout = float(ps_timeout)
        # The clock that stamps when a screen was captured. Injected so a replayed fixture reads
        # the same every time, and so this module holds no ambient clock of its own.
        self._now = now or time.time
        self._last: dict | None = None      # last classification, for occurrence counting

    @property
    def terminal(self) -> str:
        return self._terminal

    # -- binding ------------------------------------------------------------

    async def bind(self, *, nonce: str, session_id: str, ancestor_pid: int,
                   registry: dict | None = None) -> dict:
        """Prove this handle is the pane running OUR command. Zero keystrokes.

        The nonce must be the FIRST token of our command line: the rendered tool call wraps and
        is truncated after a few lines, so a nonce behind a long prefix falls outside the
        visible tail (measured — the first probe attempt scored 0 hits for exactly that). The
        nonce's SHAPE is validated here because a caller that let it drift is a caller whose 0
        hits would be a bug, not a wrong pane, and those two must never look alike.

        Returns `{"bound": True, ...}` or `{"bound": False, "refusal": <reason>}`. A refusal is
        never a retry instruction.
        """
        nonce = str(nonce or "")
        if not nonce or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,63}", nonce):
            # A nonce with spaces or shell metacharacters could not have survived as the first
            # token of a command line, so "0 hits" from it would be meaningless.
            return self._refuse(REFUSE_BAD_NONCE, nonce=nonce)
        if not self._terminal:
            return self._refuse(REFUSE_NO_HANDLE, nonce=nonce)

        owner = owner_fingerprint(ancestor_pid, ps_timeout=self._ps_timeout)
        if not owner:
            # No live ancestor means nothing to attribute an effect to, and no fingerprint to
            # detect loss against later.
            return self._refuse(REFUSE_NO_OWNER, nonce=nonce)
        claude, why = registry_claims(registry, pid=ancestor_pid, session_id=session_id,
                                      owner=owner,
                                      start_granularity=self._start_granularity)
        if claude is None:
            return self._refuse(REFUSE_REGISTRY, nonce=nonce, detail=why)

        read = await self._read(limit=60)
        if not read.get("ok"):
            return self._refuse(REFUSE_READ_FAILED, nonce=nonce, detail=read.get("error", ""))

        tail = read.get("tail") or []
        hits = self._nonce_hits(tail, nonce)
        if hits == 0:
            # THE HARD REFUSE. A handle pointing at another pane cannot show a command that pane
            # is not running (measured: 0 hits in both sibling panes). No retry, and above all
            # no "helping" by typing the nonce in.
            return self._refuse(REFUSE_UNPROVEN, nonce=nonce, hits=0, tail_lines=len(tail))

        show = await self._show()
        classification = apply_agent_wait(
            classify("\n".join(tail), previous=None, at=read["captured_at"]),
            agent_wait(show), previous=None)
        self._last = classification

        return {
            "bound": True,
            "refusal": None,
            "nonce": nonce,
            "hits": hits,
            "session_id": str(session_id),
            "handle": self._terminal,
            # Context, NOT proof. Measured: these come back None on some calls, so they are
            # recorded for owner-loss comparison and never consulted for identity.
            "incarnation": show.get("incarnationId"),
            "worktree_path": show.get("worktreePath"),
            "owner": owner,
            "claude": claude,
            "bound_at": read["captured_at"],
            "classification": classification,
        }

    def _refuse(self, reason: str, **extra: Any) -> dict:
        out = {"bound": False, "refusal": reason, "handle": self._terminal,
               "hits": 0, "owner": {}, "incarnation": None}
        out.update(extra)
        return out

    @staticmethod
    def _nonce_hits(tail: list[str], nonce: str) -> int:
        """Count nonce occurrences on lines rendering a RUNNING command.

        The `  ⎿  $ ` prefix and its continuation lines prove the command is running NOW. A bare
        nonce elsewhere in the tail is not proof: old transcript text, a previous run's output,
        or the operator pasting the nonce into chat all match a naive substring scan, and each
        of those is a pane that may not be ours.
        """
        hits = 0
        in_block = False
        for line in tail:
            if line.startswith(_TOOL_CALL_PREFIX):
                in_block = True
            elif in_block and not line.startswith("     "):
                # Continuation lines are indented under the marker; the first that is not ends
                # the block.
                in_block = False
            if in_block and nonce in line:
                hits += 1
        return hits

    # -- observation --------------------------------------------------------

    async def observe(self, *, binding: dict, pending_tool_ids: frozenset[str]) -> dict:
        """One bounded read plus classification, with owner re-validation.

        `pending_tool_ids` comes from the tailer and supplies the tool id a dialog belongs to
        when one is unambiguous. When zero or several tools are pending we attach nothing rather
        than guess: a WRONG tool id would make two different dialogs look like one, which is the
        stale-authorization bug in a different costume.

        ORDER IS THE INVARIANT. The metadata and process probes run FIRST so the screen is the
        LAST thing captured and therefore the freshest thing in the observation.
        """
        show = await self._show()
        owner_now = owner_fingerprint((binding.get("owner") or {}).get("pid", 0),
                                      ps_timeout=self._ps_timeout)
        lost = owner_lost(binding, current_owner=owner_now, current_show=show)

        read = await self._read(limit=60)
        if not read.get("ok"):
            return {"ok": False, "lost": lost, "classification": None,
                    "screen": "", "error": read.get("error", "")}

        tail = read.get("tail") or []
        screen = "\n".join(tail)

        previous = dict(self._last) if self._last else None
        if previous is not None:
            previous["incarnation"] = binding.get("incarnation")
            previous["tool_id"] = self._sole_tool_id(pending_tool_ids)
        # Orca's verdict was taken with the metadata, a moment BEFORE the screen. A prompt that
        # opens in between is still found by the screen; one that closes in between leaves a
        # stale wait for one poll, which only ever reports a wait, never an approval.
        classification = apply_agent_wait(
            classify(screen, previous=previous, at=read["captured_at"]),
            agent_wait(show), previous=previous)

        # A dialog that has gone away is remembered as ABSENT, so the NEXT dialog gets a fresh
        # occurrence. Forgetting it would restart the counter at 1 and let a stale confirmation
        # for the previous occurrence 1 match the new one.
        if classification.get("dialog") is None and previous and previous.get("dialog"):
            faded = dict(previous["dialog"])
            faded["present"] = False
            classification["_faded_dialog"] = faded
            self._last = {**classification, "dialog": faded}
        else:
            self._last = classification

        # Owner loss does NOT suppress the classification — the voice layer still wants to
        # describe what it last saw — but it is reported alongside so the caller fences first.
        return {"ok": True, "lost": lost, "classification": classification,
                "screen": screen, "captured_at": read["captured_at"]}

    @staticmethod
    def _sole_tool_id(pending_tool_ids: frozenset[str]) -> str | None:
        ids = tuple(pending_tool_ids or ())
        return ids[0] if len(ids) == 1 else None

    # -- transport ----------------------------------------------------------

    async def _read(self, *, limit: int) -> dict:
        """One bounded pane read, carrying the time the screen was ACTUALLY captured.

        `captured_at` is stamped here, next to the read, and never re-derived by a caller: a
        timestamp taken anywhere else measures when the OBSERVER finished, not how old the
        screen is. It is taken AFTER the transport returns, because the state being described is
        the one that existed when orca answered.
        """
        argv = ["orca", "terminal", "read", "--terminal", self._terminal,
                "--limit", str(int(limit)), "--json"]
        result = await self._run(argv, self._read_timeout)
        captured_at = self._now()
        envelope = self._envelope(result)
        if envelope is None:
            return {"ok": False, "error": "unparseable read envelope",
                    "captured_at": captured_at,
                    "raw": (result or {}).get("stdout", "")[:400]}
        terminal = (envelope.get("result") or {}).get("terminal") or {}
        tail = terminal.get("tail")
        if not isinstance(tail, list):
            return {"ok": False, "error": "read envelope carried no tail list",
                    "captured_at": captured_at}
        return {"ok": True, "tail": [str(line) for line in tail], "terminal": terminal,
                "captured_at": captured_at}

    async def _show(self) -> dict:
        """Metadata, best-effort. A failure here is NOT a binding failure: measured, this call
        returns partial data on its own schedule, and nothing it reports is load-bearing."""
        argv = ["orca", "terminal", "show", "--terminal", self._terminal, "--json"]
        try:
            result = await self._run(argv, self._read_timeout)
        except Exception:
            return {}
        envelope = self._envelope(result)
        if envelope is None:
            return {}
        return (envelope.get("result") or {}).get("terminal") or {}

    @staticmethod
    def _envelope(result: dict | None) -> dict | None:
        """Parse the `orca ... --json` envelope out of a run() result.

        Tolerant of leading noise on stdout (a banner, a warning): the alternative would turn a
        cosmetic stdout change into a refusal to bind.
        """
        if not isinstance(result, dict):
            return None
        stdout = result.get("stdout")
        if not isinstance(stdout, str) or not stdout.strip():
            return None
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            pass
        start = stdout.find("{")
        while start != -1:
            try:
                return json.loads(stdout[start:])
            except json.JSONDecodeError:
                start = stdout.find("{", start + 1)
        return None


# ------------------------------------------------------------------ ownership without a screen
#
# Any host that runs Claude Code with its session registry, messaging socket and transcript —
# a plain terminal, the Claude desktop app — can be bound without reading a screen. The proof
# is the same two facts the pane proof rests on, taken from the session's own records:
#   1. ancestry: this launcher descends from the `claude` process the session registry names
#      for THIS session (checked by the caller, `find_claude_ancestor`), alive, start time
#      agreeing with the registry (`registry_claims`);
#   2. the launch: the session's own transcript holds exactly one main-thread Bash call carrying
#      the fresh nonce, from the current claude process. Ancestry + fresh nonce + single use tie
#      that call to this process; its own environment (NONCE=<n>) and argv (--nonce <n> start)
#      must agree, which catches mis-launches. Nothing is parsed from command text.
# What is lost without a screen: dialogs. Nothing observes a permission prompt, so the voice
# never offers a spoken approval (`dialogs=False`) — approvals stay on the keyboard.

TRANSCRIPT_TAIL_BYTES = 1 << 20     # a SIZE, not a time: how far back the launch may sit


def _row_epoch(row: dict) -> float | None:
    """The transcript row's own timestamp (ISO-8601, `Z` = UTC) as epoch seconds, or None."""
    from datetime import datetime

    raw = str(row.get("timestamp") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def launch_in_transcript(path: str | Path, nonce: str, *, session_id: str,
                         not_before: float | None,
                         tail_bytes: int = TRANSCRIPT_TAIL_BYTES) -> bool:
    """True when the transcript's tail shows the session's agent issued this launch: exactly one
    tool call carries the nonce (as a whole token), and that call is

      * a `Bash` call on this session's main thread (not a subagent sidechain, not another
        session's row),
      * written no earlier than `not_before` — the owning claude process's start, so a launch
        from an earlier process of a resumed session is history,
      * its command contains the nonce.

    What the command DOES is not judged from its text (re-implementing a shell is a losing
    game); the running daemon proves that about ITSELF: its own argv parsed by its own parser
    is `start --nonce <n>`, and its own environment carries `NONCE=<n>`, which only the
    process a `NONCE=<n> …` command started has (`bind_without_screen`).

    A chat line or a tool result is not a launch. More than one matching call is ambiguous and
    refused. Whether the call has finished is NOT checked: a background launch's result is
    written as soon as it is spawned."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    carriers, launches = 0, 0
    # The nonce as a whole token: a longer nonce that contains it is a different nonce.
    token = re.compile(r"(?<![A-Za-z0-9._-])" + re.escape(nonce) + r"(?![A-Za-z0-9._-])")
    for line in data.splitlines():
        if not token.search(line):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        content = (row.get("message") or {}).get("content") if isinstance(row, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if not token.search(json.dumps(block.get("input") or {})):
                continue
            # Only calls from THIS claude process compete (an older process's are history).
            if not_before is not None:
                when = _row_epoch(row)
                if when is None or when + 1.0 < not_before:
                    continue
            carriers += 1
            if block.get("name") != "Bash" or row.get("isSidechain") is True:
                continue
            if row.get("sessionId") not in (None, session_id):
                continue
            if not_before is not None:
                when = _row_epoch(row)
                # `procStart` is to the second (PS_START_GRANULARITY).
                if when is None or when + 1.0 < not_before:
                    continue
            if token.search(str((block.get("input") or {}).get("command") or "")):
                launches += 1
    return launches == 1 and carriers == 1


def bind_without_screen(*, nonce: str, session_id: str, ancestor_pid: int,
                        registry: dict | None, transcript: str | Path, ps_timeout: float,
                        start_granularity: float, now: Callable[[], float],
                        env_nonce: str | None) -> dict:
    """The screen-less binding: same shape as `Pane.bind`'s, `handle` empty, `dialogs` False."""
    nonce = str(nonce or "")
    refuse = {"bound": False, "handle": "", "hits": 0, "owner": {}, "incarnation": None,
              "nonce": nonce}
    if not nonce or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,63}", nonce):
        return {**refuse, "refusal": REFUSE_BAD_NONCE}
    if env_nonce != nonce:
        # The launch command sets NONCE=<n>; only the process it started carries it.
        return {**refuse, "refusal": REFUSE_NONCE_NOT_OURS}
    owner = owner_fingerprint(ancestor_pid, ps_timeout=ps_timeout)
    if not owner:
        return {**refuse, "refusal": REFUSE_NO_OWNER}
    claude, why = registry_claims(registry, pid=ancestor_pid, session_id=session_id,
                                  owner=owner, start_granularity=start_granularity)
    if claude is None:
        return {**refuse, "refusal": REFUSE_REGISTRY, "detail": why}
    if not launch_in_transcript(transcript, nonce, session_id=str(session_id),
                                not_before=claude.get("start_epoch")):
        return {**refuse, "refusal": REFUSE_NO_LAUNCH}
    return {"bound": True, "refusal": None, "nonce": nonce, "hits": 1,
            "session_id": str(session_id), "handle": "", "proof": "transcript",
            "dialogs": False, "incarnation": None, "worktree_path": None,
            "owner": owner, "claude": claude, "bound_at": now(),
            "classification": None}


class ScreenlessPane:
    """The pane seat when nothing reads a screen: it re-validates the owner and reports no
    dialog, ever. Same `observe` contract as `Pane`, so the adapter needs no branch."""

    terminal = ""

    def __init__(self, *, ps_timeout: float) -> None:
        self._ps_timeout = ps_timeout

    async def observe(self, *, binding: dict, pending_tool_ids: frozenset = frozenset()) -> dict:
        owner_now = owner_fingerprint((binding.get("owner") or {}).get("pid", 0),
                                      ps_timeout=self._ps_timeout)
        lost = owner_lost(binding, current_owner=owner_now, current_show={})
        if lost:
            return {"ok": False, "lost": lost, "classification": None}
        return {"ok": True, "lost": None, "classification": None}
