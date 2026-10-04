"""`bettercallgpt` — the installed entry point to the voice daemon.

The daemon itself is `voice/`. This launcher adds what an installed package needs and a source
checkout does not:

1. Where config and state live. The daemon reads `.env` beside the bundle; an installed package sits in
   site-packages, where nobody keeps a `.env`. Unless the operator already set them, this sets
     BETTERCALLGPT_ENV_FILE  -> ~/.config/bettercallgpt/.env   (%APPDATA%\\bettercallgpt\\.env on Windows)
     VOICE_LISTEN_STATE_DIR  -> ~/.local/state/bettercallgpt   (%LOCALAPPDATA%\\bettercallgpt on Windows)
   A source checkout that has its own `.env` keeps using it.
2. `doctor` — a no-session preflight: the daemon's own provider/backend/platform rules,
   credentials (names only, never values), runtime modules and audio devices. It opens no
   stream, socket or session.
3. `statusline` — one segment for a Claude Code `statusLine`: "🎙 voice" while this
   session's call is live ("🎙 voice ↻" while its voice connection is being renewed),
   nothing otherwise. Reads the
   statusLine JSON on stdin for the session id, and only the daemon's status file. Never
   fails loudly: a status bar is not the place.
4. Orca. A `start` run inside an Orca pane gains `--terminal <that pane>` when the daemon's
   own pane proof already holds for it (read-only, claims nothing); the daemon then reads
   the pane and can say when the agent is waiting on a permission prompt. Approval stays on
   the keyboard. When the proof does not hold, the start is left as it was (the screenless
   proof). Off with BETTERCALLGPT_ORCA_PANE=0.

Every other command (`status`, `stop` and their flags) is passed to `voice.app.daemon.main`
untouched.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ENV_FILE_NAME = "BETTERCALLGPT_ENV_FILE"
STATE_DIR_NAME = "VOICE_LISTEN_STATE_DIR"
APP = "bettercallgpt"
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
ORCA_HANDLE_NAME = "ORCA_TERMINAL_HANDLE"   # set by Orca in every pane it spawns
ORCA_OPT_OUT_NAME = "BETTERCALLGPT_ORCA_PANE"


def user_config_env(environ=os.environ, platform: str = sys.platform) -> Path:
    if platform.startswith("win"):
        base = Path(environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP / ".env"


def user_state_dir(environ=os.environ, platform: str = sys.platform) -> Path:
    if platform.startswith("win"):
        base = Path(environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / APP


def bundle_env() -> Path:
    """The `.env` the daemon reads by default: beside the `voice/` package."""
    spec = importlib.util.find_spec("voice")
    root = Path(spec.origin).resolve().parent.parent if spec and spec.origin else Path.cwd()
    return root / ".env"


def apply_defaults(environ=os.environ, platform: str = sys.platform) -> dict:
    """Set the two locations unless the operator already did. Returns what was set."""
    applied = {}
    if not environ.get(ENV_FILE_NAME) and not bundle_env().is_file():
        environ[ENV_FILE_NAME] = str(user_config_env(environ, platform))
        applied[ENV_FILE_NAME] = environ[ENV_FILE_NAME]
    if not environ.get(STATE_DIR_NAME):
        environ[STATE_DIR_NAME] = str(user_state_dir(environ, platform))
        applied[STATE_DIR_NAME] = environ[STATE_DIR_NAME]
    return applied


def backend_check(backend: str, cfg, platform: str) -> str | None:
    """Why the selected backend cannot run here, or None. Starts nothing.

    The daemon's own preflight rules (backend registry: known harness, supported OS, process
    dialect) come first; bettercallgpt adds only what an installed copy can check cheaply on top:
    that the process child is actually runnable."""
    import shlex
    import shutil

    from voice.backend import registry as backends

    try:
        backends.require(backend, platform)
    except backends.Unsupported as exc:
        return str(exc)
    if backend == "process":
        try:
            words = shlex.split(cfg("VOICE_PROCESS_ARGV").strip())
        except ValueError as exc:
            return f"VOICE_PROCESS_ARGV does not parse: {exc}"
        if not words:
            return "process needs VOICE_PROCESS_ARGV (e.g. 'codex exec --json')"
        exe = words[0]
        # Resolve exactly as the backend's subprocess will: the cwd string as configured, a
        # literal command path, PATH searched with relative entries taken from that cwd.
        cwd = cfg("VOICE_BACKEND_CWD") or None
        if cwd is not None and not os.path.isdir(cwd):
            return f"VOICE_BACKEND_CWD is not a directory: {cwd!r}"
        base = cwd or os.curdir
        if os.sep in exe or (os.altsep and os.altsep in exe):
            found = os.path.join(base, exe)
            ok = os.path.isfile(found) and os.access(found, os.X_OK)
        else:
            # The backend hands its child PATH="" when PATH is unset (cwd only), never the OS
            # default search path — so resolve against exactly that.
            path = os.environ.get("PATH", "")
            dirs = [os.path.join(base, d) if d else base
                    for d in os.get_exec_path({"PATH": path})]
            ok = shutil.which(exe, path=os.pathsep.join(dirs)) is not None
        return None if ok else f"process: {exe!r} not found (PATH / VOICE_BACKEND_CWD)"
    return None


def doctor(platform: str | None = None) -> dict:
    """Preflight without a session. Never prints a secret value; opens nothing."""
    from voice import config as voice_config
    from voice import platform as voice_platform
    from voice.live import providers

    voice_config.load_env_file(force=True)   # this process may have read another path
    platform = platform or voice_platform.PLATFORM
    provider = voice_config.cfg("VOICE_LIVE_PROVIDER", providers.DEFAULT_PROVIDER)
    backend = voice_config.cfg("VOICE_BACKEND", "claude_code")
    profile = providers.get(provider)
    names = tuple(profile.required_env) if profile is not None else None
    missing = ([n for n in names if not voice_config.cfg(n).strip()] if names is not None
               else None)
    backend_problem = backend_check(backend, voice_config.cfg, platform)
    modules = {m: importlib.util.find_spec(m) is not None for m in ("websockets", "sounddevice")}
    audio = {"input_devices": None, "output_devices": None}
    if modules["sounddevice"]:
        try:
            import sounddevice as sd
            devs = sd.query_devices()
            audio = {"input_devices": sum(1 for d in devs if d["max_input_channels"] > 0),
                     "output_devices": sum(1 for d in devs if d["max_output_channels"] > 0)}
        except Exception as exc:          # PortAudio missing: report, never raise
            audio["error"] = type(exc).__name__
    report = {
        "platform": platform,
        # A misconfigured value can be a pasted secret: print names through the redactor.
        "provider": voice_config.redact_text(provider),
        "provider_known": names is not None,
        "credentials_missing": missing,
        "backend": voice_config.redact_text(backend),
        "backend_problem": (voice_config.redact_text(backend_problem)
                            if backend_problem else None),
        # claude_code proves ownership at start: from the Orca pane with --terminal (dialogs
        # observable), else from the session's own transcript (no dialogs).
        "session_binding": "checked at start" if backend == "claude_code" else "n/a",
        "orca_pane": orca_pane_state(backend),
        "modules": modules,
        "audio": audio,
        "env_file": str(voice_config.env_file_path()),
        "state_dir": os.environ.get(STATE_DIR_NAME, ""),
    }
    report = voice_config.redact_tree(report)
    report["ready"] = bool(names is not None and not missing and backend_problem is None
                           and all(modules.values())
                           and (audio.get("input_devices") or 0) > 0
                           and (audio.get("output_devices") or 0) > 0)
    return report


def orca_pane_state(backend: str, environ=os.environ, which=None) -> str:
    """Whether a `start` here would try to bind an Orca pane, and if not, why."""
    if backend != "claude_code":
        return "n/a"
    if not environ.get(ORCA_HANDLE_NAME, "").strip():
        return "not in an Orca pane"
    if environ.get(ORCA_OPT_OUT_NAME, "").strip().lower() in {"0", "false", "no", "off"}:
        return f"off ({ORCA_OPT_OUT_NAME})"
    if (which or shutil.which)("orca") is None:
        return "orca CLI not on PATH"
    return "tried at start"


def pane_proves(parsed, handle: str) -> bool:
    """The daemon's own pane proof, run once ahead of it. Read-only and claims nothing, so a
    refusal leaves the start exactly as it was; the daemon proves ownership again itself.
    It holds when the host has drawn this Bash call (measured in Orca 1.4.200: bound on the
    first read, 0.15-0.21 s); a call drawn off screen (tmux, a headless or parallel call)
    stays on the screenless proof."""
    import asyncio

    from voice.app import daemon

    session = daemon.resolve_session_id(parsed.session)
    claude_pid = daemon.find_claude_ancestor(session)
    if not (parsed.nonce and claude_pid):
        return False
    try:
        proof = asyncio.run(daemon.prove_ownership(
            nonce=parsed.nonce, session_id=session, terminal=handle, claude_pid=claude_pid,
            run=daemon._pane_runner(), read_bound_s=daemon.PANE_READ_BOUND_S))
    except Exception:          # a pane that cannot be read is not a reason to refuse a start
        return False
    return bool(proof["binding"].get("bound"))


def with_orca_pane(args: list[str], environ=os.environ, which=None, prove=None) -> list[str]:
    """`args` plus `--terminal <this pane>` when a `start` can bind the Orca pane."""
    from voice import config as voice_config
    from voice.app import daemon
    from voice.backend import registry as backends

    # The daemon's own parser, silenced: a bad command line is the daemon's to report, once.
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            parsed = daemon.build_parser().parse_args(args)
    except SystemExit:
        return args
    if parsed.command != "start" or parsed.terminal or parsed.status:   # --status starts nothing
        return args
    backend = parsed.backend or voice_config.cfg("VOICE_BACKEND", backends.DEFAULT_BACKEND)
    if orca_pane_state(backend, environ, which) != "tried at start":
        return args
    handle = environ[ORCA_HANDLE_NAME].strip()
    if not (prove or pane_proves)(parsed, handle):
        return args
    return ["--terminal", handle, *args]


def statusline(stdin, environ=os.environ, alive=None) -> str:
    """The segment text, or "" — the same rule as the daemon's statusline segment: the call is
    running, its relay is qualified, it has not ended, and its process is still there."""
    try:
        if stdin.isatty():                            # run by hand: nothing to read
            return ""
        session = str(json.loads(stdin.read() or "{}").get("session_id") or "")
        if not SESSION_ID.fullmatch(session):         # a name, never a path (or a drive)
            return ""
        root = Path(environ.get(STATE_DIR_NAME) or user_state_dir(environ))
        status = json.loads((root / session / "status.json").read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            return ""
        if status.get("phase") != "running" or status.get("relay") != "qualified" \
                or status.get("ended"):
            return ""
        if not (alive or _alive)(status.get("pid")):
            return ""
    except (OSError, ValueError, AttributeError, TypeError, OverflowError):
        return ""
    segment = "🎙 voice ↻" if status.get("reconnecting") is True else "🎙 voice"
    # What the call console's band shows, in short: words heard and not handed over yet, and
    # spoken messages waiting behind the agent's running turn.
    unsent = status.get("unsent")
    if isinstance(unsent, dict) and unsent.get("chars"):
        segment += " ✎"
    queued = status.get("queued")
    if isinstance(queued, list) and queued:
        segment += f" ⇪{len(queued)}"
    return segment


def _alive(pid) -> bool:
    if type(pid) is not int or not 0 < pid < 2 ** 31:
        return False
    if os.name == "nt":
        return True          # os.kill(pid, 0) sends CTRL_C_EVENT on Windows; trust `ended`
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    apply_defaults()
    if args[:1] == ["statusline"]:
        print(statusline(sys.stdin), end="")
        return 0
    if args[:1] == ["doctor"]:
        report = doctor()
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 1
    from voice.app import daemon
    return daemon.main(with_orca_pane(args))


if __name__ == "__main__":
    raise SystemExit(main())
