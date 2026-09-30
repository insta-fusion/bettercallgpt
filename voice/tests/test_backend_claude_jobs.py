"""Tests for `voice/backend/claude_jobs.py` — the mesh job tools as a backend.

The api is a scripted fake: each `status` call pops the next doc from a list, so the long-poll
sequence running → running-with-events → terminal is exercised exactly, with no clock. The fake
also records the `wait_sec` it was handed, which is how the "the long-poll IS the wait" rule is
checked rather than asserted in a comment.

Steer-state meanings are the mesh's own (the job mesh's own docs, §jobs); the mapping table test is the
guard against rounding `unconfirmed` up to success.
"""
from __future__ import annotations

import asyncio
import unittest

from voice.backend.base import OBS_OWNER_LOST, OBS_PROGRESS, OBS_RECEIPT, OBS_RESULT
from voice.backend.claude_jobs import ClaudeJobsBackend


class FakeJobsApi:
    """A scripted stand-in for the four mesh job callables."""

    def __init__(self, statuses, *, start=None, steer=None, stop=None, max_wait_sec=20):
        self._statuses = list(statuses)
        self._start = start if start is not None else {"job_id": "job-1"}
        self._steer = steer if steer is not None else {"state": "delivered"}
        self._stop = stop if stop is not None else {"stopped": True}
        self.max_wait_sec = max_wait_sec
        self.cwd = "/tmp/project"
        self.started: list[tuple[str, str]] = []
        self.steered: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.polls: list[tuple[str, float, int]] = []

    async def start(self, prompt, cwd):
        self.started.append((prompt, cwd))
        return self._start

    async def status(self, job_id, wait_sec, after_seq):
        self.polls.append((job_id, wait_sec, after_seq))
        if not self._statuses:
            raise AssertionError("observe() polled after the job went terminal")
        return self._statuses.pop(0)

    async def steer(self, job_id, text):
        self.steered.append((job_id, text))
        return self._steer

    async def stop(self, job_id):
        self.stopped.append(job_id)
        return self._stop


def running(seq, events=()):
    return {"state": "running", "seq": seq, "events": list(events)}


def finished(seq, result, state="finished", **extra):
    return {"state": state, "seq": seq, "events": [], "result": result, **extra}


async def drain(stream):
    """Observations up to and including the first result (the stream then waits for the next
    job), or to the end of a stream that ends (a lost owner)."""
    seen = []
    async for obs in stream:
        seen.append(obs)
        if obs.kind in (OBS_RESULT, OBS_OWNER_LOST):
            break
    await stream.aclose()
    return seen


class ObserveTests(unittest.TestCase):
    """The long-poll drives everything; a terminal state yields the result and goes idle."""

    def test_receipt_then_progress_then_result(self):
        async def run():
            api = FakeJobsApi([
                running(1),
                running(4, [{"kind": "text", "text": "reading the file"},
                            {"kind": "tool", "text": "Edit voice/backend/process.py"}]),
                finished(6, "the change is in place"),
            ])
            backend = ClaudeJobsBackend(api)
            receipt = await backend.send("do the thing", tag="v1", priority="next",
                                         transcript="do the thing", interpretation="task")
            self.assertEqual(receipt.outcome, "posted")
            self.assertEqual(api.started, [("do the thing", "/tmp/project")])

            seen = await drain(backend.observe())
            kinds = [obs.kind for obs in seen]
            self.assertEqual(kinds, [OBS_RECEIPT, OBS_PROGRESS, OBS_PROGRESS, OBS_RESULT])

            # The receipt binds the starting request's tag to the job.
            self.assertEqual((seen[0].tag, seen[0].turn_id), ("v1", "job-1"))
            self.assertEqual(seen[1].text, "reading the file")
            self.assertEqual(seen[3].text, "the change is in place")
            self.assertEqual(seen[3].payload["job_state"], "finished")
            self.assertFalse(seen[3].payload["failed"])
            self.assertTrue(all(obs.turn_id == "job-1" for obs in seen))

        asyncio.run(run())

    def test_the_long_poll_is_the_wait_and_the_seq_advances(self):
        """Every poll asks for the api's OWN maximum and resumes past the last seq."""
        async def run():
            api = FakeJobsApi([running(3), running(7), finished(9, "done")], max_wait_sec=20)
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            await drain(backend.observe())

            self.assertEqual([wait for _, wait, _ in api.polls], [20, 20, 20])
            self.assertEqual([after for _, _, after in api.polls], [0, 3, 7])

        asyncio.run(run())

    def test_a_failed_job_results_with_failed_and_the_error(self):
        async def run():
            api = FakeJobsApi([finished(2, "", state="failed", error="stdin stalled")])
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            seen = await drain(backend.observe())

            result = seen[-1]
            self.assertEqual(result.kind, OBS_RESULT)
            self.assertTrue(result.payload["failed"])
            self.assertEqual(result.payload["job_state"], "failed")
            self.assertEqual(result.payload["error"], "stdin stalled")

        asyncio.run(run())

    def test_a_cancelled_job_is_terminal_too(self):
        async def run():
            api = FakeJobsApi([running(1), finished(2, "", state="cancelled")])
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            seen = await drain(backend.observe())
            self.assertEqual(seen[-1].payload["job_state"], "cancelled")
            self.assertEqual(seen[-1].kind, OBS_RESULT)

        asyncio.run(run())

    def test_a_steer_event_arrives_as_a_receipt(self):
        async def run():
            api = FakeJobsApi([
                running(2, [{"kind": "steer", "state": "received", "text": "also append X",
                             "tag": "v2"}]),
                finished(3, "done"),
            ])
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            seen = await drain(backend.observe())

            receipts = [obs for obs in seen if obs.kind == OBS_RECEIPT]
            self.assertEqual([r.tag for r in receipts], ["v1", "v2"],
                             "the starting request's receipt, then the steer's")
            receipts = receipts[1:]
            self.assertEqual(receipts[0].payload["steer_state"], "received")

        asyncio.run(run())


class SendTests(unittest.TestCase):
    """Idle → start; running → steer, with the steer state read honestly."""

    def test_send_while_idle_starts_a_job(self):
        async def run():
            api = FakeJobsApi([finished(1, "ok")])
            backend = ClaudeJobsBackend(api)
            receipt = await backend.send("first task", tag="v1", priority="next",
                                         transcript="first task", interpretation="task")
            self.assertEqual(receipt.outcome, "posted")
            self.assertEqual(api.started, [("first task", "/tmp/project")])
            self.assertEqual(api.steered, [])
            self.assertEqual((await backend.state()).turn_id, "job-1")

        asyncio.run(run())

    def test_send_now_while_running_steers_the_same_job(self):
        async def run():
            api = FakeJobsApi([finished(1, "ok")])
            backend = ClaudeJobsBackend(api)
            await backend.send("first", tag="v1", priority="next", transcript="first",
                               interpretation="task")
            receipt = await backend.send("also append X", tag="v2", priority="now",
                                         transcript="also append X", interpretation="correction")

            self.assertEqual(receipt.outcome, "posted")
            self.assertEqual(api.steered, [("job-1", "also append X")])
            self.assertEqual(len(api.started), 1)   # the same run, not a second one

        asyncio.run(run())

    def test_a_start_that_returns_no_job_id_is_refused(self):
        async def run():
            api = FakeJobsApi([], start={"sentinel": "[drive_claude_start BLOCKED] cwd jail"})
            backend = ClaudeJobsBackend(api)
            receipt = await backend.send("go", tag="v1", priority="next", transcript="go",
                                         interpretation="task")
            self.assertEqual(receipt.outcome, "refused")
            self.assertIn("BLOCKED", receipt.reason)

        asyncio.run(run())

    def test_steer_state_maps_to_the_receipt_outcome(self):
        """The mesh's steer vocabulary, mapped without rounding anything up."""
        cases = [
            ("delivered", "posted"),
            ("received", "posted"),
            ("queued_next_turn", "posted"),
            ("unconfirmed", "uncertain"),
            ("rejected", "refused"),
        ]

        async def run():
            for state, expected in cases:
                api = FakeJobsApi([finished(1, "ok")], steer={"state": state})
                backend = ClaudeJobsBackend(api)
                await backend.send("first", tag="v1", priority="next", transcript="first",
                                   interpretation="task")
                receipt = await backend.send("correction", tag="v2", priority="now",
                                             transcript="correction", interpretation="correction")
                self.assertEqual(receipt.outcome, expected, f"steer state {state}")
                self.assertEqual(receipt.tag, "v2")

        asyncio.run(run())

    def test_unconfirmed_is_uncertain_and_says_never_resubmit(self):
        """`unconfirmed` means the bytes reached the pipe and consumption is unknown."""
        async def run():
            api = FakeJobsApi([finished(1, "ok")], steer={"state": "unconfirmed"})
            backend = ClaudeJobsBackend(api)
            await backend.send("first", tag="v1", priority="next", transcript="first",
                               interpretation="task")
            receipt = await backend.send("correction", tag="v2", priority="now",
                                         transcript="correction", interpretation="correction")

            self.assertEqual(receipt.outcome, "uncertain")
            self.assertIn("resubmit", receipt.reason)

        asyncio.run(run())

    def test_queued_next_turn_is_posted_and_flagged(self):
        async def run():
            api = FakeJobsApi([finished(1, "ok")], steer={"state": "queued_next_turn"})
            backend = ClaudeJobsBackend(api)
            await backend.send("first", tag="v1", priority="next", transcript="first",
                               interpretation="task")
            receipt = await backend.send("correction", tag="v2", priority="now",
                                         transcript="correction", interpretation="correction")

            self.assertEqual(receipt.outcome, "posted")
            self.assertIn("next turn", receipt.reason)

        asyncio.run(run())

    def test_an_unknown_steer_state_is_uncertain_never_posted(self):
        async def run():
            api = FakeJobsApi([finished(1, "ok")], steer={"state": "who_knows"})
            backend = ClaudeJobsBackend(api)
            await backend.send("first", tag="v1", priority="next", transcript="first",
                               interpretation="task")
            receipt = await backend.send("correction", tag="v2", priority="now",
                                         transcript="correction", interpretation="correction")
            self.assertEqual(receipt.outcome, "uncertain")

        asyncio.run(run())


class LongPollBoundTests(unittest.TestCase):
    """The long-poll bound is injected, never written as a literal in this module."""

    def test_mesh_api_requires_the_bound_to_be_injected(self):
        """`max_wait_sec` has no default: a bound here would be this file inventing a timer."""
        from voice.backend import claude_jobs

        with self.assertRaises(TypeError) as caught:
            claude_jobs.mesh_api(cwd=".")
        self.assertIn("max_wait_sec", str(caught.exception))

    def test_the_backend_polls_with_whatever_bound_the_api_declares(self):
        """Whatever the api declares is what the poll asks for — no substitute, no clamp."""
        async def run():
            for declared in (5, 20):
                api = FakeJobsApi([finished(1, "ok")], max_wait_sec=declared)
                backend = ClaudeJobsBackend(api)
                await backend.send("go", tag="v1", priority="next", transcript="go",
                                   interpretation="task")
                await drain(backend.observe())
                self.assertEqual([wait for _, wait, _ in api.polls], [declared])

        asyncio.run(run())

    def test_an_api_declaring_no_bound_polls_without_one(self):
        """A missing declaration must not silently become a number this module chose."""
        async def run():
            api = FakeJobsApi([finished(1, "ok")])
            del api.max_wait_sec
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            await drain(backend.observe())
            self.assertIsNone(api.polls[0][1])

        asyncio.run(run())


class CancelAndRefusalTests(unittest.TestCase):

    def test_cancel_stops_the_job(self):
        async def run():
            api = FakeJobsApi([finished(1, "ok")])
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            receipt = await backend.cancel("job-1")
            self.assertEqual(receipt.outcome, "applied")
            self.assertEqual(api.stopped, ["job-1"])

        asyncio.run(run())

    def test_an_unconfirmed_stop_is_uncertain_not_applied(self):
        """`stopped: false` carries a cleanup_note — it is not a confirmed stop."""
        async def run():
            api = FakeJobsApi([finished(1, "ok")],
                              stop={"stopped": False, "cleanup_note": "cleanup timed out"})
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            receipt = await backend.cancel("job-1")
            self.assertEqual(receipt.outcome, "uncertain")
            self.assertIn("cleanup", receipt.reason)

        asyncio.run(run())

    def test_a_rejected_stop_is_refused(self):
        async def run():
            api = FakeJobsApi([finished(1, "ok")],
                              stop={"state": "rejected", "reason": "already finished"})
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            receipt = await backend.cancel("job-1")
            self.assertEqual(receipt.outcome, "refused")
            self.assertIn("already finished", receipt.reason)

        asyncio.run(run())

    def test_cancel_with_no_job_is_refused(self):
        async def run():
            receipt = await ClaudeJobsBackend(FakeJobsApi([])).cancel("job-1")
            self.assertEqual(receipt.outcome, "refused")
            self.assertIn("no job", receipt.reason)

        asyncio.run(run())

    def test_a_job_exposes_no_dialog(self):
        async def run():
            receipt = await ClaudeJobsBackend(FakeJobsApi([])).answer("occ-1", "yes")
            self.assertEqual(receipt.outcome, "refused")
            self.assertIn("no dialog", receipt.reason)

        asyncio.run(run())

    def test_a_finished_job_goes_idle_and_the_next_request_starts_a_new_job(self):
        async def run():
            api = FakeJobsApi([finished(1, "ok")])
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            await drain(backend.observe())

            self.assertEqual((await backend.state()).activity, "idle")
            self.assertEqual((await backend.cancel("job-1")).outcome, "refused")
            again = await backend.send("more", tag="v2", priority="now", transcript="more",
                                       interpretation="next task")
            self.assertEqual(again.outcome, "posted")
            self.assertEqual(len(api.started), 2, "a new job, not a steer into the finished one")

        asyncio.run(run())

    def test_a_status_sentinel_ends_the_stream_as_owner_loss(self):
        """A sentinel is a FAILED leg, never an answer — the stream must not spin on it."""
        async def run():
            api = FakeJobsApi(["[drive_job_status ERROR] unknown job_id"])
            backend = ClaudeJobsBackend(api)
            await backend.send("go", tag="v1", priority="next", transcript="go",
                               interpretation="task")
            seen = await drain(backend.observe())
            self.assertEqual([obs.kind for obs in seen], [OBS_RECEIPT, OBS_OWNER_LOST])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()


class ObserveBeforeTheFirstJob(unittest.IsolatedAsyncioTestCase):
    """The loop starts observing before any request exists. The observer must wait for the
    first job rather than end — ending left the job the operator then asked for unobserved."""

    async def test_observe_started_before_send_sees_the_job_result(self):
        class Api:
            max_wait_sec = 1

            async def start(self, prompt, cwd):
                return {"job_id": "job-1"}

            async def status(self, job_id, wait_sec, after_seq):
                return {"state": "finished", "seq": 1, "events": [], "result": "done"}

        backend = ClaudeJobsBackend(Api())
        stream = backend.observe()
        first = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0)
        self.assertFalse(first.done(), "the observer waits for a job instead of ending")
        await backend.send("run it", tag="req-1", priority="next",
                           transcript="run it", interpretation="run it")
        receipt = await asyncio.wait_for(first, 1)
        self.assertEqual((receipt.kind, receipt.tag), ("receipt", "req-1"))
        obs = await asyncio.wait_for(stream.__anext__(), 1)
        self.assertEqual((obs.kind, obs.text), ("result", "done"))
        await stream.aclose()


class SteersAreBoundToTheirRequests(unittest.IsolatedAsyncioTestCase):
    """Closing review 2: a correction steered into a running job is reported by the mesh in
    the status document's `steers` list, by steer_id. It must become a tagged receipt before
    the result, so the job's result answers the correction's request too."""

    async def test_a_received_steer_is_a_tagged_receipt_before_the_result(self):
        class Api:
            max_wait_sec = 1

            def __init__(self):
                self.polls = 0

            async def start(self, prompt, cwd):
                return {"job_id": "job-1"}

            async def steer(self, job_id, text):
                return {"steer_id": "s-1", "state": "delivered", "job_state": "running"}

            async def status(self, job_id, wait_sec, after_seq):
                self.polls += 1
                if self.polls == 1:
                    return {"state": "running", "seq": 1, "events": [], "steers": []}
                return {"state": "finished", "seq": 2, "events": [], "result": "done",
                        "steers": [{"steer_id": "s-1", "state": "received", "text": "also X",
                                    "turn": 1}]}

        backend = ClaudeJobsBackend(Api())
        await backend.send("do A", tag="req-1", priority="next", transcript="do A",
                           interpretation="do A")
        stream = backend.observe()
        first = await stream.__anext__()
        self.assertEqual((first.kind, first.tag), (OBS_RECEIPT, "req-1"))
        steered = await backend.send("also X", tag="req-2", priority="now",
                                     transcript="also X", interpretation="also X")
        self.assertEqual(steered.outcome, "posted")
        rest = []
        async for obs in stream:
            rest.append(obs)
            if obs.kind == OBS_RESULT:
                break
        await stream.aclose()
        self.assertEqual([(o.kind, o.tag) for o in rest],
                         [(OBS_RECEIPT, "req-2"), (OBS_RESULT, None)])
        self.assertEqual(rest[0].turn_id, "job-1")

    async def test_a_terminal_status_during_an_in_flight_steer_still_answers_it(self):
        """Short review: the job finishes while `steer` has not returned. The steer's tag
        must be registered before the result, and bound by the terminal status."""
        release = asyncio.Event()

        class Api:
            max_wait_sec = 1

            async def start(self, prompt, cwd):
                return {"job_id": "job-1"}

            async def steer(self, job_id, text):
                await release.wait()
                return {"steer_id": "s-1", "state": "delivered", "job_state": "finished"}

            async def status(self, job_id, wait_sec, after_seq):
                return {"state": "finished", "seq": 2, "events": [], "result": "done",
                        "steers": [{"steer_id": "s-1", "state": "delivered", "text": "also X",
                                    "turn": 1}]}

        backend = ClaudeJobsBackend(Api())
        await backend.send("do A", tag="req-1", priority="next", transcript="do A",
                           interpretation="do A")
        stream = backend.observe()
        self.assertEqual((await stream.__anext__()).tag, "req-1")
        steering = asyncio.ensure_future(backend.send(
            "also X", tag="req-2", priority="now", transcript="also X", interpretation="also X"))
        await asyncio.sleep(0)
        nxt = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0)
        self.assertFalse(nxt.done(), "the result waits for the in-flight steer")
        release.set()
        await steering
        receipt = await asyncio.wait_for(nxt, 1)
        self.assertEqual((receipt.kind, receipt.tag), (OBS_RECEIPT, "req-2"))
        result = await asyncio.wait_for(stream.__anext__(), 1)
        self.assertEqual(result.kind, OBS_RESULT)
        await stream.aclose()
