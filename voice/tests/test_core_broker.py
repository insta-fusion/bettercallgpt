"""DESIGN.md §Acceptance C1–C6 — the consent gate.

The rows this file answers are the ones the spike said we would get wrong if we trusted the
model: C5 replays the very transcript that made gpt-realtime approve `rm -rf build/` in four runs
out of five, and asserts the broker refuses it.
"""
from __future__ import annotations

import json
import pathlib
import unittest

from voice.agent.broker import (
    PHRASE_CANCEL,
    Broker,
    contains_challenge,
    normalize,
    option_phrase,
    ordinal_word,
)
from voice.backend.base import Dialog

FIXTURES = pathlib.Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"


def permission_dialog(occurrence_id="occ-1", prompt="Bash wants to run rm -rf build/ ?",
                      options=(("1", "Yes"), ("2", "No"))):
    """A dialog exactly as the pane hands it over: verbatim prompt, rendered options, and NO
    parsed action or scope — those are always None now."""
    return Dialog(occurrence_id=occurrence_id, kind="dialog", prompt=prompt,
                  action=None, scope=None, options=options)


def question_dialog(occurrence_id="occ-q"):
    return Dialog(occurrence_id=occurrence_id, kind="dialog", prompt="Which branch?",
                  action=None, scope=None,
                  options=(("1", "main"), ("2", "dev")))


def deliver(broker, response_id="resp-challenge"):
    """Give an armed effect all three pieces of delivery evidence."""
    broker.speaking(response_id)
    broker.note_output_transcript(response_id, broker.challenge_text())
    broker.note_response_done(response_id, "completed")
    broker.note_rendered(response_id, reached_end=True, epoch_unchanged=True)


class NormalizationTests(unittest.TestCase):
    def test_normalization_is_one_function_over_nfkc_space_punctuation_case(self):
        self.assertEqual(normalize("确认允许执行。"), normalize("确认允许执行"))
        self.assertEqual(normalize(" 确认  允许 执行 "), normalize("确认允许执行"))
        self.assertEqual(normalize("ＣＯＮＦＩＲＭ"), normalize("confirm"))
        self.assertEqual(normalize("「确认允许执行」"), normalize("确认允许执行"))

    def test_normalization_does_not_erase_interior_words(self):
        self.assertNotEqual(normalize("确认允许执行了吧"), normalize("确认允许执行"))

    def test_containment_is_over_normalized_text(self):
        self.assertTrue(contains_challenge("好的。 确认允许执行 ,请说。", "确认允许执行"))
        self.assertFalse(contains_challenge("请确认一下", "确认允许执行"))


class C1ArmingTests(unittest.TestCase):
    def test_C1_arms_from_an_occurrence_a_verbatim_prompt_and_enumerable_options(self):
        broker = Broker()
        effect = broker.arm(permission_dialog(), revision=3)
        self.assertIsNotNone(effect)
        self.assertEqual(effect.occurrence_id, "occ-1")
        self.assertEqual(effect.revision, 3)
        self.assertEqual(effect.challenge.phrase, option_phrase(1))
        self.assertEqual(effect.challenge.choice, "1", "the choice is an INDEX, not an intent")

    def test_C1_refuses_to_arm_when_the_options_cannot_be_enumerated(self):
        """No structural option list means nothing can be named by position, so there is no
        phrase to arm. Terminal-only by construction."""
        broker = Broker()
        bare = Dialog(occurrence_id="occ-2", kind="dialog", prompt="something",
                      action=None, scope=None, options=())
        self.assertIsNone(broker.arm(bare, revision=1))
        self.assertIsNone(broker.armed)

    def test_C1_refuses_to_arm_a_single_option_dialog(self):
        broker = Broker()
        one = Dialog(occurrence_id="occ-3", kind="dialog", prompt="ok?",
                     action=None, scope=None, options=(("1", "OK"),))
        self.assertIsNone(broker.arm(one, revision=1))

    def test_C1_refuses_to_arm_without_a_verbatim_prompt(self):
        broker = Broker()
        blank = Dialog(occurrence_id="occ-4", kind="dialog", prompt="",
                       action=None, scope=None, options=(("1", "Yes"), ("2", "No")))
        self.assertIsNone(broker.arm(blank, revision=1))

    def test_C1_no_option_label_is_ever_interpreted(self):
        """The labels could say anything in any language; the broker reads them ALOUD and never
        reads them FOR meaning. Two dialogs with unrelated labels arm identically."""
        first = Broker()
        first.arm(permission_dialog(options=(("1", "Yes"), ("2", "No"))), revision=1)
        second = Broker()
        second.arm(permission_dialog(options=(("1", "继续"), ("2", "算了"))), revision=1)
        self.assertEqual([c.phrase for c in first.armed.phrases()],
                         [c.phrase for c in second.armed.phrases()])
        self.assertEqual([c.choice for c in first.armed.phrases()],
                         [c.choice for c in second.armed.phrases()])

    def test_C1_challenge_text_reads_the_prompt_and_options_verbatim(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        text = broker.challenge_text()
        self.assertIn("Bash wants to run rm -rf build/ ?", text, "the harness's own words")
        self.assertIn("Yes", text)
        self.assertIn("No", text)
        self.assertIn(option_phrase(1), text)
        self.assertIn(option_phrase(2), text)
        self.assertIn(PHRASE_CANCEL, text)

    def test_C1_the_prompt_is_read_once_not_per_option(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        self.assertEqual(broker.challenge_text().count("Bash wants to run"), 1)

    def test_C1_every_rendered_option_gets_its_own_phrase(self):
        broker = Broker()
        broker.arm(question_dialog(), revision=1)
        phrases = [c.phrase for c in broker.armed.phrases()]
        self.assertIn(option_phrase(1), phrases)
        self.assertIn(option_phrase(2), phrases)
        for phrase in phrases:
            self.assertGreaterEqual(len(phrase), 4, "under ~2s utterances were dropped by ASR")

    def test_C1_a_phrase_names_a_position_not_an_intent(self):
        self.assertEqual(option_phrase(1), "确认选择第一个")
        self.assertEqual(option_phrase(3), "确认选择第三个")
        self.assertEqual(ordinal_word(2), "二")


class C2DeliveryEvidenceTests(unittest.TestCase):
    def test_C2_all_three_pieces_make_a_delivery(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        deliver(broker)
        self.assertTrue(broker.is_delivered())

    def test_C2_a_paraphrased_challenge_is_not_delivered(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        broker.speaking("resp-challenge")
        broker.note_output_transcript("resp-challenge", "后台想删点东西,你同意吗?")
        broker.note_response_done("resp-challenge", "completed")
        broker.note_rendered("resp-challenge", reached_end=True, epoch_unchanged=True)
        self.assertFalse(broker.is_delivered())

    def test_C2_an_interrupted_challenge_is_tombstoned(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        broker.speaking("resp-challenge")
        broker.note_output_transcript("resp-challenge", broker.challenge_text())
        broker.note_response_done("resp-challenge", "completed")
        broker.note_rendered("resp-challenge", reached_end=False, epoch_unchanged=True)
        self.assertIsNone(broker.armed)
        self.assertEqual(broker.history[-1].reason, "challenge_interrupted")

    def test_C2_a_moved_audio_epoch_tombstones_the_challenge(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        broker.speaking("resp-challenge")
        broker.note_rendered("resp-challenge", reached_end=True, epoch_unchanged=False)
        self.assertIsNone(broker.armed)

    def test_C2_a_cancelled_challenge_response_is_tombstoned(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        broker.speaking("resp-challenge")
        broker.note_response_done("resp-challenge", "cancelled")
        self.assertIsNone(broker.armed)

    def test_C2_evidence_from_another_response_id_does_not_count(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        broker.speaking("resp-challenge")
        broker.note_output_transcript("resp-other", broker.challenge_text())
        broker.note_response_done("resp-other", "completed")
        broker.note_rendered("resp-other", reached_end=True, epoch_unchanged=True)
        self.assertFalse(broker.is_delivered())


class C3ConsentTests(unittest.TestCase):
    def setUp(self):
        self.broker = Broker()
        self.broker.arm(permission_dialog(), revision=1)
        deliver(self.broker)

    def test_C3_the_exact_phrase_executes(self):
        verdict = self.broker.consume(option_phrase(1), current_occurrence_id="occ-1")
        self.assertEqual(verdict.outcome, "execute")
        self.assertEqual(verdict.choice, "1")

    def test_C3_each_phrase_executes_its_own_index(self):
        verdict = self.broker.consume(option_phrase(2), current_occurrence_id="occ-1")
        self.assertEqual(verdict.outcome, "execute")
        self.assertEqual(verdict.choice, "2")

    def test_C3_the_cancel_phrase_executes_cancel(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        deliver(broker)
        verdict = broker.consume(PHRASE_CANCEL, current_occurrence_id="occ-1")
        self.assertEqual(verdict.outcome, "execute")
        self.assertEqual(verdict.choice, "cancel")

    def test_C3_near_misses_all_refuse(self):
        for spoken in ("确认", "第一个", "确认选择第一个吗", "确认选择第一个了吧",
                       "选择第一个", "确认选择第二个个"):
            with self.subTest(spoken=spoken):
                broker = Broker()
                broker.arm(permission_dialog(), revision=1)
                deliver(broker)
                verdict = broker.consume(spoken, current_occurrence_id="occ-1")
                self.assertEqual(verdict.outcome, "refuse", spoken)
                self.assertEqual(verdict.reason, "phrase_mismatch")

    def test_C3_the_arm_is_consumed_whatever_the_transcript_says(self):
        self.broker.consume("随便说点别的", current_occurrence_id="occ-1")
        self.assertIsNone(self.broker.armed)
        again = self.broker.consume(option_phrase(1), current_occurrence_id="occ-1")
        self.assertEqual(again.outcome, "ignore")

    def test_C3_an_empty_or_failed_transcript_tombstones(self):
        verdict = self.broker.consume("", current_occurrence_id="occ-1")
        self.assertEqual(verdict.outcome, "tombstone")
        self.assertEqual(verdict.reason, "empty_transcript")

    def test_C3_a_failed_transcription_tombstones_even_with_text(self):
        verdict = self.broker.consume("确认允许执行", current_occurrence_id="occ-1", failed=True)
        self.assertEqual(verdict.outcome, "tombstone")

    def test_C3_punctuation_and_spacing_around_the_phrase_still_execute(self):
        verdict = self.broker.consume("「确认选择第一个」。", current_occurrence_id="occ-1")
        self.assertEqual(verdict.outcome, "execute")

    def test_C3_an_undelivered_challenge_never_executes(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        verdict = broker.consume(option_phrase(1), current_occurrence_id="occ-1")
        self.assertEqual(verdict.outcome, "tombstone")
        self.assertEqual(verdict.reason, "not_delivered")


class C4LifecycleTests(unittest.TestCase):
    def test_C4_executes_once_per_effect_id(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        deliver(broker)
        self.assertEqual(broker.consume(option_phrase(1),
                                        current_occurrence_id="occ-1").outcome, "execute")
        # Same dialog, same revision → same effect id. It cannot execute twice.
        broker.arm(permission_dialog(), revision=1)
        deliver(broker, "resp-challenge-2")
        verdict = broker.consume(option_phrase(1), current_occurrence_id="occ-1")
        self.assertEqual(verdict.outcome, "refuse")
        self.assertEqual(verdict.reason, "already_executed")

    def test_C4_occurrence_change_between_arm_and_answer_refuses(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        deliver(broker)
        verdict = broker.consume(option_phrase(1), current_occurrence_id="occ-999")
        self.assertEqual(verdict.outcome, "refuse")
        self.assertEqual(verdict.reason, "occurrence_changed")

    def test_C4_reset_clears_the_arm(self):
        broker = Broker()
        broker.arm(permission_dialog(), revision=1)
        deliver(broker)
        dead = broker.reset("reconnect")
        self.assertIsNotNone(dead)
        self.assertIsNone(broker.armed)
        self.assertEqual(broker.consume(option_phrase(1),
                                        current_occurrence_id="occ-1").outcome, "ignore")

    def test_C4_a_new_dialog_replaces_and_tombstones_the_old_arm(self):
        broker = Broker()
        broker.arm(permission_dialog("occ-1"), revision=1)
        deliver(broker)
        broker.arm(permission_dialog("occ-2"), revision=2)
        self.assertEqual(broker.armed.occurrence_id, "occ-2")
        self.assertIn("replaced_by_new_dialog", [v.reason for v in broker.history])

    def test_C4_dialog_closed_clears_its_own_arm_only(self):
        broker = Broker()
        broker.arm(permission_dialog("occ-1"), revision=1)
        broker.dialog_closed("occ-other")
        self.assertIsNotNone(broker.armed)
        broker.dialog_closed("occ-1")
        self.assertIsNone(broker.armed)


class C5ReplayTests(unittest.TestCase):
    """The R4 run: the model called `confirm_effect(yes)` on an unrelated 「对」. The broker must
    refuse the very same utterance."""

    def _r4_transcripts(self) -> list[str]:
        path = FIXTURES / "voice-rt-R4.jsonl"
        texts: list[str] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") == "conversation.item.input_audio_transcription.completed":
                    texts.append(str(record.get("transcript") or ""))
        return texts

    def test_C5_the_R4_fixture_carries_the_unrelated_agreement(self):
        texts = self._r4_transcripts()
        self.assertTrue(texts, "no completed transcripts parsed from voice-rt-R4.jsonl")
        self.assertTrue(any("对" in text for text in texts), texts)

    def test_C5_an_unrelated_agreement_after_a_challenge_never_executes(self):
        for spoken in self._r4_transcripts():
            with self.subTest(spoken=spoken):
                broker = Broker()
                broker.arm(permission_dialog(), revision=1)
                deliver(broker)
                verdict = broker.consume(spoken, current_occurrence_id="occ-1")
                self.assertNotEqual(verdict.outcome, "execute", spoken)

    def test_C5_a_model_shaped_proposal_cannot_arm_an_effect(self):
        """R4's `propose_effect` arguments, offered directly to the broker: no occurrence id, no
        extracted action, so there is nothing to arm. Arming needs a harness observation."""
        broker = Broker()
        proposal = json.loads(
            '{"kind":"answer_dialog","target_id":"permission_rm_rf_build",'
            '"scope":"允许运行 rm -rf build/ 吗?"}')

        class _ModelProposal:
            occurrence_id = None
            kind = proposal["kind"]
            prompt = proposal["scope"]
            action = None
            scope = proposal["scope"]
            options = ()

        self.assertIsNone(broker.arm(_ModelProposal(), revision=1))
        self.assertIsNone(broker.armed)


class C6TerminalOnlyTests(unittest.TestCase):
    def test_C6_the_gpt_live_strategy_refuses_to_arm(self):
        broker = Broker(terminal_only=True)
        self.assertIsNone(broker.arm(permission_dialog(), revision=1))
        self.assertIsNone(broker.armed)
        self.assertEqual(broker.history[-1].reason, "terminal_only")

    def test_C6_nothing_can_be_consented_to_in_terminal_only_mode(self):
        broker = Broker(terminal_only=True)
        broker.arm(permission_dialog(), revision=1)
        verdict = broker.consume(option_phrase(1), current_occurrence_id="occ-1")
        self.assertEqual(verdict.outcome, "ignore")


if __name__ == "__main__":
    unittest.main()
