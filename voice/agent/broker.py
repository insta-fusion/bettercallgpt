"""The effect broker — the one deterministic gate in a system that otherwise decides nothing.

Everywhere else the conversation model owns meaning. Here it does not, and the reason is
measured, not theoretical: on the best-behaved provider the model approved `rm -rf build/` on an
unrelated 「对」 in four runs out of five (spike R4). So consent for an irreversible effect is
taken out of the model's hands entirely and put into a shape the model cannot forge.

The shape is a challenge and a password.

1. **Arm from the harness, never from the model.** An effect exists only because
   `backend.observe()` reported an open dialog with an occurrence id and an extractable action
   and scope. There is no consent tool in the schema; a model call could not arm one if it tried.
2. **Speak the challenge, and prove it was spoken.** Delivery needs all three: the challenge
   response finished, its own output transcript CONTAINS our full challenge text after
   normalization, and the audio sink rendered that response to its end with the epoch unchanged.
   A paraphrase is not a delivery. An interruption is not a delivery.
3. **The next thing the operator says consumes the arm, whatever it is.** Execute only if that
   item's completed transcript, normalized, EQUALS the phrase. Empty, failed, near-miss, changed
   occurrence — all refuse. An arm can never outlive one utterance.

The confirming phrase is the ONE place a fixed string decides an outcome. It is not a keyword
table: the phrase is a property of the armed challenge, it is compared only while that effect is
armed, and it routes nothing — a miss refuses, it never selects a different action. Phrases are
at least four syllables because both providers dropped utterances under about two seconds.

Pure: no async, no I/O, no clock. The loop does the speaking and the pressing; this decides.

Design: voice/DESIGN.md §Consent, §Acceptance C1–C6.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Literal

# --------------------------------------------------------------------------- normalization

# Punctuation stripped at TOKEN EDGES only, so an utterance differs from the phrase by its
# punctuation alone and never by its words. Interior characters are untouched.
_EDGE_PUNCTUATION = "，。、；：！？「」『』（）《》〈〉…—·,.;:!?\"'()[]{}<>-_~`"


def normalize(text: str) -> str:
    """The one normalization both the challenge check and the consent check use.

    NFKC (so full-width and half-width forms compare equal), whitespace collapsed to nothing for
    CJK-safe comparison, punctuation stripped at token edges, case-folded. One function, because
    two would eventually disagree and the disagreement would be a consent bug.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    tokens = [token.strip(_EDGE_PUNCTUATION) for token in folded.split()]
    joined = "".join(token for token in tokens if token)
    return joined.strip(_EDGE_PUNCTUATION)


def contains_challenge(spoken: str, challenge: str) -> bool:
    """Whether the model's rendering actually carried our words. Containment, not similarity."""
    normalized_challenge = normalize(challenge)
    if not normalized_challenge:
        return False
    return normalized_challenge in normalize(spoken)


# --------------------------------------------------------------------------- phrases

# The confirming phrases. Each is a property of the challenge the broker composes — carried on
# the armed effect, compared only against the one utterance that consumes that arm, and routing
# nothing. Every one is at least four syllables (R13: shorter utterances were dropped by ASR).
#
# A phrase names an INDEX, never an intent. There is no "allow" phrase and no "deny" phrase,
# because deciding which rendered option counts as allowing would mean reading the option's
# words — the judgement this whole layer refuses to make. The operator hears the options read
# out in order and names the one they want by position.
_CN_ORDINALS = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")

PHRASE_CANCEL = "确认取消选择"


def ordinal_word(index: int) -> str:
    """The spoken ordinal for a position. Beyond ten, the digits are spoken as they are."""
    return _CN_ORDINALS[index - 1] if 1 <= index <= len(_CN_ORDINALS) else str(index)


def option_phrase(index: int) -> str:
    """The confirming phrase for choosing the option at this position."""
    return f"确认选择第{ordinal_word(index)}个"


# --------------------------------------------------------------------------- state

ArmState = Literal["armed", "delivered", "executed", "refused", "tombstoned"]


@dataclass(frozen=True)
class Challenge:
    """What must be spoken, and the password that answers it. Both belong to this effect."""
    text: str                    # the full sentence the model must render
    phrase: str                  # the exact confirming utterance
    choice: str                  # what to press on the backend if the phrase matches


@dataclass
class ArmedEffect:
    effect_id: str
    occurrence_id: str
    revision: int
    kind: str
    prompt: str                  # the harness's own words, verbatim
    options: tuple[tuple[str, str], ...]     # (key, label) exactly as rendered
    header: str                  # the prompt and options, read out once
    challenge: Challenge
    alternatives: tuple[Challenge, ...] = ()   # deny variant, other options
    state: ArmState = "armed"
    response_id: str | None = None             # the challenge response, once spoken
    # Delivery evidence, all three required.
    evidence_done: bool = False
    evidence_transcript: bool = False
    evidence_rendered: bool = False
    reason: str = ""

    @property
    def delivered(self) -> bool:
        return self.evidence_done and self.evidence_transcript and self.evidence_rendered

    def phrases(self) -> tuple[Challenge, ...]:
        return (self.challenge,) + self.alternatives


@dataclass(frozen=True)
class Verdict:
    """What the broker decided about one consuming utterance."""
    outcome: Literal["execute", "refuse", "tombstone", "ignore"]
    effect_id: str | None = None
    occurrence_id: str | None = None
    choice: str | None = None
    reason: str = ""


@dataclass
class Broker:
    """Holds at most one armed effect. A second dialog replaces the first by tombstoning it —
    two live passwords at once would be a way to answer the wrong question correctly."""

    terminal_only: bool = False          # set by the GPT-Live strategy: no delivery evidence exists
    armed: ArmedEffect | None = None
    executed: set[str] = field(default_factory=set)
    history: list[Verdict] = field(default_factory=list)
    _pending_header: str = ""

    # ------------------------------------------------------------------ arming

    def arm(self, dialog, revision: int) -> ArmedEffect | None:
        """Arm from a `Dialog` OBSERVATION. Returns None with a tombstone when it cannot be armed.

        `dialog` is `voice.backend.base.Dialog`; typed structurally so this file imports nothing
        from the backend seam and stays pure.
        """
        if self.terminal_only:
            self._tombstone_current("replaced_by_terminal_only")
            self.history.append(Verdict("refuse", None, getattr(dialog, "occurrence_id", None),
                                        reason="terminal_only"))
            return None
        occurrence_id = getattr(dialog, "occurrence_id", None)
        prompt = str(getattr(dialog, "prompt", "") or "")
        options = tuple(getattr(dialog, "options", ()) or ())
        # STRUCTURE, not wording, is what makes a dialog armable: an occurrence to bind to, the
        # harness's own prompt to read back, and at least two options we can name by position.
        # An option list that could not be enumerated is terminal-only by construction.
        if not occurrence_id or not prompt or len(options) < 2:
            self.history.append(Verdict("refuse", None, occurrence_id, reason="not_enumerable"))
            return None

        kind = str(getattr(dialog, "kind", "dialog") or "dialog")
        effect_id = f"{occurrence_id}:{revision}"
        primary, alternatives = self._compose(prompt, options)
        self._tombstone_current("replaced_by_new_dialog")
        self.armed = ArmedEffect(
            effect_id=effect_id,
            occurrence_id=occurrence_id,
            revision=revision,
            kind=kind,
            prompt=prompt,
            options=options,
            header=self._pending_header,
            challenge=primary,
            alternatives=alternatives,
        )
        return self.armed

    def _compose(self, prompt: str,
                 options: tuple[tuple[str, str], ...]) -> tuple[Challenge, tuple[Challenge, ...]]:
        """Compose the challenge from what the screen SHOWED: the prompt verbatim, then the
        options read out in rendered order, each with the phrase that picks it.

        Nothing here interprets an option. The old version named an action and a scope parsed out
        of the prompt, which meant a mis-parse would have had the operator confirming a sentence
        that did not describe what was about to happen. Reading the harness's own words back and
        letting the operator choose by position removes the parse entirely.
        """
        spoken = "；".join(f"{ordinal_word(position)}、{label}"
                          for position, (_key, label) in enumerate(options, start=1))
        primary_key, _primary_label = options[0]
        header = f"后台在问：{prompt}。选项：{spoken}。"

        def challenge_for(position: int, key: str) -> Challenge:
            phrase = option_phrase(position)
            # `text` is this option's own clause. The shared header is prepended once by
            # `challenge_text`, because the delivery check compares the model's rendering
            # against the WHOLE spoken challenge and a header repeated per option would make a
            # correct reading fail containment.
            return Challenge(text=f"要选第{ordinal_word(position)}个，就说「{phrase}」",
                             phrase=phrase, choice=key)

        primary = challenge_for(1, primary_key)
        alternatives = tuple(challenge_for(position, key)
                             for position, (key, _label) in enumerate(options[1:], start=2))
        # Cancelling is always available and names no option, so it needs no reading of one.
        cancel = Challenge(text=f"要取消，就说「{PHRASE_CANCEL}」",
                           phrase=PHRASE_CANCEL, choice="cancel")
        self._pending_header = header
        return primary, alternatives + (cancel,)


    def challenge_text(self) -> str | None:
        """The full sentence the model must speak: the harness's prompt and options once, then
        every phrase that can answer it. This exact string is what the delivery check looks for
        in the model's own output transcript, so a paraphrase or a truncation is not a delivery.
        """
        if self.armed is None:
            return None
        clauses = "；".join(challenge.text for challenge in self.armed.phrases())
        return f"{self.armed.header}{clauses}；说别的都不算。"


    def speaking(self, response_id: str) -> None:
        """The loop created the challenge response. Bind the arm to that response id: evidence
        from any other response is not this challenge's evidence."""
        if self.armed is not None:
            self.armed.response_id = response_id

    # ------------------------------------------------------------------ delivery evidence

    def note_response_done(self, response_id: str, status: str) -> None:
        if self.armed is None or self.armed.response_id != response_id:
            return
        if status == "completed":
            self.armed.evidence_done = True
        else:
            self._tombstone_current(f"challenge_{status}")

    def note_output_transcript(self, response_id: str, text: str) -> None:
        """Our own words checked against the model's rendering. A paraphrase fails here."""
        if self.armed is None or self.armed.response_id != response_id:
            return
        full = self.challenge_text() or ""
        if contains_challenge(text, full):
            self.armed.evidence_transcript = True

    def note_rendered(self, response_id: str, *, reached_end: bool, epoch_unchanged: bool) -> None:
        """The audio sink played that response to its last frame without the epoch moving."""
        if self.armed is None or self.armed.response_id != response_id:
            return
        if reached_end and epoch_unchanged:
            self.armed.evidence_rendered = True
        else:
            self._tombstone_current("challenge_interrupted")

    def is_delivered(self) -> bool:
        return self.armed is not None and self.armed.delivered

    # ------------------------------------------------------------------ consent

    def consume(self, transcript: str | None, *, current_occurrence_id: str | None,
                failed: bool = False) -> Verdict:
        """The next committed input item after delivery. It consumes the arm WHATEVER it says.

        This is the whole gate. Everything that is not the exact phrase refuses; refusal is not a
        second chance, because the arm is gone either way.
        """
        effect = self.armed
        if effect is None:
            return self._record(Verdict("ignore", reason="nothing_armed"))
        if not effect.delivered:
            # An undelivered challenge was never a question the operator could answer.
            self._clear()
            return self._record(Verdict("tombstone", effect.effect_id, effect.occurrence_id,
                                        reason="not_delivered"))
        self._clear()
        if effect.effect_id in self.executed:
            return self._record(Verdict("refuse", effect.effect_id, effect.occurrence_id,
                                        reason="already_executed"))
        if failed or not (transcript or "").strip():
            return self._record(Verdict("tombstone", effect.effect_id, effect.occurrence_id,
                                        reason="empty_transcript"))
        if current_occurrence_id != effect.occurrence_id:
            return self._record(Verdict("refuse", effect.effect_id, effect.occurrence_id,
                                        reason="occurrence_changed"))
        spoken = normalize(transcript or "")
        for candidate in effect.phrases():
            if spoken == normalize(candidate.phrase):
                self.executed.add(effect.effect_id)
                return self._record(Verdict("execute", effect.effect_id, effect.occurrence_id,
                                            choice=candidate.choice, reason="phrase_matched"))
        return self._record(Verdict("refuse", effect.effect_id, effect.occurrence_id,
                                    reason="phrase_mismatch"))

    # ------------------------------------------------------------------ lifecycle

    def dialog_closed(self, occurrence_id: str) -> None:
        if self.armed is not None and self.armed.occurrence_id == occurrence_id:
            self._tombstone_current("dialog_closed")

    def reset(self, reason: str = "reconnect") -> ArmedEffect | None:
        """A reconnect or a session reset clears every arm: the challenge the operator heard
        belongs to a session that no longer exists."""
        dead = self.armed
        self._tombstone_current(reason)
        return dead

    def _tombstone_current(self, reason: str) -> None:
        if self.armed is None:
            return
        effect = self.armed
        effect.state = "tombstoned"
        effect.reason = reason
        self.history.append(Verdict("tombstone", effect.effect_id, effect.occurrence_id,
                                    reason=reason))
        self.armed = None

    def _clear(self) -> None:
        self.armed = None

    def _record(self, verdict: Verdict) -> Verdict:
        self.history.append(verdict)
        return verdict
