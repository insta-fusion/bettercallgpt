"""Capture and playback, with the audio EPOCH that makes an interruption exact.

Three things live here:

**`Capture`** — 24 kHz PCM16 mono off the microphone into an async callback. The device
layer is injected (`open_input`), so tests hand it a fake and no test ever opens a real
microphone.

**`PlaybackSink`** — the `AudioSink` of `live/base.py`, and the home of the epoch rule
from DESIGN.md §Audio epoch: **the epoch is keyed by response id**. `cancel(rid)` drops
everything still queued for that response, returns the frames that actually rendered, and
POISONS the id — every later `play()` for it is dropped, however late the delta arrives.
That is the replacement for `LateAudioFromACutResponse`: a delta carrying a cancelled
response id never reaches the speaker, and it holds without any timer, because lateness
is not what identifies the audio — the id is.

`rendered_ms` counts only frames the writer HANDED TO THE STREAM. Never queued bytes:
`conversation.item.truncate` tells the server what the operator heard, and the server
rejects a cut longer than the item's audio, so an over-count is both a lie and an error
(the production "already shorter than" storm, ported from `voice_live_transport`).

**`AudioLock`** — the cross-process one-mic-one-speaker lock, ported from `voice_audio`.

`sounddevice` is imported lazily inside the device factories: PortAudio initializes at
import, and that must not happen in a process that only wanted the types.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

SAMPLE_RATE = 24_000
CHANNELS = 1
BYTES_PER_FRAME = 2                 # pcm16 mono
BLOCK_FRAMES = 480                  # 20 ms at 24 kHz — a block size, not a duration knob

_HOLDER_OFF = 64                    # diagnostic record offset; byte 0 is the msvcrt lock
_HOLDER_BLANK = b" " * 54


def _resolve(fut: Any) -> None:
    if not fut.done():
        fut.set_result(None)


def frames_of(pcm: bytes) -> int:
    return len(pcm) // BYTES_PER_FRAME


def frames_to_ms(frames: int) -> int:
    return (frames * 1000) // SAMPLE_RATE


# ---------------------------------------------------------------- device seam

class OutputStream(Protocol):
    """The slice of a device stream the sink uses. A fake implements exactly this.

    `abort` discards what the DEVICE still holds. Dropping our queue is not enough: the
    ring buffer already handed to PortAudio keeps sounding for as long as it holds, so a
    cut that only clears software state still lets the cancelled response finish speaking.

    **`abort` leaves the stream INACTIVE**, which is why `start` is part of this protocol:
    a sink that aborted and walked away would write the NEXT reply into a stopped stream,
    and PortAudio raises — killing the writer thread for the rest of the call. So every
    abort is followed by a restart before any later frame is accepted.
    """

    def write(self, pcm: bytes) -> None: ...
    def abort(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


def open_output_stream() -> OutputStream:
    """The real speaker. Lazy import: PortAudio initializes here, not at module import."""
    import sounddevice as sd

    stream = sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16")
    stream.start()
    return stream  # sounddevice carries write/abort/start/stop/close


def open_input_stream(callback: Callable[[bytes], None]) -> Any:
    """The real microphone: 24 kHz PCM16 mono blocks into `callback` on the device thread."""
    import sounddevice as sd

    def cb(indata, frames, time_info, status):     # PortAudio thread
        callback(bytes(indata))

    stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
                               blocksize=BLOCK_FRAMES, callback=cb)
    try:
        stream.start()
    except Exception:
        _close_quietly(stream)
        raise
    return stream


def _abort_quietly(stream: Any) -> None:
    """Discard what the device holds and unblock a writer stuck in `write`."""
    try:
        stream.abort()
    except Exception:
        pass


def _abandon_quietly(stream: Any) -> None:
    """Teardown of a device that stopped answering: abort (unblocks a stuck write), then
    close. Runs on a thread nobody joins."""
    _abort_quietly(stream)
    _close_quietly(stream)


def _close_quietly(stream: Any) -> None:
    """ALWAYS attempt close, even when stop() raised — a half-open PortAudio stream
    leaks the device for the whole machine (ported from voice_audio._close_stream)."""
    try:
        stream.stop()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


# ---------------------------------------------------------------- capture

class Capture:
    """Microphone -> an async callback, one 20 ms PCM16 block at a time.

    The device callback runs on PortAudio's thread, so blocks are handed to the event
    loop with `call_soon_threadsafe`. `on_audio` is awaited on the loop; a slow consumer
    backs up in the queue rather than blocking the device thread.
    """

    def __init__(self, loop: Any, on_audio: Callable[[bytes], Any], *,
                 open_input: Callable[[Callable[[bytes], None]], Any] = open_input_stream) -> None:
        self._loop = loop
        self._on_audio = on_audio
        self._open_input = open_input
        self._stream: Any = None

    def start(self) -> None:
        self._stream = self._open_input(self._deliver)

    def _deliver(self, pcm: bytes) -> None:
        self._loop.call_soon_threadsafe(self._dispatch, pcm)

    def _dispatch(self, pcm: bytes) -> None:
        result = self._on_audio(pcm)
        if hasattr(result, "__await__"):
            self._loop.create_task(result)

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            _close_quietly(stream)


# ---------------------------------------------------------------- playback

class PlaybackSink:
    """`AudioSink` with a response-id-keyed epoch.

    The writer thread owns `_rendered`: it is incremented only AFTER a successful write,
    so it can never over-count what the operator heard. `cancel()` is called from the
    event loop; `_lock` guards the queue and the cancelled set, and the rendered counters
    are read under it too so a cut sees a consistent figure.
    """

    dead: bool = False   # the writer thread hit the device and left; declared so callers can read it

    def __init__(self, *, open_output: Callable[[], OutputStream] = open_output_stream) -> None:
        self._open_output = open_output
        self._stream: OutputStream | None = None
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        # THE DEVICE BARRIER, separate from `_lock`. `_lock` guards bookkeeping and is taken
        # briefly; this one is held across a blocking `write` and across an `abort`, so the
        # two can never interleave. Without it a cancel of response A could abort the stream
        # while the writer was already mid-write on response B — B's audio cut by A's cut.
        self._device = threading.Lock()
        self._cancelled: set[str] = set()                  # poisoned response ids
        self._rendered: dict[tuple[str, str], int] = {}    # (rid, item) -> frames written
        self._thread: threading.Thread | None = None
        self.dead = False
        # DELIVERY EVIDENCE (DESIGN.md §Acceptance C2). `_done` holds the response ids the WIRE has
        # reported finished; `_pending` counts frames accepted but not yet handed to the
        # device, per response. A response reached its end only when the provider says it
        # is over AND nothing of it is still waiting — two facts from opposite ends of the
        # path, neither sufficient alone.
        self._done: set[str] = set()
        self._pending: dict[str, int] = {}
        # Frames DISCARDED because the device was gone. Kept apart from `_rendered` on
        # purpose: a dropped frame is the opposite of evidence, and merging the two is how
        # an unheard consent challenge came to satisfy delivery.
        self._dropped: dict[str, int] = {}
        # Response ids whose epoch advanced at or after their FIRST rendered frame. Kept
        # apart from `_cancelled` because a cancel before anything rendered interrupted
        # nothing the operator heard, and C2 asks whether the operator was interrupted.
        self._epoch_broken: set[str] = set()
        # Entries queued and not yet taken (or dropped) by the writer, and who is waiting
        # for that count to reach zero. Resolved from the writer thread through each
        # waiter's own loop — the daemon awaits it under a lifecycle bound, so a device that
        # never takes the frames cannot hold the ending, and no thread is stranded.
        self._queued = 0
        self._drain_waiters: list[tuple[Any, Any]] = []
        # DEVICE RESIDENCY: every response written to the device since its last abort. The
        # device does not report what it has played, so any of them may still be sounding
        # from its buffer after the queue is empty — an older one included, even when newer
        # audio was written after it. Cleared when an abort discards the buffer.
        self._resident: set[str] = set()
        self._closing = False

    # -------------------------------------------------- lifecycle

    def start(self) -> None:
        self._stream = self._open_output()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self, *, bound_s: float | None = None) -> None:
        """Stop the writer and release the device. `bound_s` is the caller's lifecycle bound
        on the join: a writer stuck in a device write that never returns is abandoned —
        marked dead, its stream aborted to unblock it — instead of holding the caller."""
        with self._lock:
            # From here `play` drops instead of queueing: the sentinel below is the last
            # thing the writer will ever take. `cancel` and `close` share the daemon's one
            # loop thread and never interleave, so nothing else reads this.
            self._closing = True
        self._q.put(None)
        thread, self._thread = self._thread, None
        abandoned = False
        if thread is not None:
            # The stream stays in place through the join: the writer is still playing the
            # tail (the falling tone) and a missing device would make it drop those frames.
            thread.join(bound_s)
            abandoned = thread.is_alive()
        stream, self._stream = self._stream, None
        if abandoned:
            # The device is not answering; nothing queued will ever be taken. Settle the
            # count and every waiter now, and hand the device's own teardown to a thread
            # nobody waits for — a synchronous stop/close on a wedged device would hold the
            # caller as surely as the join did.
            with self._lock:
                self.dead = True
                self._queued = 0
                self._taken_locked()
            if stream is not None:
                threading.Thread(target=_abandon_quietly, args=(stream,), daemon=True).start()
            return
        if stream is not None:
            # The device answered every write, so stop/close answer too.
            _close_quietly(stream)

    def drain(self) -> None:
        """Block until the writer has taken — or, dead, dropped — everything queued. Tests use
        it instead of waiting on a clock; the daemon awaits `drained` instead, which never
        blocks the loop. Safe after the writer died: it empties the queue on its way out and
        `play` enqueues nothing once `dead`."""
        self._q.join()

    def drained(self, loop: Any) -> Any:
        """A future on `loop` that resolves once everything queued now has been taken by the
        device or dropped. Already resolved when nothing is queued or the writer is dead."""
        fut = loop.create_future()
        with self._lock:
            if self._queued == 0 or self.dead:
                fut.set_result(None)
            else:
                self._drain_waiters.append((loop, fut))
        return fut

    def _taken_locked(self) -> None:
        """One queue entry handed to the device or dropped. Caller holds `_lock`. Never
        below zero: a writer abandoned at close may still finish a write later."""
        if self._queued > 0:
            self._queued -= 1
        if self._queued > 0:
            return
        waiters, self._drain_waiters = self._drain_waiters, []
        for loop, fut in waiters:
            try:
                loop.call_soon_threadsafe(_resolve, fut)
            except RuntimeError:
                # The waiter's loop is closed; nobody is waiting. Not a device fault.
                pass

    # -------------------------------------------------- AudioSink

    def play(self, response_id: str, item_id: str, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            if response_id in self._cancelled:
                # THE EPOCH. This response was cut; its audio is not the operator's
                # present, however early it was generated or how late it arrived.
                return
            if self.dead or self._closing:
                # No writer will take it — dead, or the sentinel is already queued behind
                # everything the writer will ever take: dropped, on the record, and never
                # queued — a queue nobody consumes would hang `drain` and `drained`.
                self._drop_locked(response_id, frames_of(pcm))
                return
            self._pending[response_id] = self._pending.get(response_id, 0) + frames_of(pcm)
            self._queued += 1
            self._q.put((response_id, item_id, pcm))

    def cancel(self, response_id: str) -> int:
        """Drop this response's queued audio, poison its id, return frames rendered."""
        return self.cancel_many((response_id,))

    def cancel_many(self, response_ids: Any) -> int:
        """`cancel` for several ids at once: every id is poisoned and dequeued under ONE lock,
        and the device is aborted AT MOST ONCE under one barrier. Cancelling ids one by one
        let the writer advance other audio between the aborts (a GPT-Live flush cancels every
        segment the operator talked over). Returns the frames rendered across all of them."""
        ids = set(response_ids)
        if not ids:
            return 0
        with self._lock:
            self._cancelled.update(ids)
            # The epoch verdict and the returned figure are both settled below, under the
            # device lock, once any in-flight write has landed.
            kept: list[Any] = []
            while True:
                try:
                    entry = self._q.get_nowait()
                except queue.Empty:
                    break
                self._q.task_done()
                if entry is None or entry[0] not in ids:
                    kept.append(entry)
                else:
                    self._taken_locked()
            for entry in kept:
                self._q.put(entry)
            # Nothing of these responses is waiting for the device any more.
            for rid in ids:
                self._pending.pop(rid, None)
        # WHAT THE DEVICE STILL HOLDS. Frames already written sit in PortAudio's ring and
        # keep sounding unless the stream is aborted; `abort` discards them rather than
        # draining. Only when something of THESE responses actually rendered — aborting
        # otherwise would cut the tail of whatever is legitimately playing.
        #
        # Under the DEVICE lock, not `_lock`: the writer holds this same lock across its
        # write, so the abort waits for an in-flight chunk instead of cutting it. That is
        # what stops A's cancel from aborting B's audio.
        with self._device:
            # RE-READ under the device lock. The figure taken above was a snapshot from
            # before the barrier: a chunk of this response could have been mid-write, and
            # crediting zero would skip the abort for audio the operator just heard.
            with self._lock:
                rendered = 0
                for rid in ids:
                    frames = sum(f for (r, _item), f in self._rendered.items() if r == rid)
                    if frames:
                        self._epoch_broken.add(rid)
                    rendered += frames
            if rendered:
                self._abort_and_restart()
        return rendered

    def retire(self, response_ids: Any) -> None:
        """A previous provider session's audio, cut because the operator talked on the new
        one. Its ids are poisoned and their queued frames dropped, and when any of them may
        still be in the device buffer (RESIDENT: written since the last abort) the device is
        aborted — whatever was written after it, since the device cannot drop one response
        and keep another. Everything that abort cut is marked interrupted: the new session's
        audio in the same buffer was talked over too, and says so (`epoch_unchanged`)."""
        ids = set(response_ids)
        if not ids:
            return
        with self._lock:
            self._cancelled.update(ids)
            kept: list[Any] = []
            while True:
                try:
                    entry = self._q.get_nowait()
                except queue.Empty:
                    break
                self._q.task_done()
                if entry is None or entry[0] not in ids:
                    kept.append(entry)
                else:
                    self._taken_locked()
            for entry in kept:
                self._q.put(entry)
            for rid in ids:
                self._pending.pop(rid, None)
        with self._device:
            with self._lock:
                cut = set(self._resident) if self._resident & ids else set()
                for rid in cut:
                    self._epoch_broken.add(rid)
            if cut:
                self._abort_and_restart()

    def _abort_and_restart(self) -> None:
        """Discard the device's buffer and bring the stream back up. Caller holds `_device`.

        The restart is the half that was missing: `abort()` leaves a PortAudio stream
        stopped, so the next reply's first `write` raised, the writer thread died, and the
        operator's voice went silent for the rest of the call after one barge-in.
        """
        stream = self._stream
        if stream is None:
            return
        with self._lock:
            self._resident.clear()               # the buffer they sat in is being discarded
        try:
            stream.abort()
        except Exception:
            # A device that cannot abort is one whose tail we cannot stop. Not a reason to
            # fail the cut: the epoch still holds for everything not yet written.
            return
        try:
            stream.start()
        except Exception:
            # The stream cannot be revived. Replace it rather than leave a dead one in
            # place, so the next response is not written into a stopped device.
            _close_quietly(stream)
            try:
                self._stream = self._open_output()
            except Exception:
                self._stream = None
                self.dead = True

    def _drop_locked(self, response_id: str, frames: int) -> None:
        """Record frames that never reached the device. Caller holds `_lock`."""
        if frames > 0:
            self._dropped[response_id] = self._dropped.get(response_id, 0) + frames
        self._pending.pop(response_id, None)

    def _settle_pending_locked(self, response_id: str, frames: int) -> None:
        left = self._pending.get(response_id, 0) - frames
        if left > 0:
            self._pending[response_id] = left
        else:
            self._pending.pop(response_id, None)

    def queued_ids(self) -> set[str]:
        """The response ids whose audio may still SOUND: frames waiting for the device, and
        every response resident in its buffer (written since the last abort). A relay reads
        it to know which audio a previous provider session left behind."""
        with self._lock:
            ids = {rid for rid, frames in self._pending.items() if frames > 0}
            return ids | self._resident

    @property
    def device_dead(self) -> bool:
        """The device is gone: nothing more will be heard on this sink.

        The loop turns this into a `thinking` note for the operator, and the broker treats
        it as NO DELIVERY — an effect may not be armed against audio that cannot play.
        """
        return self.dead

    def dropped_frames(self, response_id: str) -> int:
        """Frames of this response discarded because there was no device to write them."""
        with self._lock:
            return self._dropped.get(response_id, 0)

    def rendered_ms(self, response_id: str, item_id: str) -> int | None:
        with self._lock:
            frames = self._rendered.get((response_id, item_id))
        if not frames:
            return None
        return frames_to_ms(frames)

    def note_response_done(self, response_id: str) -> None:
        """The wire adapter's hook: this response is over as far as the PROVIDER knows.

        Half of `reached_end`. It says no further audio is coming, not that what already
        came has played — the queue answers that, and only both together mean the operator
        heard the whole thing.
        """
        with self._lock:
            self._done.add(response_id)

    def reached_end(self, response_id: str) -> bool:
        with self._lock:
            if self.dead:
                # A dead device proves nothing about what was heard, including about audio
                # written before it died — the tail was in a buffer that never drained.
                return False
            if response_id not in self._done:
                return False
            if self._pending.get(response_id, 0) > 0:
                return False
            if self._dropped.get(response_id, 0) > 0:
                # Part of this response never reached the speaker, so it did not reach
                # its end however much of it did.
                return False
            # A response that ended without ever rendering a frame delivered nothing;
            # treating that as "reached its end" would credit silence as spoken.
            return any(rid == response_id for rid, _item in self._rendered)

    def epoch_unchanged(self, response_id: str) -> bool:
        with self._lock:
            if self.dead or self._dropped.get(response_id, 0) > 0:
                # Not "uninterrupted" — unknown. The honest answer to "did the operator
                # hear this without interruption" is no when the speaker stopped working.
                return False
            return response_id not in self._epoch_broken

    # -------------------------------------------------- writer thread

    def _run(self) -> None:
        while True:
            entry = self._q.get()
            if entry is None:
                self._q.task_done()
                return
            response_id, item_id, pcm = entry
            frames = frames_of(pcm)
            # THE BARRIER. Check, write and ACCOUNT inside one hold of the device lock, so
            # a `cancel()` can neither slip between the check and the write nor abort the
            # device mid-write. Accounting belongs inside too: recording the frames after
            # releasing left a window where a cut saw zero rendered for a chunk the
            # operator had already heard, and then skipped the abort that would have
            # silenced its tail.
            try:
                with self._device:
                    with self._lock:
                        cut = response_id in self._cancelled
                        if cut:
                            self._pending.pop(response_id, None)
                            self._taken_locked()
                    if cut:
                        self._q.task_done()
                        continue
                    if self._stream is None:
                        # NO DEVICE, SO NOTHING WAS HEARD. Counting these as rendered is
                        # what let `reached_end` report true with zero device writes — and
                        # a consent challenge nobody heard would have satisfied C2's third
                        # delivery evidence. A dropped frame is recorded as dropped.
                        with self._lock:
                            self._drop_locked(response_id, frames)
                            self._taken_locked()
                        self._q.task_done()
                        continue
                    self._stream.write(pcm)
                    with self._lock:
                        key = (response_id, item_id)
                        self._rendered[key] = self._rendered.get(key, 0) + frames
                        self._resident.add(response_id)
                        self._settle_pending_locked(response_id, frames)
                        self._taken_locked()
            except Exception:
                self.dead = True
                # This frame never reached the speaker, and neither will anything queued
                # behind it. All of it is DROPPED, not rendered: `reached_end` must report
                # that delivery cannot be proven rather than hang or, worse, claim success.
                with self._lock:
                    self._drop_locked(response_id, frames)
                    for rid, waiting in list(self._pending.items()):
                        self._drop_locked(rid, waiting)
                    self._taken_locked()
                    # Everything behind it is dropped too — and accounted for, so `drain`
                    # and `drained` both return instead of waiting on a dead writer.
                    while True:
                        try:
                            entry = self._q.get_nowait()
                        except queue.Empty:
                            break
                        self._q.task_done()
                        if entry is not None:          # the close sentinel was never counted
                            self._taken_locked()
                self._q.task_done()
                return
            self._q.task_done()


# ---------------------------------------------------------------- the machine lock

# b3: lifecycle-ports begin  (AudioLock: acquisition machinery; cadence is INJECTED)
class AudioLock:
    """Cross-process audio-device lock: one mic + speaker per machine.

    Ported from `voice_audio.AudioLock`. `acquire(timeout=0)` is skip-if-busy and takes no
    cadence at all. A positive timeout has to retry, because `flock`/`msvcrt` give no
    readiness event a selector can wait on — a file lock is not a descriptor that becomes
    readable when the holder leaves. So the cadence is an INJECTED PORT: the daemon's
    lifecycle block owns it, and this class declares no default. A module-level `POLL_S`
    was this file choosing a duration, which is exactly what DESIGN.md §Timers forbids.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None, *,
                 poll_s: float | None = None) -> None:
        self.path = Path(path or os.environ.get("VOICE_LOCK_FILE")
                         or Path.home() / ".claude" / "voice-butler.lock")
        # None is legal and means "never wait": `acquire(timeout=0)` needs no cadence, so a
        # caller that only ever skips-if-busy supplies nothing. A positive timeout without
        # a cadence is a refusal, not an invented interval.
        self._poll_s = None if poll_s is None else float(poll_s)
        self._fh: Any = None

    def _try_lock(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_NOFOLLOW: never follow a planted symlink. 0600: never world-readable via umask.
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fh = os.fdopen(os.open(self.path, flags, 0o600), "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        # Diagnostic holder record at offset 64 — flock/msvcrt stays the sole authority.
        # Never touch bytes 0-63: Windows msvcrt locks byte 0.
        try:
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            rec = f"{os.getpid():>10d} {int(time.time()):>10d} {iso:<32s}".encode("ascii")
            fh.seek(_HOLDER_OFF)
            fh.write(rec)
            fh.flush()
        except Exception:
            pass
        return True

    def acquire(self, timeout: float = 0.0) -> bool:
        if timeout > 0 and not self._poll_s:
            raise ValueError(
                "AudioLock(poll_s=...) is required to WAIT for the lock: a file lock has "
                "no readiness event, and this class will not invent a retry cadence. "
                "The daemon's lifecycle block owns it.")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if self._try_lock():
                    return True
            except OSError:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._poll_s)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(_HOLDER_OFF)
            fh.write(_HOLDER_BLANK)
            fh.flush()
        except Exception:
            pass
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def __enter__(self) -> "AudioLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()
# b3: lifecycle-ports end
