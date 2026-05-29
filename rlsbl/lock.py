"""Advisory file lock using fcntl.flock to prevent concurrent rlsbl operations from mutating project state in both regular and monorepo modes."""

import atexit
import fcntl
import os
import sys
from contextlib import contextmanager

# Module-level fd so the lock persists for the process lifetime
_lock_fd = None


def acquire_lock(lock_dir=".rlsbl", *, project_root):
    """Acquire an exclusive advisory lock on <lock_dir>/lock.

    If another process holds the lock, prints a waiting message and
    blocks until the lock is available. Returns early if already locked
    (prevents fd leak on double-acquire).

    lock_dir: directory for the lock file (default ".rlsbl").
              In monorepo mode pass ".rlsbl-monorepo".
    project_root: when provided, lock_dir is resolved relative to it.
    """
    if project_root is not None:
        lock_dir = os.path.join(str(project_root), lock_dir)
    global _lock_fd

    # Guard against double-acquire: if already holding the lock, return early
    if _lock_fd is not None:
        return

    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, "lock")

    _lock_fd = open(lock_path, "w")

    try:
        # Try non-blocking first to detect contention
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
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
        # that dirties the working tree for subsequent operations.
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass
        # Remove the containing directory if empty (cleans up spurious
        # dirs created by the bug where lock_root pointed at the wrong path).
        try:
            os.rmdir(os.path.dirname(lock_path))
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
        fd = open(lock_path, "w")
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
def rlsbl_lock(lock_dir=".rlsbl", *, project_root):
    """Context manager that acquires the lock on enter and releases on exit."""
    acquire_lock(lock_dir=lock_dir, project_root=project_root)
    try:
        yield
    finally:
        release_lock()
