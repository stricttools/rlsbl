"""Advisory file lock using fcntl.flock to prevent concurrent rlsbl operations from mutating project state in both regular and monorepo modes.

One lock file per state home -- ``.rlsbl/lock`` for a standalone project,
``.rlsbl-monorepo/lock`` for a workspace -- and every operation that mutates
that state takes it: ``release run``, ``monorepo release run``, ``scaffold``,
``release scrub``, and the repository conversions. There is deliberately no
second lock elsewhere (under ``.git/``, say): a lock a release does not take
excludes nothing, and the whole point of the conversion taking one is that it
cannot run while a release is running.

Waiting is the default and refusing is the alternative. A short operation
queueing behind another is ordinary; a long, destructive one (a conversion that
rewrites history into a new repository) is better refused with the holder named
than left blocked behind a release that may be waiting on CI, which is what
``wait=False`` is for.
"""

import atexit
import fcntl
import os
import sys
from contextlib import contextmanager
from . import effects
from .errors import RlsblError

# Module-level fd so the lock persists for the process lifetime
_lock_fd = None


class LockHeldError(RlsblError):
    """The advisory lock is held by another process and waiting was refused."""


def acquire_lock(lock_dir=".rlsbl", *, project_root, wait=True):
    """Acquire an exclusive advisory lock on <lock_dir>/lock.

    If another process holds the lock and ``wait`` is True, prints a waiting
    message and blocks until the lock is available. Returns early if already
    locked (prevents fd leak on double-acquire).

    lock_dir: directory for the lock file (default ".rlsbl").
              In monorepo mode pass ".rlsbl-monorepo".
    project_root: when provided, lock_dir is resolved relative to it.
    wait: block until the lock is free (the default). With ``wait=False`` a
          held lock raises :class:`LockHeldError` naming the lock file instead,
          for an operation whose caller would rather be told than queued.
    """
    if project_root is not None:
        lock_dir = os.path.join(str(project_root), lock_dir)
    global _lock_fd

    # Guard against double-acquire: if already holding the lock, return early
    if _lock_fd is not None:
        return

    # Real in every mode, preview included: flock needs a real descriptor, and
    # two concurrent previews race on project state exactly like two live runs
    # do.  See the "Process infrastructure" section of rlsbl/effects.py.
    effects.lock_makedirs(lock_dir)
    lock_path = os.path.join(lock_dir, "lock")

    _lock_fd = effects.lock_open(lock_path)

    try:
        # Try non-blocking first to detect contention
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        if not wait:
            # Refused, not queued: drop the descriptor first so the refusal
            # leaves no half-acquired state behind for a later acquire.
            _lock_fd.close()
            _lock_fd = None
            raise LockHeldError(
                f"another rlsbl process holds {lock_path}. A release or a "
                f"repository conversion is already mutating this workspace's "
                f"state; wait for it to finish (or resume it) and re-run."
            )
        # Another process holds the lock -- inform user and wait
        print("Another rlsbl process is running. Waiting...", file=sys.stderr)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX)

    atexit.register(release_lock)


def release_lock():
    """Release the advisory lock, close the file descriptor, and remove the lock file."""
    global _lock_fd

    if _lock_fd is not None:
        lock_path = _lock_fd.name
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
        _lock_fd = None
        # Remove the lock file so it doesn't leave an untracked file
        # that dirties the working tree for subsequent operations.  Recorded
        # removals would leave the real file a preview really created behind.
        try:
            effects.lock_remove(lock_path)
        except FileNotFoundError:
            pass
        # Remove the containing directory if empty (cleans up spurious
        # dirs created by the bug where lock_root pointed at the wrong path).
        try:
            effects.lock_rmdir(os.path.dirname(lock_path))
        except OSError:
            pass


def is_stale(lock_path=None, *, project_root):
    """Check if a lock file exists but no process holds it.

    Returns True if the file exists and is not held (stale).
    Returns False if the file doesn't exist or is actively held.

    project_root: when lock_path is None, build the default
                  lock path relative to project_root.
    """
    if lock_path is None:
        lock_path = os.path.join(str(project_root), ".rlsbl", "lock")

    if not os.path.exists(lock_path):
        return False

    fd = None
    try:
        # Real in every mode for the same reason acquire_lock's is: the probe
        # IS the flock, and a recorded stand-in has no descriptor to take.
        fd = effects.lock_open(lock_path)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Lock acquired means no one holds it -- stale
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except (OSError, BlockingIOError):
        # Another process holds the lock -- not stale
        return False
    finally:
        if fd is not None:
            fd.close()


@contextmanager
def rlsbl_lock(lock_dir=".rlsbl", *, project_root, wait=True):
    """Context manager that acquires the lock on enter and releases on exit.

    ``wait=False`` refuses a held lock with :class:`LockHeldError` instead of
    queueing behind its holder -- see :func:`acquire_lock`.
    """
    acquire_lock(lock_dir=lock_dir, project_root=project_root, wait=wait)
    try:
        yield
    finally:
        release_lock()
