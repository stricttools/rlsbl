"""The single authorized surface for effectful calls in rlsbl production code.

Every subprocess launch, filesystem mutation, and network call made by
``rlsbl/`` goes through this module.  Nothing else in the package may call
``subprocess.run``, ``open(path, "w")``, ``os.replace``, ``shutil.rmtree``,
``urllib.request.urlopen``, or their siblings directly --
``tests/test_effects_chokepoint.py`` enforces that with an AST scan and a tiny
explicit exemption list.

Why a chokepoint: rlsbl is migrating onto strictcli's ``ctx.effects`` regime,
where every mutation is declared, previewable under ``--dry-run``, and
recorded.  With every effect funnelled through this one module, that migration
adapts one file instead of ~380 call sites.

The wrappers are deliberately thin and behavior-preserving: they forward to the
stdlib with the same arguments and let the stdlib's own exceptions
(``subprocess.CalledProcessError``, ``TimeoutExpired``, ``OSError``, ...)
propagate unchanged, so call sites keep their existing ``except`` clauses.
"""

import os
import shutil
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
    explicit (no ``**kwargs``) so the accepted surface stays closed and the
    later ``ctx.effects.run`` adaptation has a finite signature to map.

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


# ---------------------------------------------------------------------------
# Network effects
# ---------------------------------------------------------------------------


def gh(
    args,
    *,
    repo=None,
    cwd=None,
    env=None,
    timeout=None,
    check=False,
    capture_output=False,
    text=False,
):
    """Invoke the ``gh`` CLI.

    When *repo* is given, ``GH_REPO`` is injected into a per-call environment
    copy so ``gh`` targets that repository.  ``os.environ`` is never mutated
    (critical for thread-safety in watch.py's ThreadPoolExecutor).
    """
    if repo is not None:
        base = env if env is not None else os.environ
        env = {**base, "GH_REPO": repo}
    return run(
        ["gh", *args],
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=check,
        capture_output=capture_output,
        text=text,
    )


def gh_argv(args):
    """The argv a :func:`gh` call would execute (for previews and messages)."""
    return ["gh", *args]


def urlopen(url, *, timeout=None):
    """Open an HTTP(S) request and return the response object.

    *url* is a URL string or a ``urllib.request.Request``.  The return value is
    a context manager, exactly as ``urllib.request.urlopen`` returns.
    """
    if timeout is None:
        return urllib.request.urlopen(url)
    return urllib.request.urlopen(url, timeout=timeout)


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


def atomic_write_text(path, content, *, encoding="utf-8", preserve_mode=False):
    """Write *content* to *path* atomically (temp file + :func:`os.replace`).

    A crash mid-write can never leave a truncated file: the content lands in a
    sibling temp file that is renamed over the target in one directory
    operation.  Because the rename is a directory operation it also succeeds
    when *path* itself is read-only (0o444 changelog files), with no unlock
    step.

    When *preserve_mode* is true and *path* already exists, the replacement
    carries the target's ORIGINAL mode -- a deliberately locked file (a 0o444
    released changelog, say) must not silently become writable.  Otherwise the
    new file gets the umask-derived default, matching plain ``open(path, "w")``
    exactly: ``tempfile.mkstemp`` creates 0o600 files, so the mode is always
    set explicitly rather than inherited from the temp file.
    """
    directory = os.path.dirname(path) or "."
    target_mode = None
    if preserve_mode and os.path.exists(path):
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
