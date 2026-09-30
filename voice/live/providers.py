"""The provider registry — which conversation model the voice talks through.

One row per `VOICE_LIVE_PROVIDER` value. Each row is a `ProviderProfile`: the credential env
NAMES it needs, how to build its session, its strategy and its `VoicePort`, the provider-shaped
session fields, the tools it exposes, its persona prompt, and its declared `Capabilities`.
The daemon asks this module for a profile by name and never names a provider itself.

    voice_live  Azure Voice Live (realtime wire)       realtime.py   + strategy_function.py
    openai      OpenAI Realtime  (realtime wire)       realtime.py   + strategy_function.py
    gpt_live    Azure GPT-Live   (gpt-live-1)          gpt_live.py   + strategy_delegation.py

Adding a provider = a wire adapter, a strategy, and one row here.

Values are read through `voice.config.cfg` at BUILD time, never at import, and never printed:
the credential check names what is missing and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from voice import config as voice_config

from .base import Capabilities
from .port import SessionVoice

DEFAULT_PROVIDER = "voice_live"


@dataclass(frozen=True)
class Bounds:
    """The daemon's lifecycle ports, handed through to the session. No defaults here."""
    open_s: float
    close_s: float
    reconnect_s: float


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    required_env: tuple[str, ...]
    # (sink, connect, bounds) -> LiveSession
    build_session: Callable[[Any, Any, Bounds], Any]
    # (session, sink) -> Strategy
    build_strategy: Callable[[Any, Any], Any]
    # (session) -> VoicePort
    build_voice: Callable[[Any], Any]
    # () -> provider-specific session fields, passed through untouched by the adapter
    session_extra: Callable[[], dict[str, Any]]
    # () -> the closed tool schema the session exposes ([] without function tools)
    tools: Callable[[], list[dict[str, Any]]]
    prompt: str                      # agency fragment: voice/prompts/providers/<prompt>
    capabilities: Capabilities


# ------------------------------------------------------------------ the realtime family

VOICE_LIVE_CAPS = Capabilities(response_identity=True, server_echo_cancellation=True,
                               input_mute=False, function_tools=True)
OPENAI_CAPS = Capabilities(response_identity=True, server_echo_cancellation=False,
                           input_mute=False, function_tools=True)


def _realtime_tools() -> list[dict[str, Any]]:
    from .strategy_function import TOOLS

    return TOOLS


def _function_strategy(session: Any, sink: Any) -> Any:
    from .strategy_function import FunctionStrategy

    return FunctionStrategy(session, sink)


def _voice_live_session(sink: Any, connect: Any, bounds: Bounds) -> Any:
    from .realtime import RealtimeSession

    return RealtimeSession(
        provider="voice_live",
        model=voice_config.cfg("VOICE_MODEL", "gpt-realtime-2.1"),
        api_key=voice_config.cfg("AZURE_OPENAI_API_KEY"),
        endpoint=voice_config.cfg("AZURE_OPENAI_ENDPOINT"),
        api_version=voice_config.cfg("VOICE_LIVE_API_VERSION", "2026-07-15"),
        host_override=voice_config.cfg("VOICE_LIVE_HOST"),
        sink=sink, connect=connect,
        open_bound_s=bounds.open_s, close_bound_s=bounds.close_s,
        reconnect_bound_s=bounds.reconnect_s)


def _openai_session(sink: Any, connect: Any, bounds: Bounds) -> Any:
    from .realtime import RealtimeSession

    return RealtimeSession(
        provider="openai",
        model=voice_config.cfg("VOICE_MODEL", "gpt-realtime"),
        api_key=voice_config.cfg("OPENAI_API_KEY"), sink=sink, connect=connect,
        open_bound_s=bounds.open_s, close_bound_s=bounds.close_s,
        reconnect_bound_s=bounds.reconnect_s)


def _voice_live_extra() -> dict[str, Any]:
    vad = voice_config.cfg("VOICE_LIVE_VAD", "azure_semantic_vad")
    extra: dict[str, Any] = {
        "turn_detection": {"type": vad},
        # The reason the Voice Live transport exists: the service subtracts its own
        # voice, so the mic can stay open on speakers without self-interrupting.
        "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
        "input_audio_transcription": {"model": voice_config.cfg(
            "VOICE_TRANSCRIBE_MODEL", "whisper-1")},
    }
    effort = voice_config.cfg("VOICE_REASONING_EFFORT").strip()
    if effort:
        extra["reasoning_effort"] = effort
    return extra


def _openai_extra() -> dict[str, Any]:
    return {"turn_detection": {"type": "server_vad"}}


# ------------------------------------------------------------------ GPT-Live

def _gpt_live_session(sink: Any, connect: Any, bounds: Bounds) -> Any:
    from .gpt_live import GptLiveSession

    # Its own names: gpt-live-1 is deployed on a different resource (Canada Central) from the
    # Voice Live one, and a key is per-resource, never per-region.
    return GptLiveSession(
        model=voice_config.cfg("VOICE_GPT_LIVE_MODEL", "gpt-live-1"),
        api_key=voice_config.cfg("VOICE_GPT_LIVE_API_KEY"),
        endpoint=voice_config.cfg("VOICE_GPT_LIVE_ENDPOINT"),
        sink=sink, connect=connect,
        open_bound_s=bounds.open_s, close_bound_s=bounds.close_s,
        reconnect_bound_s=bounds.reconnect_s)


def _delegation_strategy(session: Any, sink: Any) -> Any:
    from .strategy_delegation import DelegationStrategy

    return DelegationStrategy(session, sink)


def _gpt_live_voice(session: Any) -> Any:
    from .gpt_live import GptLiveVoice

    return GptLiveVoice(session)


def _gpt_live_caps() -> Capabilities:
    from .gpt_live import CAPABILITIES

    return CAPABILITIES


# ------------------------------------------------------------------ the table

PROVIDERS: dict[str, ProviderProfile] = {
    "voice_live": ProviderProfile(
        name="voice_live",
        required_env=("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"),
        build_session=_voice_live_session,
        build_strategy=_function_strategy,
        build_voice=lambda session: SessionVoice(session, VOICE_LIVE_CAPS),
        session_extra=_voice_live_extra,
        tools=_realtime_tools,
        prompt="relay_tool.md",
        capabilities=VOICE_LIVE_CAPS),
    "openai": ProviderProfile(
        name="openai",
        required_env=("OPENAI_API_KEY",),
        build_session=_openai_session,
        build_strategy=_function_strategy,
        build_voice=lambda session: SessionVoice(session, OPENAI_CAPS),
        session_extra=_openai_extra,
        tools=_realtime_tools,
        prompt="relay_tool.md",
        capabilities=OPENAI_CAPS),
    "gpt_live": ProviderProfile(
        name="gpt_live",
        required_env=("VOICE_GPT_LIVE_ENDPOINT", "VOICE_GPT_LIVE_API_KEY"),
        build_session=_gpt_live_session,
        build_strategy=_delegation_strategy,
        build_voice=_gpt_live_voice,
        session_extra=dict,
        tools=list,
        prompt="delegation.md",
        capabilities=_gpt_live_caps()),
}


def names() -> list[str]:
    return sorted(PROVIDERS)


def get(name: str) -> ProviderProfile | None:
    return PROVIDERS.get(name)
