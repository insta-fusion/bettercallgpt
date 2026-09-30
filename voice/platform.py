"""What differs between operating systems, in one file.

Three things, nothing else:

* `watch(path, poll_s=...)` — something to `await` until a file or directory changes. macOS
  gets kqueue (the event loop waits on the kqueue fd; nothing sleeps). Linux and Windows get a
  stat-signature poll at the cadence the DAEMON injects (`poll_s` has no default here). No new
  dependency on any OS.
* `install_signal(loop, sig, callback)` — `loop.add_signal_handler` where the loop supports it,
  the plain `signal.signal` path (handed back to the loop thread-safely) where it does not
  (Windows, a non-main thread), and a clean `False` where neither can be installed.
* `private_dir(path)` — create a directory only its owner can enter (0700 on POSIX; an
  existing one is tightened too). The state directory holds the conversation ledger, so on a
  shared machine it must not be readable by other local users. Windows keeps the inherited
  per-user ACL of the profile directory; `chmod` there cannot express more.

Everything else OS-specific stays where it is and is DECLARED, not hidden: the Claude Code
relay's peer check (`LOCAL_PEERPID`) and the pane's `ps` probe are macOS-only, and the backend
registry says so (`voice/backend/registry.py`), so an unsupported combination is refused at
preflight instead of failing mid-call.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any, Callable

PLATFORM = "darwin" if sys.platform == "darwin" else ("windows" if os.name == "nt" else "linux")


def private_dir(path: str | Path) -> Path:
    """`path` as a directory only its owner can enter. Idempotent; tightens an existing one."""
    p = Path(path)
    # Missing ancestors are created owner-only too: a world-listable parent would reveal the
    # session ids it holds. Existing ancestors are left as the operator made them.
    for parent in reversed([q for q in p.parents if not q.exists()]):
        parent.mkdir(mode=0o700, exist_ok=True)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(p, 0o700)
    return p


def has_kqueue() -> bool:
    import select

    return hasattr(select, "kqueue")


# ------------------------------------------------------------------ watch

class _KqueueWatch:
    """kqueue on one path. A DIRECTORY is watched for writes (an atomic `os.replace` into it
    is a write to the directory, so a watch on the old file would go deaf); a FILE for write,
    extend, rename and delete, re-armed on the path after a rename/delete so a rotated file is
    followed. `wait()` hands the kqueue fd to the loop; nothing here sleeps."""

    def __init__(self, path: Path) -> None:
        import select

        self._select = select
        self.path = Path(path)
        self._is_dir = self.path.is_dir()
        self._kq = select.kqueue()
        self._fd: int | None = None
        self._arm()

    def _arm(self) -> None:
        sel = self._select
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = os.open(str(self.path), os.O_RDONLY)
        fflags = (sel.KQ_NOTE_WRITE if self._is_dir else
                  sel.KQ_NOTE_WRITE | sel.KQ_NOTE_EXTEND | sel.KQ_NOTE_RENAME
                  | sel.KQ_NOTE_DELETE)
        event = sel.kevent(self._fd, filter=sel.KQ_FILTER_VNODE,
                           flags=sel.KQ_EV_ADD | sel.KQ_EV_ENABLE | sel.KQ_EV_CLEAR,
                           fflags=fflags)
        self._kq.control([event], 0, 0)

    def fileno(self) -> int:
        return self._kq.fileno()

    def drain(self) -> None:
        sel = self._select
        events = self._kq.control(None, 64, 0)
        if not self._is_dir and any(e.fflags & (sel.KQ_NOTE_RENAME | sel.KQ_NOTE_DELETE)
                                    for e in events):
            try:
                self._arm()
            except OSError:
                pass

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        fired = loop.create_future()
        loop.add_reader(self.fileno(), lambda: None if fired.done() else fired.set_result(None))
        try:
            await fired
        finally:
            loop.remove_reader(self.fileno())
            self.drain()

    __call__ = wait

    def close(self) -> None:
        try:
            self._kq.close()
        finally:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None


def _signature(path: Path) -> Any:
    """What changes when the path's content changes: for a file its stat, for a directory the
    stat of every entry (an atomic replace changes the entry's inode and mtime)."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_dir():
        return (st.st_ino, st.st_size, st.st_mtime_ns)
    entries = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: e.name):
            try:
                est = entry.stat()
            except OSError:
                continue
            entries.append((entry.name, est.st_ino, est.st_size, est.st_mtime_ns))
    except OSError:
        pass
    return (st.st_mtime_ns, tuple(entries))


class _PollWatch:
    """The portable fallback: compare the path's stat signature at the injected cadence. The
    cadence is a LIFECYCLE PORT (how often to look), never a decision: nothing is released by
    time passing, only by the signature changing."""

    def __init__(self, path: Path, *, poll_s: float) -> None:
        self.path = Path(path)
        self._poll_s = poll_s
        self._last = _signature(self.path)
        self._closed = False

    async def wait(self) -> None:
        while not self._closed:
            # b3: lifecycle-ports begin  (the portable watch's poll cadence — injected)
            await asyncio.sleep(self._poll_s)
            # b3: lifecycle-ports end
            now = _signature(self.path)
            if now != self._last:
                self._last = now
                return
        # A closed watch never fires again; the waiter is cancelled by its owner.
        await asyncio.Event().wait()

    __call__ = wait

    def close(self) -> None:
        self._closed = True


def watch(path: str | Path, *, poll_s: float) -> Any:
    """An awaitable change watch on `path` (a file or a directory), or None when the path
    cannot be opened at all. kqueue where it exists, a stat poll everywhere else."""
    path = Path(path)
    if has_kqueue():
        try:
            return _KqueueWatch(path)
        except OSError:
            pass
    if not path.exists():
        return None
    return _PollWatch(path, poll_s=poll_s)


# ------------------------------------------------------------------ signals

def install_signal(loop: Any, sig: int, callback: Callable[[], Any]) -> bool:
    """Run `callback` on the loop when `sig` arrives. True when a handler is installed.

    `add_signal_handler` does not exist on Windows' proactor loop and refuses off the main
    thread; there the plain handler hands the callback back to the loop thread-safely. A
    platform that has neither (or a signal it does not define) is a clean False — the
    session still ends by its own control channel, it just cannot be ended by that signal.
    """
    try:
        loop.add_signal_handler(sig, callback)
        return True
    except (NotImplementedError, RuntimeError, ValueError, AttributeError):
        pass
    try:
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(callback))
        return True
    except (ValueError, OSError, AttributeError, RuntimeError):
        return False
