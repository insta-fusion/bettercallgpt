"""DESIGN.md §Acceptance A1, A2, D3 — the join, the transcript gate, and ack-is-not-dispatch.

These are the assertions that keep a dispatch honest: it must name the one input item that
produced it, and it must not exist before that item's words do.
"""
from __future__ import annotations

import unittest

from voice.conversation.log import ConversationLog


class A1JoinTests(unittest.TestCase):
    def test_A1_join_names_the_single_candidate_input_item(self):
        log = ConversationLog()
        log.commit_input("item-1")
        log.complete_transcript("item-1", "把测试跑一下")
        log.response_created("resp-1", "user")

        item_id, reason = log.join("resp-1")

        self.assertEqual(item_id, "item-1")
        self.assertEqual(reason, "joined")

    def test_A1_broker_challenge_response_is_excluded_from_the_join(self):
        """A response our own code asked for can never carry a dispatchable call."""
        log = ConversationLog()
        log.commit_input("item-1")
        log.complete_transcript("item-1", "把测试跑一下")
        log.response_created("resp-challenge", "challenge")

        item_id, reason = log.join("resp-challenge")

        self.assertIsNone(item_id)
        self.assertEqual(reason, "not_user_origin")

    def test_A1_interleaved_challenge_does_not_steal_the_candidate(self):
        """The challenge response is created between the item and the user response. The
        candidate set was frozen at the challenge, so the user response has none — refused,
        never attributed to the likelier item."""
        log = ConversationLog()
        log.commit_input("item-1")
        log.complete_transcript("item-1", "把测试跑一下")
        log.response_created("resp-challenge", "challenge")
        log.response_created("resp-user", "user")

        self.assertEqual(log.join("resp-challenge"), (None, "not_user_origin"))
        self.assertEqual(log.join("resp-user"), (None, "no_candidate"))

    def test_A1_two_candidate_items_refuse_rather_than_guess(self):
        log = ConversationLog()
        log.commit_input("item-1")
        log.commit_input("item-2")
        log.complete_transcript("item-1", "第一句")
        log.complete_transcript("item-2", "第二句")
        log.response_created("resp-1", "user")

        item_id, reason = log.join("resp-1")

        self.assertIsNone(item_id)
        self.assertEqual(reason, "ambiguous")

    def test_A1_unknown_response_refuses(self):
        self.assertEqual(ConversationLog().join("nope"), (None, "no_response"))


class A2TranscriptGateTests(unittest.TestCase):
    def test_A2_dispatch_waits_for_transcript_when_the_call_arrives_first(self):
        log = ConversationLog()
        log.commit_input("item-1")
        log.response_created("resp-1", "user")

        self.assertEqual(log.join("resp-1"), (None, "transcript_pending"))

        log.complete_transcript("item-1", "把测试跑一下")
        self.assertEqual(log.join("resp-1"), ("item-1", "joined"))

    def test_A2_transcript_before_call_joins_immediately(self):
        log = ConversationLog()
        log.commit_input("item-1")
        log.complete_transcript("item-1", "把测试跑一下")
        log.response_created("resp-1", "user")

        self.assertEqual(log.join("resp-1"), ("item-1", "joined"))

    def test_A2_request_carries_transcript_and_interpretation_unedited(self):
        log = ConversationLog()
        log.commit_input("item-1")
        log.complete_transcript("item-1", "把测试跑一下 ,谢谢")
        log.response_created("resp-1", "user")
        record = log.open_request("req-1", input_item_id="item-1", response_id="resp-1",
                                  call_id="call-1", interpretation="运行测试套件",
                                  priority="now")

        self.assertEqual(record.transcript, "把测试跑一下 ,谢谢")
        self.assertEqual(record.interpretation, "运行测试套件")


class A2RevisionTests(unittest.TestCase):
    def test_A2_revision_advances_once_per_completed_turn(self):
        log = ConversationLog()
        self.assertEqual(log.revision, 0)
        log.commit_input("item-1")
        log.complete_transcript("item-1", "一")
        self.assertEqual(log.revision, 1)
        log.commit_input("item-2")
        log.complete_transcript("item-2", "二")
        self.assertEqual(log.revision, 2)

    def test_A2_a_committed_item_alone_does_not_advance_the_revision(self):
        log = ConversationLog()
        log.commit_input("item-1")
        self.assertEqual(log.revision, 0)


class D3AckIsNotDispatchTests(unittest.TestCase):
    def test_D3_speech_without_a_call_records_no_request(self):
        """The S2 shape: the model spoke, the model acknowledged, the model never called."""
        log = ConversationLog()
        log.commit_input("item-1")
        log.complete_transcript("item-1", "顺便把 lint 跑了")
        log.response_created("resp-1", "user")
        log.response_done("resp-1", "completed")

        self.assertEqual(list(log.requests()), [])
        self.assertEqual(log.unresolved(), [])


if __name__ == "__main__":
    unittest.main()


class NarrationNeverStealsTheCandidate(unittest.TestCase):
    """Words from the terminal or the daemon — a result, the greeting, the goodbye — are not a
    turn: a narration response created between the operator's item and the user response
    must leave the candidate pool alone (found by review: the first request after the
    greeting was refused `no_candidate`)."""

    def test_narration_between_item_and_user_response_leaves_the_candidate(self):
        log = ConversationLog()
        log.commit_input("item-1")
        log.complete_transcript("item-1", "帮我看一下", failed=False)
        log.response_created("resp-greeting", "narration")
        log.response_created("resp-user", "user")
        self.assertEqual(log.join("resp-user"), ("item-1", "joined"))
        self.assertEqual(log.join("resp-greeting"), (None, "not_user_origin"))

    def test_a_challenge_still_freezes_the_pool(self):
        log = ConversationLog()
        log.commit_input("item-1")
        log.complete_transcript("item-1", "删掉它", failed=False)
        log.response_created("resp-challenge", "challenge")
        log.response_created("resp-user", "user")
        self.assertEqual(log.join("resp-user")[0], None)
