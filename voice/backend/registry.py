"""The harness registry — which agent the voice hands work to.

One row per `VOICE_BACKEND` value: a `BackendProfile` with how to build it and what it can do
(`BackendCapabilities`), including WHICH OPERATING SYSTEMS it runs on. The daemon asks this
module by name and never branches on a backend's name itself; an unknown harness, an unknown
process dialect or an unsupported OS is refused at startup with `Unsupported`, before any paid
session opens.

    claude_code   Claude Code over its relay socket + transcript      macOS only (LOCAL_PEERPID, ps)
                  (with an Orca pane: screen proof + dialog observation; without one — any
                  terminal; the Claude desktop app is UNVERIFIED — ownership is proven from
                  the session's own transcript and no dialog is observed)
    process       a headless CLI child speaking JSONL (dialects)     macOS, Linux (SIGINT stop)

Adding a harness = a Backend implementation and one row here. Adding a `process` dialect = a
parser in `process.DIALECTS`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from voice import config as voice_config


class Unsupported(RuntimeError):
    """A harness, dialect or platform this build cannot run. Raised at startup, never mid-call."""


@dataclass(frozen=True)
class BackendCapabilities:
    # Operating systems it runs on (`voice.platform.PLATFORM` values); None = any.
    platforms: frozenset[str] | None
    # It can surface and answer the harness's permission dialogs (else approvals stay in the
    # terminal and the voice only narrates that one is waiting).
    dialogs: bool
    # Proven in a live session with the operator, not only against fakes.
    verified_live: bool
    why_platform: str = ""


@dataclass(frozen=True)
class BuildContext:
    """What a builder may need from the daemon. Nothing here is read from the environment."""
    # claude_code is built by the daemon from the ownership handshake's proven binding; the
    # daemon hands its builder in rather than this module learning the handshake.
    claude_code: Callable[[], Any] | None = None


@dataclass(frozen=True)
class BackendProfile:
    name: str
    build: Callable[[BuildContext], Any]
    capabilities: BackendCapabilities
    # The persona's consent paragraph (voice/prompts/backends/<prompt>).
    prompt: str = "terminal.md"
    # Side-effect-free configuration check shared by preflight and `build`.
    check: Callable[[], Any] | None = None
    # The launcher must prove the session is the operator's (the `--nonce` ownership
    # handshake) before the daemon starts; its proof is what `build` binds to.
    ownership_handshake: bool = False
    # Permission dialogs are observable only with a screen to read (`--terminal`); without
    # one they stay in the terminal.
    dialogs_need_screen: bool = False


def _check_process() -> tuple[str, Any]:
    """(argv, parser) for the configured process child, or `Unsupported`. No side effects:
    preflight and the builder share it, so `--status` refuses what `start` would refuse."""
    from voice.backend import process as process_mod

    argv = voice_config.cfg("VOICE_PROCESS_ARGV").strip()
    if not argv:
        raise Unsupported("VOICE_BACKEND=process needs VOICE_PROCESS_ARGV "
                          "(the child command line, e.g. 'codex exec --json')")
    dialect = voice_config.cfg("VOICE_PROCESS_DIALECT", "codex_exec")
    parser = process_mod.DIALECTS.get(dialect)
    if parser is None:
        raise Unsupported(f"VOICE_PROCESS_DIALECT={dialect!r} is unsupported; "
                          f"registered: {sorted(process_mod.DIALECTS)}")
    return argv, parser


def _build_process(ctx: BuildContext) -> Any:
    import shlex

    from voice.backend import process as process_mod

    argv, parser = _check_process()
    return process_mod.ProcessBackend(process_mod.ChildSpec(
        argv=tuple(shlex.split(argv)), parser=parser,
        cwd=voice_config.cfg("VOICE_BACKEND_CWD") or None))


def _build_claude_code(ctx: BuildContext) -> Any:
    if ctx.claude_code is None:
        raise Unsupported(
            "VOICE_BACKEND=claude_code is built by `build_claude_code_backend`, which needs "
            "the proven binding from the ownership handshake. The daemon calls it directly.")
    return ctx.claude_code()


BACKENDS: dict[str, BackendProfile] = {
    "claude_code": BackendProfile(
        name="claude_code", build=_build_claude_code, prompt="dialog.md",
        ownership_handshake=True, dialogs_need_screen=True,
        capabilities=BackendCapabilities(
            platforms=frozenset({"darwin"}), dialogs=True, verified_live=True,
            why_platform="the relay proves its peer with LOCAL_PEERPID and the pane probe "
                         "reads `ps -o lstart`, both macOS-only")),
    "process": BackendProfile(
        name="process", build=_build_process, check=_check_process,
        capabilities=BackendCapabilities(
            platforms=frozenset({"darwin", "linux"}), dialogs=False, verified_live=False,
            why_platform="the child is stopped with SIGINT, which Windows does not deliver")),
}


# The harness a start uses when VOICE_BACKEND names none.
DEFAULT_BACKEND = "claude_code"


def names() -> list[str]:
    return sorted(BACKENDS)


def get(name: str) -> BackendProfile | None:
    return BACKENDS.get(name)


def require(name: str, platform: str) -> BackendProfile:
    """The profile, or `Unsupported` naming what is wrong: an unknown harness, or an OS it
    does not run on."""
    profile = BACKENDS.get(name)
    if profile is None:
        raise Unsupported(f"unknown VOICE_BACKEND {name!r}; expected one of {names()}")
    allowed = profile.capabilities.platforms
    if allowed is not None and platform not in allowed:
        raise Unsupported(f"VOICE_BACKEND={name} is unsupported on {platform}: "
                          f"{profile.capabilities.why_platform or 'not built for it'}")
    if profile.check is not None:
        profile.check()
    return profile
