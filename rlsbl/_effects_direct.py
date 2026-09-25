"""The direct stdlib primitives behind :mod:`rlsbl.effects`.

This module is the *bottom* of the effect chokepoint: the only place in
``rlsbl/`` that may call ``subprocess.run``, ``open(path, "w")``,
``os.replace``, ``shutil.rmtree``, ``urllib.request.urlopen`` and their
siblings.  Nothing imports it except :mod:`rlsbl.effects`, which decides --
per the mode rule documented there -- whether an operation executes here or is
minted on strictcli's ``ctx.effects`` handle instead.

It is deliberately free of any mention of ``ctx.effects``: strictcli's built-in
``effects-bypass`` lint roots its reachability analysis at registered handlers
and at functions that reach for the handle, so keeping the primitives in a
module that does neither is what makes them invisible to it -- the same reason
``tests/test_effects_chokepoint.py`` exempts this file by name.

The wrappers are deliberately thin and behavior-preserving: they forward to the
stdlib with the same arguments and let the stdlib's own exceptions
(``subprocess.CalledProcessError``, ``TimeoutExpired``, ``OSError``, ...)
propagate unchanged, so call sites keep their existing ``except`` clauses.
"""

import os
import shutil
import socket
import stat
import subprocess
import tempfile
import urllib.request

# ---------------------------------------------------------------------------
# Process effects
# ---------------------------------------------------------------------------


def run(
    argv,
    *,
    cwd=None,
    env=None,
    timeout=None,
    check=False,
    capture_output=False,
    text=False,
    shell=False,
):
    """Run a command and return the :class:`subprocess.CompletedProcess`.

    A behavior-preserving passthrough to ``subprocess.run``.  Every keyword is
    explicit (no ``**kwargs``) so the accepted surface stays closed and
    :mod:`rlsbl.effects` has a finite signature to route.

    Only non-default keywords reach ``subprocess.run``, so the underlying call
    is byte-identical to the direct call this wrapper replaced.

    Args:
        argv: argument list, or a shell string when *shell* is true.
        cwd: working directory for the child process.
        env: complete environment mapping for the child (None inherits).
        timeout: seconds before ``TimeoutExpired`` is raised.
        check: raise ``CalledProcessError`` on a non-zero exit.
        capture_output: capture stdout/stderr instead of inheriting them.
        text: decode captured streams as text.
        shell: run *argv* through the system shell.
    """
    kwargs = {}
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env
    if timeout is not None:
        kwargs["timeout"] = timeout
    if check:
        kwargs["check"] = True
    if capture_output:
        kwargs["capture_output"] = True
    if text:
        kwargs["text"] = True
    if shell:
        kwargs["shell"] = True
    return subprocess.run(argv, **kwargs)


def spawn(argv, *, cwd=None, env=None):
    """Start a child process without waiting for it, returning the Popen."""
    return subprocess.Popen(list(argv), cwd=cwd, env=env)

# ---------------------------------------------------------------------------
# Network effects
# ---------------------------------------------------------------------------


def urlopen(url, *, timeout=None):
    """Open an HTTP(S) request and return the response object.

    *url* is a URL string or a ``urllib.request.Request``.  The return value is
    a context manager, exactly as ``urllib.request.urlopen`` returns.
    """
    if timeout is None:
        return urllib.request.urlopen(url)
    return urllib.request.urlopen(url, timeout=timeout)


def tcp_connect(host, port, *, timeout=None):
    """Open a TCP connection to *host*:*port* and return the socket."""
    return socket.create_connection((host, port), timeout=timeout)


# ---------------------------------------------------------------------------
# Filesystem effects -- writes
# ---------------------------------------------------------------------------


def open_write(path, mode="w", *, encoding=None, newline=None):
    """Open *path* for writing and return the file object.

    A thin ``open`` wrapper for streaming writers (``json.dump``, loops of
    ``f.write``).  Use it as a context manager, exactly like ``open``.
    Whole-content writers should prefer :func:`write_text` /
    :func:`atomic_write_text`.
    """
    if "w" not in mode and "a" not in mode and "x" not in mode:
        raise ValueError(f"open_write requires a write mode, got {mode!r}")
    return open(path, mode, encoding=encoding, newline=newline)


def open_exclusive(path, *, file_mode=0o644, encoding="utf-8"):
    """Create *path* and return it open for writing, failing if it exists.

    ``O_CREAT | O_EXCL`` closes a TOCTOU: an ``exists()`` check far above the
    write cannot be trusted, and this raises ``FileExistsError`` when a racer
    won.  The mode is passed at creation rather than chmod'ed afterwards, so
    the file is never briefly wider than intended.
    """
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, file_mode)
    return os.fdopen(fd, "w", encoding=encoding)


def write_text(path, content, *, encoding="utf-8", newline=None):
    """Write *content* to *path*, truncating any existing file."""
    with open(path, "w", encoding=encoding, newline=newline) as f:
        f.write(content)


def append_text(path, content, *, encoding="utf-8"):
    """Append *content* to *path*, creating it when absent."""
    with open(path, "a", encoding=encoding) as f:
        f.write(content)


def write_bytes(path, data):
    """Write *data* to *path*, truncating any existing file."""
    with open(path, "wb") as f:
        f.write(data)


def atomic_write_text(
    path, content, *, encoding="utf-8", preserve_mode=False, file_mode=None
):
    """Write *content* to *path* atomically (temp file + :func:`os.replace`).

    A crash mid-write can never leave a truncated file: the content lands in a
    sibling temp file that is renamed over the target in one directory
    operation.  Because the rename is a directory operation it also succeeds
    when *path* itself is read-only (0o444 changelog files), with no unlock
    step.

    Permission bits of the result, in precedence order:

    * *file_mode*, when given, is applied verbatim.
    * *preserve_mode* keeps an existing target's ORIGINAL bits -- a
      deliberately locked file (a 0o444 released changelog, say) must not
      silently become writable.
    * otherwise the umask-derived default, matching plain ``open(path, "w")``.

    The mode is always set explicitly because ``tempfile.mkstemp`` creates
    0o600 files; inheriting that would silently narrow every rewritten file.
    """
    if file_mode is not None and preserve_mode:
        raise ValueError("pass either file_mode or preserve_mode, not both")

    directory = os.path.dirname(path) or "."
    if file_mode is not None:
        target_mode = file_mode
    elif preserve_mode and os.path.exists(path):
        target_mode = stat.S_IMODE(os.stat(path).st_mode)
    else:
        # Mirror open(path, "w") for a new file: 0o666 masked by the umask.
        current_umask = os.umask(0)
        os.umask(current_umask)
        target_mode = 0o666 & ~current_umask

    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.chmod(tmp_path, target_mode)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# ---------------------------------------------------------------------------
# Filesystem effects -- temporary files and directories
# ---------------------------------------------------------------------------


def temp_root():
    """The directory temporary files are created in when no *dir* is given."""
    return tempfile.gettempdir()


def mkdtemp(*, prefix=None, suffix=None, dir=None):
    """Create a temporary directory and return its path."""
    return tempfile.mkdtemp(prefix=prefix, suffix=suffix, dir=dir)


def temp_file(content, *, prefix=None, suffix=None, dir=None, encoding="utf-8"):
    """Create a temporary file holding *content* and return its path.

    The file is closed on return and is never deleted automatically -- the
    caller owns it, exactly as ``NamedTemporaryFile(delete=False)`` did.
    """
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=dir)
    with os.fdopen(fd, "w", encoding=encoding) as f:
        f.write(content)
    return path


# ---------------------------------------------------------------------------
# Filesystem effects -- directories, moves, deletions, modes
# ---------------------------------------------------------------------------


def makedirs(path, *, exist_ok=False):
    """Create *path* and any missing parents.

    The default mirrors ``os.makedirs`` exactly (an existing *path* raises)
    so translating a call site never changes its behavior.
    """
    os.makedirs(path, exist_ok=exist_ok)


def mkdir(path):
    """Create a single directory *path* (parents must already exist)."""
    os.mkdir(path)


def rename(src, dst):
    """Rename *src* to *dst*, failing if *dst* exists (POSIX: overwrites)."""
    os.rename(src, dst)


def replace(src, dst):
    """Atomically move *src* onto *dst*, overwriting *dst* if it exists."""
    os.replace(src, dst)


def remove(path, *, missing_ok=False):
    """Delete the file at *path*."""
    if missing_ok:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
    else:
        os.unlink(path)


def rmdir(path):
    """Remove the empty directory at *path*."""
    os.rmdir(path)


def removedirs(path):
    """Remove *path* and then each now-empty parent directory."""
    os.removedirs(path)


def rmtree(path, *, ignore_errors=False):
    """Recursively delete the directory tree at *path*."""
    shutil.rmtree(path, ignore_errors=ignore_errors)


def chmod(path, mode):
    """Set the permission bits of *path*."""
    os.chmod(path, mode)


def copy_file(src, dst):
    """Copy *src* to *dst*, preserving metadata (``shutil.copy2``)."""
    return shutil.copy2(src, dst)


def copytree(src, dst, *, dirs_exist_ok=False, ignore=None, symlinks=False):
    """Recursively copy the directory tree *src* to *dst*."""
    return shutil.copytree(
        src, dst, dirs_exist_ok=dirs_exist_ok, ignore=ignore, symlinks=symlinks
    )
