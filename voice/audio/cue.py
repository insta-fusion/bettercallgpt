"""The daemon's two sounds: up when the relay is ready, down when the session ends.

Not speech and not the model's. The operator hears the edges of a session without a word
being said, so neither sound can be mistaken for a result, and the model is never asked to
script a greeting. Pure PCM through the same sink the model speaks through: nothing here is
platform-specific.
"""

from __future__ import annotations

import math
import struct

from voice.audio.io import BLOCK_FRAMES, SAMPLE_RATE

# Six blocks — a payload size, like BLOCK_FRAMES, not a duration knob.
CUE_FRAMES = BLOCK_FRAMES * 6
# Two notes a fifth apart; "start" rises through them, "stop" falls.
_NOTES_HZ = (523.25, 783.99)
_PEAK = 12_000                     # well under pcm16 full scale, so it never clips
START = "start"
STOP = "stop"
ITEM_ID = "cue"


# Every cue's sink key starts with this; nothing a provider sends does.
KEY_PREFIX = "cue:"


def response_id(kind: str) -> str:
    """The reserved sink key, so a cue never shares an epoch with a model response."""
    return f"{KEY_PREFIX}{kind}"


def earcon(kind: str) -> bytes:
    """pcm16 mono at the sink's rate. Each note fades in and out so it lands click-free."""
    low, high = _NOTES_HZ
    notes = (low, high) if kind == START else (high, low)
    half = CUE_FRAMES // 2
    out = bytearray()
    for hz in notes:
        for i in range(half):
            # A triangular envelope: silent at the first and last sample of each note.
            env = 1.0 - abs((2.0 * i / (half - 1)) - 1.0)
            sample = int(_PEAK * env * math.sin(2.0 * math.pi * hz * i / SAMPLE_RATE))
            out += struct.pack("<h", sample)
    return bytes(out)
