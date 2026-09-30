"""`VoicePort` over a wire that has responses, items and calls — the realtime family.

Every intent here is the exact call sequence the loop used to make on the session itself, so
moving the loop onto the port changed nothing on the wire: a system message is an item, a
spoken line is that item plus ONE `response.create` with `tool_choice: "none"` on that
response only, and a receipt is a `function_call_output` (plus one response when the operator
has not heard anything for the turn yet).

GPT-Live's port lives beside its adapter (`gpt_live.py`); this one is shared by every
provider whose session implements the realtime item verbs.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import redact_text, redact_tree
from .base import Capabilities, Origin, RealtimeControls

# The realtime family's wire, as declared for a session nobody described (tests, and any
# caller that hands the loop a bare session). The provider profiles declare their own.
RESPONSE_WIRE = Capabilities(response_identity=True, server_echo_cancellation=False,
                             input_mute=False, function_tools=True)


def message(text: str) -> dict[str, Any]:
    """A legal system message carrying `text` verbatim."""
    return {"type": "message", "role": "system",
            "content": [{"type": "input_text", "text": text}]}


class RedactedVoice:
    """Any `VoicePort`, with known secret values masked in every word it sends.

    The provider hears what the terminal shows: agent progress and results, typed input,
    dialog prompts. A credential the daemon itself holds (any secret-named setting, see
    `voice.config.redact_text`) must never ride along. This wraps the port the provider
    profile built, so every provider and every intent is covered in one place. A challenge
    that carried a secret is spoken masked; its delivery evidence then fails and consent
    refuses — the safe direction.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.capabilities = getattr(inner, "capabilities", RESPONSE_WIRE)

    def __getattr__(self, name: str) -> Any:        # anything that carries no words
        return getattr(self._inner, name)

    async def context(self, text: str) -> None:
        await self._inner.context(redact_text(text))

    async def announce(self, text: str, origin: Origin) -> None:
        await self._inner.announce(redact_text(text), origin)

    async def challenge(self, text: str) -> None:
        await self._inner.challenge(redact_text(text))

    async def receipt(self, call_id: str, output: dict[str, Any], *, speak: bool) -> None:
        await self._inner.receipt(call_id, redact_tree(output), speak=speak)

    async def result(self, text: str, *, answers: list[str] | None) -> None:
        await self._inner.result(redact_text(text), answers=answers)

    async def cut(self, response_id: str) -> None:
        await self._inner.cut(response_id)

    async def seed(self, recap: str) -> None:
        await self._inner.seed(redact_text(recap))


class SessionVoice:
    """The realtime verbs, driven by intent."""

    capabilities: Capabilities = RESPONSE_WIRE

    def __init__(self, session: RealtimeControls,
                 capabilities: Capabilities = RESPONSE_WIRE) -> None:
        self.session = session
        self.capabilities = capabilities

    async def context(self, text: str) -> None:
        await self.session.add_item(message(text), origin="narration")

    async def announce(self, text: str, origin: Origin) -> None:
        await self.session.add_item(message(text), origin=origin)
        await self.session.create_response(origin=origin, tool_choice="none")

    async def challenge(self, text: str) -> None:
        await self.session.add_item(message(json.dumps({"challenge": text}, ensure_ascii=False)),
                                    origin="challenge")
        # `tool_choice="none"` IS THE POINT: the challenge must be said out loud in our exact
        # words, and an unspoken challenge can never gather its delivery evidence.
        await self.session.create_response(origin="challenge", instructions=text,
                                           tool_choice="none")

    async def receipt(self, call_id: str, output: dict[str, Any], *, speak: bool) -> None:
        # `tool_choice="none"` on THIS response only: a response that answers a receipt must
        # speak, not call again (variant A of R11 fired `request` three times for one sentence).
        await self.session.call_output(call_id, output)
        if speak:
            await self.session.create_response(origin="narration", tool_choice="none")

    async def result(self, text: str, *, answers: list[str] | None) -> None:
        await self.session.add_item(message(text), origin="narration")
        if answers is not None:
            await self.session.create_response(origin="narration", tool_choice="none")

    async def cut(self, response_id: str) -> None:
        await self.session.cancel_response(response_id)

    async def seed(self, recap: str) -> None:
        # One system item and no response: the model reads it, nobody hears it.
        await self.session.add_item(message(recap), origin="narration")
