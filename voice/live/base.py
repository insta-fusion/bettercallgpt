"""The live seam — what any realtime voice provider must look like to the agent loop.

Three things live here and nothing else: the PROVIDER-NEUTRAL EVENT (`LiveEvent`) a strategy
receives, the CONTRACT a strategy yields to the loop (`Request` / `TranscriptFailed`), and the
INTENTS the loop speaks back through (`VoicePort`, with the provider's declared
`Capabilities`). A provider adapter (`realtime.py`, `gpt_live.py`) turns its wire into `LiveEvent`s;
a strategy (`strategy_function.py`, `strategy_delegation.py`) turns that provider's agency
mechanism — function calls, or metadata-only delegations — into the contract. The loop, the
broker and the backends never see a provider event name.

Identity is the whole point of the event shape: every event carries whatever the wire gave it
(response id, item id, input item id, millisecond ranges, delegation id, generation), and
nothing here decides meaning. Audio bytes never ride in an event — they go to the `AudioSink`.

Design: voice/DESIGN.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable

# Who asked for a response. A function call joins ONLY a `user`-origin response; a broker
# challenge or a narration can never sit between a committed input item and its call.
# `farewell`: the one narration the daemon waits for before it closes the socket.
Origin = Literal["user", "challenge", "narration", "farewell"]

Priority = Literal["now", "next"]

# Provider-neutral event kinds. A provider adapter maps its own names onto these and MUST NOT
# invent others; a strategy that needs a kind not listed here changes this file first.
KIND_SESSION_STARTED = "session.started"
KIND_INPUT_COMMITTED = "input.committed"          # input_item_id
KIND_INPUT_TRANSCRIPT = "input.transcript"        # input_item_id, payload.text, payload.complete (bool)
KIND_INPUT_SPEECH_STARTED = "input.speech_started"
KIND_INPUT_SPEECH_STOPPED = "input.speech_stopped"
KIND_RESPONSE_CREATED = "response.created"        # response_id, payload.origin
KIND_RESPONSE_DONE = "response.done"              # response_id, payload.status (completed|cancelled|failed)
KIND_OUTPUT_ITEM = "output.item"                  # response_id, item_id, payload.type (audio|function_call|message)
KIND_OUTPUT_AUDIO = "output.audio"                # response_id, item_id, payload.frames (int) — bytes went to the sink
KIND_OUTPUT_TRANSCRIPT = "output.transcript"      # response_id, item_id, payload.text, payload.done
KIND_CALL = "call"                                # response_id, item_id, payload.call_id/name/arguments (dict or None)
KIND_DELEGATION_CREATED = "delegation.created"    # delegation_id, offset_ms (GPT-Live)
KIND_APPENDED = "appended"                        # payload.kind (instructions|thinking|commentary), start_ms/end_ms
KIND_ERROR = "error"                              # payload.message
KIND_DISCONNECTED = "disconnected"                # generation that died
KIND_CLOSED = "closed"


@dataclass(frozen=True)
class LiveEvent:
    seq: int                                  # client-side monotonic, per session
    kind: str
    generation: int = 0                       # socket generation; bumps on reconnect
    response_id: str | None = None
    item_id: str | None = None
    input_item_id: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    offset_ms: int | None = None
    delegation_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)   # never audio bytes


# ------------------------------------------------------------------ the contract
# What a strategy yields to the agent loop. These are the ONLY things code acts on.

@dataclass(frozen=True)
class Request:
    """The operator's words, routed by the voice model, bound to the turn that produced them."""
    transcript: str                 # raw ASR of the input item — evidence, never edited
    interpretation: str             # the model's `text` argument — interpretation, never edited
    priority: Priority
    input_item_id: str
    response_id: str
    call_id: str
    generation: int


@dataclass(frozen=True)
class TranscriptFailed:
    """The provider committed an input item but could not transcribe it. Terminal for that
    item: no Request may be built on it, and any armed effect awaiting the next input item is
    consumed (tombstoned) by it."""
    input_item_id: str
    generation: int


Decision = Request | TranscriptFailed

# A Request requires a `user`-origin response uniquely joined to one committed input item with
# a COMPLETE transcript. A call arriving on a challenge / narration response is never a
# decision — those responses are created with tool_choice none, and a call on them is a
# provider fault to log and refuse. Response origin is correlated by provider identity
# (Realtime: `response.metadata` set on response.create and echoed on response.created),
# never by arrival order alone.


@dataclass(frozen=True)
class SessionConfig:
    instructions: str
    voice: str
    tools: list[dict[str, Any]]              # the closed schema; [] for providers without tools
    tool_choice: str = "auto"
    extra: dict[str, Any] = field(default_factory=dict)   # provider-specific, passed through


class AudioSink(Protocol):
    """Where output audio goes. The sink owns the audio epoch; the strategy tells it which
    response id is current and which one was cancelled."""

    def play(self, response_id: str, item_id: str, pcm: bytes) -> None: ...
    def cancel(self, response_id: str) -> int:
        """Drop everything queued for this response id; return the frames actually rendered."""
        ...
    def rendered_ms(self, response_id: str, item_id: str) -> int | None:
        """Milliseconds of this item actually rendered, or None if no frames were rendered."""
        ...
    def reached_end(self, response_id: str) -> bool:
        """True once every frame the provider sent for this response id has been rendered
        (the response is done AND the queue for it drained to the device). The broker's third
        delivery evidence."""
        ...
    def epoch_unchanged(self, response_id: str) -> bool:
        """True iff no audio epoch advance (cancel) happened since this response's first frame
        was rendered — i.e. the operator did not interrupt it."""
        ...


class LiveSession(Protocol):
    """One provider connection — what EVERY provider has, and all the daemon and the loop may
    assume: open it, stream the microphone up, read events, close it."""

    generation: int

    async def start(self, config: SessionConfig) -> None: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    def events(self) -> AsyncIterator[LiveEvent]: ...
    async def close(self) -> None: ...


@runtime_checkable
class RealtimeControls(Protocol):
    """OPTIONAL verbs only the realtime family has (responses, items, function calls,
    cancel, truncate). Implemented by `RealtimeSession`; GPT-Live has none of them. Code that
    needs them is realtime-family code (`FunctionStrategy`, `SessionVoice`), built only for a
    provider whose `Capabilities.response_identity` is true — callers check the capability,
    never catch a missing verb."""

    async def create_response(self, origin: Origin, *, instructions: str | None = None,
                              tool_choice: str | None = None) -> None:
        """Ask the provider for a response, tagged with who asked. Serialized: the next
        `response.created` event is attributed to this origin."""
        ...

    async def add_item(self, item: dict[str, Any], origin: Origin) -> None:
        """A conversation item (system message, challenge text, availability notice)."""
        ...

    async def call_output(self, call_id: str, output: dict[str, Any]) -> None: ...
    async def cancel_response(self, response_id: str) -> None: ...
    async def truncate(self, item_id: str, audio_end_ms: int) -> None: ...


class Strategy(Protocol):
    """Turns a provider's agency mechanism into the contract. Owns the join
    input item → user-origin response → call, and yields nothing until the join is unique
    and the input transcript is complete."""

    async def feed(self, event: LiveEvent) -> list[Decision]: ...


# ------------------------------------------------------------------ the way back
# What the loop SAYS to a provider, as intents rather than wire verbs. The realtime wire has
# responses, items, calls, cancel and truncate; GPT-Live has none of those — it has quiet
# context, spoken commentary and a delegation id. Asking every provider to pretend to have
# the realtime verbs would mean inventing response ids, which is the one thing this seam
# exists to never do. So the loop names what it wants, and each provider's port does it.


@dataclass(frozen=True)
class Capabilities:
    """What a provider's wire can PROVE, declared by its profile, never inferred at runtime.

    The loop and the daemon switch on these and never on a provider's name.
    """
    # Responses carry ids the provider echoes (created/done, per-response audio). Without it
    # there is no delivery evidence for a spoken consent challenge (broker → terminal-only),
    # no runaway cut and no response-keyed goodbye.
    response_identity: bool
    # The service subtracts its own voice from the microphone. Without it the provider hears
    # itself on open speakers — use headphones.
    server_echo_cancellation: bool
    # The wire has its own input mute. A provider capability only: the daemon has no operator
    # mute (its controls are start / status / stop).
    input_mute: bool
    # The model routes by a function call (`TOOLS`); otherwise by the provider's own mechanism.
    function_tools: bool
    # The wire reports that the operator STOPPED speaking (a server VAD `speech_stopped`, an
    # input item committed after it). Without it, nothing on the wire proves the operator
    # is done: once they speak, a rollover cannot find quiet, and the provider's own close
    # (a reconnect) carries the call past its session limit instead.
    speech_end: bool = True
    # How much recap a fresh provider session takes when a relay carries the conversation
    # over (`VoicePort.seed`), in UTF-8 bytes. A size, not a duration.
    recap_bytes: int = 2000


class VoicePort(Protocol):
    """The loop's only way to put words into a live conversation."""

    capabilities: Capabilities

    async def context(self, text: str) -> None:
        """Quiet context: the model may use it, is not asked to speak."""
        ...

    async def announce(self, text: str, origin: Origin) -> None:
        """Words the model is asked to speak now (greeting, goodbye, a dialog waiting)."""
        ...

    async def challenge(self, text: str) -> None:
        """A consent challenge to be said in exactly these words."""
        ...

    async def receipt(self, call_id: str, output: dict[str, Any], *, speak: bool) -> None:
        """Where a dispatch went (posted / refused / uncertain), bound to the decision that
        asked. `speak`: the operator has not heard anything for this turn yet."""
        ...

    async def result(self, text: str, *, answers: list[str] | None) -> None:
        """A backend result. `answers` holds the `call_id` of every voice request the turn
        absorbed (in order) — then it is spoken once; None means context only (a typed turn,
        the relay probe)."""
        ...

    async def cut(self, response_id: str) -> None:
        """Stop a runaway response on the wire. Only meaningful with `response_identity`."""
        ...

    async def seed(self, recap: str) -> None:
        """A relay's recap, into a FRESH provider session before anything else is said: the
        recent turns and the requests still in flight, as quiet context. How it goes in is the
        provider's own mechanism (a system item; `thinking` appends); never spoken."""
        ...
