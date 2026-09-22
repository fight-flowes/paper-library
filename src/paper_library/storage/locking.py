"""Cross-process gates; pending maintenance prevents writer starvation."""
from contextlib import contextmanager
import fcntl
from pathlib import Path
import time


def _acquire(stream, mode, blocking, timeout):
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(stream, mode | (0 if blocking and deadline is None else fcntl.LOCK_NB))
            return
        except BlockingIOError:
            if not blocking or (deadline is not None and time.monotonic() >= deadline):
                raise ValueError('正在执行任务或维护操作，请稍后重试') from None
            time.sleep(0.05)


@contextmanager
def gate(root: Path, *, exclusive=False, blocking=True, timeout=None):
    root.mkdir(parents=True, exist_ok=True)
    # Exclusive maintenance holds intent while draining existing writers. New
    # writers cannot continually jump ahead. flock releases intent after crashes.
    deadline = None if timeout is None else time.monotonic() + timeout
    def remaining():
        return None if deadline is None else max(0, deadline-time.monotonic())
    with (root / '.maintenance-intent.lock').open('a') as intent:
        _acquire(intent, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH, blocking, remaining())
        try:
            with (root / '.maintenance.lock').open('a') as stream:
                _acquire(stream, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH, blocking, remaining())
                if not exclusive:
                    fcntl.flock(intent, fcntl.LOCK_UN)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)
        finally:
            fcntl.flock(intent, fcntl.LOCK_UN)


@contextmanager
def paper_lock(root: Path):
    directory = root.parent / '.paper-locks'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (root.name + '.lock')).open('a') as stream:
        _acquire(stream, fcntl.LOCK_EX, False, None)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
