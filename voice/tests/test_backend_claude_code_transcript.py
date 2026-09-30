"""DESIGN.md §Acceptance E3, E5 — receipt attribution by exact wire equality, and transcript rotation.

Covers fixtures 3 and 5: registered wire receipts (attribution, whole-record equality, once per
tag), rotation replay (rebuild without re-emitting, truncation), a rotation into an unreadable
file fencing, and the split-record and fence behaviour those depend on. Mid-turn assistant text
arrives as `assistant_text` and a completed turn as `turn_result`.

Every case writes a REAL file on disk in byte-sized pieces, because partial reads, inode changes
and truncation are the properties under test and none of them exist in a mocked file.
"""
from __future__ import annotations

import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path

from voice.backend.claude_code.transcript import (
    UNKNOWN,
    Tailer,
    collapse,
    initial_state,
    reduce_transcript,
    tags_in,
)

FIXTURE_SESSION = "sess-fixture"
TAG_A = "⟨v#aaaa1111⟩"
TAG_B = "⟨v#bbbb2222⟩"

# Real lineage records carry `parentUuid`, null ONLY on the session's first. The rotation
# continuity check reads exactly that, so a builder that wants a session ORIGIN says parent=None
# and means it.
_MID_CONVERSATION = "parent-uuid-0000"


def kinds(events) -> list[str]:
    return [event["kind"] for event in events]


def user_str(text: str, uuid: str = "u1", session: str = FIXTURE_SESSION,
             parent: str | None = _MID_CONVERSATION) -> dict:
    return {"type": "user", "uuid": uuid, "sessionId": session, "parentUuid": parent,
            "message": {"role": "user", "content": text}}


def assistant_tool(tool_use_id: str, text: str = "", session: str = FIXTURE_SESSION,
                   parent: str | None = _MID_CONVERSATION) -> dict:
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.append({"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {}})
    return {"type": "assistant", "uuid": "a-" + tool_use_id, "sessionId": session,
            "parentUuid": parent,
            "message": {"role": "assistant", "stop_reason": "tool_use", "content": blocks}}


def assistant_end(text: str = "done", session: str = FIXTURE_SESSION,
                  parent: str | None = _MID_CONVERSATION) -> dict:
    return {"type": "assistant", "uuid": "a-end", "sessionId": session, "parentUuid": parent,
            "message": {"role": "assistant", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": text}]}}


def enqueue(content: str | None, session: str = FIXTURE_SESSION) -> dict:
    record = {"type": "queue-operation", "operation": "enqueue", "sessionId": session,
              "timestamp": "2026-09-07T00:00:00.000Z"}
    if content is not None:
        record["content"] = content
    return record


class _Case(unittest.TestCase):
    """Base: a real file on disk, written in byte-sized pieces."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="voice-cc-transcript-"))
        self.path = self.dir / "session.jsonl"
        self.path.write_bytes(b"")
        self._rotations = itertools.count()

    def tailer(self, session_id: str = FIXTURE_SESSION) -> Tailer:
        tail = Tailer(self.path, session_id=session_id)
        tail.bootstrap()
        return tail

    def append(self, blob: bytes) -> None:
        with open(self.path, "ab") as handle:
            handle.write(blob)

    def line(self, record: dict) -> bytes:
        # ensure_ascii=False so non-ASCII really lands as multi-byte UTF-8 on disk, which is
        # what the split-character case exercises.
        return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")

    def rotate_to(self, blob: bytes) -> None:
        """Replace the transcript with a NEW INODE holding `blob` — what a real rotation does."""
        replacement = self.dir / f"rot-{next(self._rotations)}.jsonl"
        replacement.write_bytes(blob)
        os.replace(replacement, self.path)


class E3ReceiptEqualityTests(_Case):
    """E3 — a REGISTERED tag is consumed only on whole-record equality with the wire."""

    def test_E3_an_exact_wire_match_is_a_consumed_receipt(self):
        tail = self.tailer()
        wire = f"run the tests {TAG_A}"
        tail.register(TAG_A, wire)
        self.append(self.line(user_str(wire)))
        events = tail.poll()
        consumed = [event for event in events if event["kind"] == "consumed"]
        self.assertEqual(len(consumed), 1)
        self.assertEqual(consumed[0]["tag"], TAG_A)

    def test_E3_a_containing_string_is_never_credited(self):
        """`do not run the tests ⟨tag⟩` CONTAINS the fragment. Crediting it would report a
        sentence as delivered that the operator never said."""
        tail = self.tailer()
        wire = f"run the tests {TAG_A}"
        tail.register(TAG_A, wire)
        self.append(self.line(user_str(f"do not {wire}")))
        events = tail.poll()
        self.assertNotIn("consumed", kinds(events))
        self.assertIn("tag_seen_unresolved", kinds(events))

    def test_E3_a_tag_quoted_in_assistant_prose_is_not_a_receipt(self):
        tail = self.tailer()
        tail.register(TAG_A, f"run the tests {TAG_A}")
        self.append(self.line(assistant_tool("t1", text=f"I saw {TAG_A} go by")))
        events = tail.poll()
        self.assertNotIn("consumed", kinds(events))

    def test_E3_a_tag_is_credited_at_most_once(self):
        tail = self.tailer()
        wire = f"run the tests {TAG_A}"
        tail.register(TAG_A, wire)
        self.append(self.line(user_str(wire, uuid="u1")))
        first = tail.poll()
        self.append(self.line(user_str(wire, uuid="u2")))
        second = tail.poll()
        self.assertEqual(kinds(first).count("consumed"), 1)
        self.assertNotIn("consumed", kinds(second))

    def test_E3_an_unregistered_tag_is_not_deduped_and_carries_no_wire_proof(self):
        """An unregistered tag was never something WE sent, so the whole-record equality rule
        has no wire to check it against. The event is emitted but it is not a proof of OUR
        delivery, and the once-per-tag gate — which exists to stop a rebuild re-crediting a
        registered send — deliberately does not apply to it."""
        tail = self.tailer()
        self.append(self.line(user_str(f"something {TAG_B}", uuid="u1")))
        first = tail.poll()
        self.append(self.line(user_str(f"something {TAG_B}", uuid="u2")))
        second = tail.poll()
        self.assertIn("consumed", kinds(first))
        self.assertIn("consumed", kinds(second), "no dedupe without a registered wire")
        self.assertEqual(tail.registered(), ())

    def test_E3_only_a_registered_tag_is_deduped_across_a_rebuild(self):
        tail = self.tailer()
        wire = f"run the tests {TAG_A}"
        tail.register(TAG_A, wire)
        self.append(self.line(user_str(wire, uuid="u1")))
        self.assertIn("consumed", kinds(tail.poll()))
        self.append(self.line(user_str(wire, uuid="u2")))
        self.assertNotIn("consumed", kinds(tail.poll()))

    def test_E3_collapse_is_the_one_normalization(self):
        self.assertEqual(collapse("  run   the  tests  "), "run the tests")

    def test_E3_tags_come_back_in_order_of_appearance(self):
        self.assertEqual(tags_in(f"{TAG_B} then {TAG_A}"), (TAG_B, TAG_A))


class E5RotationTests(_Case):
    """E5 — rotation survives: the history rebuilds without re-emitting what it already said."""

    def test_E5_rotation_rebuilds_without_re_emitting(self):
        tail = self.tailer()
        wire = f"run the tests {TAG_A}"
        tail.register(TAG_A, wire)
        first = self.line(user_str(wire)) + self.line(assistant_end("done"))
        self.append(first)
        before = tail.poll()
        self.assertIn("consumed", kinds(before))

        # A rotation that CONTINUES the history: same prefix, one record more.
        self.rotate_to(first + self.line(user_str("next thing", uuid="u2")))
        after = tail.poll()
        self.assertNotIn("consumed", kinds(after),
                         "a rebuilt history must not re-credit a tag")

    def test_E5_truncation_to_a_shorter_file_rebuilds(self):
        tail = self.tailer()
        self.append(self.line(user_str("one", uuid="u1"))
                    + self.line(user_str("two", uuid="u2")))
        tail.poll()
        self.rotate_to(self.line(user_str("one", uuid="u1")))
        tail.poll()
        self.assertIsNotNone(tail.state)

    def test_E5_a_rotation_into_an_unreadable_file_fences(self):
        """Evidence gone means activity and queue balance park at UNKNOWN, and a caller that
        gates on either refuses. Failing closed is the whole point."""
        tail = self.tailer()
        self.append(self.line(user_str("one")))
        tail.poll()
        self.path.unlink()
        tail.poll()
        state = tail.state
        self.assertEqual(state["activity"], UNKNOWN)
        self.assertEqual(state["queue_balance"], UNKNOWN)

    def test_E5_a_split_record_yields_nothing_until_the_line_completes(self):
        tail = self.tailer()
        blob = self.line(user_str(f"重启服务 {TAG_A}"))
        self.append(blob[:len(blob) // 2])
        self.assertEqual(tuple(tail.poll()), ())
        self.append(blob[len(blob) // 2:])
        self.assertNotEqual(tuple(tail.poll()), ())

    def test_E5_a_partial_tail_fences_the_state_and_unfences_itself(self):
        """A half-written record is fenced by the partial buffer, which clears the moment the
        record completes — unlike a vanished file, which is unrecoverable."""
        tail = self.tailer()
        blob = self.line(user_str("hello"))
        self.append(blob[:-5])
        tail.poll()
        self.assertEqual(tail.state["activity"], UNKNOWN)
        self.append(blob[-5:])
        tail.poll()
        self.assertNotEqual(tail.state["activity"], UNKNOWN)


class ReducerShapeTests(unittest.TestCase):
    """The one cut: structural event names, and no `tool_preamble` anywhere."""

    def test_mid_turn_text_is_assistant_text(self):
        state = initial_state()
        state, _events = reduce_transcript(state, user_str("do it"))
        state, events = reduce_transcript(state, assistant_tool("t1", text="working on it"))
        text_events = [event for event in events if event["kind"] == "assistant_text"]
        self.assertEqual(len(text_events), 1)
        self.assertEqual(text_events[0]["text"], "working on it")
        self.assertNotIn("tool_preamble", text_events[0])

    def test_an_ended_turn_is_a_turn_result(self):
        state = initial_state()
        state, _events = reduce_transcript(state, user_str("do it"))
        state, events = reduce_transcript(state, assistant_end("all done"))
        results = [event for event in events if event["kind"] == "turn_result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "all done")
        self.assertEqual(state["activity"], "idle")

    def test_no_event_kind_is_named_progress_or_result(self):
        """Both names carried a judgement about what a line MEANS. Neither survives."""
        state = initial_state()
        state, first = reduce_transcript(state, user_str("do it"))
        state, second = reduce_transcript(state, assistant_tool("t1", text="mid"))
        state, third = reduce_transcript(state, assistant_end("end"))
        seen = set(kinds(first) + kinds(second) + kinds(third))
        self.assertNotIn("progress", seen)
        self.assertNotIn("result", seen)

    def test_an_unrecognized_record_type_costs_nothing(self):
        state = initial_state()
        same, events = reduce_transcript(state, {"type": "ai-title", "title": "x"})
        self.assertIs(same, state, "no revision bump for chatter")
        self.assertEqual(events, ())

    def test_an_untagged_enqueue_still_moves_the_balance(self):
        """A queued human line we did not send is a real item Claude must eat before ours."""
        state = initial_state()
        state, events = reduce_transcript(state, enqueue("someone else's line"))
        self.assertEqual(state["queue_balance"], 1)
        self.assertIn("queued_foreign", kinds(events))

    def test_a_tagged_enqueue_names_the_tag(self):
        state = initial_state()
        state, events = reduce_transcript(state, enqueue(f"run it {TAG_A}"))
        queued = [event for event in events if event["kind"] == "queued"]
        self.assertEqual(queued[0]["tag"], TAG_A)


if __name__ == "__main__":
    unittest.main()


class TypedLinesCarryTextTests(unittest.TestCase):
    """A string user record with none of our tags is a line the OPERATOR typed: the event
    carries its text so the voice can be shown it. A relayed frame (tagged) carries none --
    its text is already the voice's own words."""

    def test_an_untagged_user_line_carries_its_text(self):
        state, events = reduce_transcript(initial_state(), user_str("git status"))
        opened = [e for e in events if e["kind"] == "turn_opened"]
        self.assertEqual([e["text"] for e in opened], ["git status"])

    def test_a_tagged_frame_carries_no_text(self):
        state, events = reduce_transcript(initial_state(), user_str("跑 lint ⟨v#0123456789abcdef⟩"))
        opened = [e for e in events if e["kind"] == "turn_opened"]
        self.assertEqual([e["text"] for e in opened], [""])


class ThinkingOnlyEndTurnTests(unittest.TestCase):
    """Measured 2026-09-21T00:58:07: the host wrote `end_turn` twice, 8 ms apart -- first a record
    holding only a thinking block, then the record holding the 171-char answer. The first must
    not close the turn, or the answer is never a result."""

    def test_the_thinking_record_keeps_the_turn_open_and_the_text_record_closes_it(self):
        state = initial_state()
        state, _e = reduce_transcript(state, user_str("how many PRs"))
        thinking = {"type": "assistant", "uuid": "a-think", "sessionId": FIXTURE_SESSION,
                    "parentUuid": "u1",
                    "message": {"role": "assistant", "stop_reason": "end_turn",
                                "content": [{"type": "thinking", "thinking": ""}]}}
        state, events = reduce_transcript(state, thinking)
        self.assertEqual([e["kind"] for e in events if e["kind"] == "turn_result"], [])
        self.assertEqual(state["activity"], "working")
        state, events = reduce_transcript(state, assistant_end("3 open PRs", parent="a-think"))
        results = [e for e in events if e["kind"] == "turn_result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "3 open PRs")
        self.assertEqual(state["activity"], "idle")
