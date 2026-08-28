"""The single authorized surface for effectful calls in rlsbl production code.

Every subprocess launch, filesystem mutation, and network call made by
``rlsbl/`` goes through this module.  Nothing else in the package may call
``subprocess.run``, ``open(path, "w")``, ``os.replace``, ``shutil.rmtree``,
``urllib.request.urlopen``, or their siblings directly --
``tests/test_effects_chokepoint.py`` enforces that with an AST scan and a tiny
explicit exemption list (this module and :mod:`rlsbl._effects_direct`, which
holds the primitives).

Why a chokepoint: rlsbl rides strictcli's ``ctx.effects`` regime, where every
mutation is declared, previewable under ``--dry-run``, and recorded.  With
every effect funnelled through this one module, that regime is adapted in one
file instead of ~380 call sites.

The mode rule (declared, never inferred)
----------------------------------------

A command handler binds the dispatch context here (``@effects.handler``), and
from then on:

* **Preview mode** (``--dry-run``; ``ctx.dry_run`` is true) -- every
  operation below is minted on ``ctx.effects``, with three named exceptions
  listed at the end of this docstring.  Mutations are recorded, never
  executed, and return strictcli's ``Unsettled`` carrier; a caller that
  forwards the carrier into a later effect keeps the preview going, and a
  caller that reads a field off it truncates the preview with the framework's
  own error.  Subprocess runs whose argv matches the app's
  ``proc_observe_allowlist`` are observes: they really execute and return real
  values, which is what lets the release engine's read-then-branch code walk a
  preview end to end.
* **Live mode** -- the operations execute through
  :mod:`rlsbl._effects_direct`, with their full rlsbl semantics: per-call
  timeouts, byte-mode captures, ``atomic_write_text``'s temp-file + rename
  (the only way to rewrite a 0o444 released changelog), and the
  ``exist_ok`` / ``missing_ok`` distinctions call sites branch on.  The
  contract's closed method set expresses none of those, so routing a live run
  through it would silently drop a hang guard or a permission-preserving
  rename.

The split is by *mode*, decided before anything runs, and identical on every
invocation -- it is not a fallback: nothing here ever tries the handle, fails,
and retries elsewhere.  What preview mode buys (recording, read-only
enforcement, the would-do log) it buys in full; what live mode keeps
(timeouts, atomicity, byte fidelity) it keeps in full.

Unbound calls -- the library path -- execute directly too.  rlsbl's checks,
its programmatic API and its own test suite call these functions outside any
command dispatch; there is no handle to mint on there.
``tests/test_effects_binding.py`` asserts that every registered command
handler carries ``@effects.handler``, so a bound path is never missed by
accident.

The operations that execute in every mode
-----------------------------------------

Each is declared by name, has a reason that is about the operation rather
than about convenience, and touches nothing a preview reports on.  The list
below is the whole set:

* :func:`lock_makedirs` / :func:`lock_open` / :func:`lock_remove` /
  :func:`lock_rmdir` -- the advisory lock is process infrastructure.  A
  preview needs mutual exclusion as much as a live run, ``fcntl.flock``
  needs a real descriptor, and the lock file is created and deleted inside
  the same process's lifetime.
* :func:`observe_scratch_files` -- operands of an allowlisted observe.  The
  observe really runs under --dry-run, so recorded stand-ins would leave it
  reading absent paths and reporting a failure that is about the preview
  rather than about the project.
* :func:`tcp_connect` -- a connect-and-close probe is a network read, and
  reads execute in every mode here (see :func:`urlopen`).
* :func:`mkdtemp` and the matching :func:`rmtree`, but ONLY inside
  :func:`observe_scratch_dirs` -- same reason as ``observe_scratch_files``,
  for the directory an allowlisted observe writes into rather than for the
  files it reads.  Outside that block both record as usual.

Everything else, including :func:`mkdtemp` and :func:`temp_file` in every
other context, records.
"""

import functools
import io
import itertools
import os
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar

import strictcli

from . import _effects_direct as _direct

# The dispatch context of the command currently running, or None outside a
# command dispatch (checks, library callers, direct unit-test calls).
_CTX: ContextVar = ContextVar("rlsbl_effects_ctx", default=None)

# Numbers the synthetic temp paths a preview hands back, so two staging
# directories in one preview are told apart in the would-do log.
_preview_temp_seq = itertools.count(1)

# The scratch directories the running observation created, or None outside an
# observation block.  See :func:`observe_scratch_dirs`.
_OBSERVE_SCRATCH: ContextVar = ContextVar("rlsbl_effects_observe_scratch", default=None)


def handler(fn):
    """Bind the dispatch context to this module for the length of a handler.

    Applied innermost on every rlsbl command handler, under the
    ``@app.command(...)`` / ``@strictcli.flag(...)`` stack.  ``functools.wraps``
    keeps ``inspect.signature`` reporting the wrapped handler's real parameters,
    so strictcli's guard v2 still validates the declared flags and args against
    the signature it would have seen without the wrapper -- no ``forwarding=``
    waiver is needed.
    """

    @functools.wraps(fn)
    def wrapper(ctx, *args, **kwargs):
        token = _CTX.set(ctx)
        try:
            return fn(ctx, *args, **kwargs)
        finally:
            _CTX.reset(token)

    wrapper.__rlsbl_effects_handler__ = True
    return wrapper


def unsettled(value):
    """True when *value* is a carrier standing in for a recorded mutation.

    The one thing a caller may do with a carrier besides forwarding it into a
    later effect: recognize it, and decline to read a result that does not
    exist.  ``rlsbl.utils.run`` and its ``gh`` siblings use it to return the
    carrier itself instead of reaching for ``.stdout`` -- so a preview walks
    past a mutation whose output nobody needed, and truncates (honestly) at the
    first caller that does need it.
    """
    return isinstance(value, strictcli.Unsettled)


def previewing():
    """True when the current dispatch is previewing rather than executing."""
    return _handle() is not None


def render_would_do_log():
    """Emit strictcli's would-do log HERE instead of at the end of the dispatch.

    The framework prints that log on every dry run, and a handler whose whole
    answer is a rendered plan otherwise finishes by announcing "Would do:" with
    nothing under it -- the plan it means sits ABOVE the header, because
    observation may record nothing (it runs above the no-writes line) and the
    apply never runs.  Calling this first puts the header where it belongs: at
    the top, introducing the plan.

    ``render_log()`` both claims and produces the log (strictcli contract
    §19.7), so the framework's own end-of-dispatch emission is suppressed and
    the log appears exactly once.  Anything recorded AFTER this call would
    therefore not be rendered, so this belongs at the point a handler knows its
    remaining output is the plan itself.  A no-op outside preview mode.
    """
    handle = _handle()
    if handle is not None:
        handle.render_log()


def _handle():
    """The strictcli effects handle to mint on, or None to execute directly."""
    ctx = _CTX.get()
    if ctx is None or not getattr(ctx, "dry_run", False):
        return None
    return ctx.effects


def _p(path):
    """Render a path operand as text for the handle."""
    return os.fspath(path)


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
    resource=None,
    skip_if_current=None,
    grant=None,
):
    """Run a command and return the :class:`subprocess.CompletedProcess`.

    In preview mode the call is minted on ``ctx.effects.run``: an allowlisted
    observe really executes and returns a ``CompletedProcess`` as always, and
    anything else is recorded and returns the ``Unsettled`` carrier standing in
    for the run that did not happen.

    Args:
        argv: argument list, or a shell string when *shell* is true.
        cwd: working directory for the child process.
        env: complete environment mapping for the child (None inherits).
        timeout: seconds before ``TimeoutExpired`` is raised.
        check: raise ``CalledProcessError`` on a non-zero exit.
        capture_output: capture stdout/stderr instead of inheriting them.
        text: decode captured streams as text.
        shell: run *argv* through the system shell.
        resource: opaque token naming what this run produces (preview only).
        skip_if_current: token the preview annotates the line with, spelling
            out that the handler skips this step when the resource is current.
        grant: name of a grant declared on the running command, whose reason
            is rendered beside the step in the preview.
    """
    h = _handle()
    if h is None:
        return _direct.run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=check,
            capture_output=capture_output,
            text=text,
            shell=shell,
        )

    # ``shell=True`` is exactly ``/bin/sh -c <string>``; the contract's method
    # set takes argv lists only, so the shell is spelled out.
    listed = ["/bin/sh", "-c", argv] if shell else list(argv)
    result = h.run(
        listed,
        cwd=cwd,
        env=env,
        check=False,
        stream=not capture_output,
        resource=resource,
        skip_if_current=skip_if_current,
        grant=grant,
    )
    if isinstance(result, strictcli.Unsettled):
        # A recorded mutation: nothing ran, so there is no exit code to test.
        # Forwarding this into a later effect keeps the preview going; reading
        # a field off it truncates, which is the honest outcome.
        return result

    stdout, stderr = result.stdout, result.stderr
    if not capture_output:
        stdout = stderr = None
    elif not text:
        stdout, stderr = stdout.encode(), stderr.encode()
    if check and result.exit_code != 0:
        raise subprocess.CalledProcessError(result.exit_code, listed, stdout, stderr)
    return subprocess.CompletedProcess(listed, result.exit_code, stdout, stderr)


def spawn(argv, *, cwd=None, env=None):
    """Start a child process without waiting for it (``PROC_SPAWN``).

    In preview mode the spawn is recorded and no child is ever forked -- which
    is the whole reason the regime needs no cross-process mode token.
    """
    h = _handle()
    if h is None:
        return _direct.spawn(argv, cwd=cwd, env=env)
    return h.spawn(list(argv), cwd=cwd, env=env)


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
        # ``is not None``, not truthiness: an explicitly empty *env* means an
        # empty child environment and must not silently widen to os.environ.
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

    A ``GET`` or ``HEAD`` is a network *read* and executes in every mode --
    reads are never effects, and a preview that could not probe a registry
    would have nothing to preview.  Any other method is a network mutation and
    is minted on ``ctx.effects.http``, so a preview records it instead of
    performing it.
    """
    h = _handle()
    method = url.get_method() if hasattr(url, "get_method") else "GET"
    if h is None or method in ("GET", "HEAD"):
        return _direct.urlopen(url, timeout=timeout)
    return h.http(
        method,
        url.full_url,
        body=url.data,
        headers=dict(url.header_items()),
        check=False,
    )


def tcp_connect(host, port, *, timeout=None):
    """Open a TCP connection to *host*:*port* and return the socket.

    Connect-and-close is a network *read*: it leaves nothing behind on the
    far side, so -- exactly like a ``GET`` -- it executes in every mode.  A
    deploy health check that could not reach the host under a preview would
    report a failure that says nothing about the deploy being previewed.

    It still lives here rather than in the caller so the network surface stays
    enumerable in one place: ``tests/test_effects_chokepoint.py`` bans
    ``socket``, ``http.client`` and ``requests`` everywhere else.
    """
    return _direct.tcp_connect(host, port, timeout=timeout)


# ---------------------------------------------------------------------------
# Filesystem effects -- writes
# ---------------------------------------------------------------------------


class _RecordedWriter(io.StringIO):
    """A file-like sink that mints one ``write`` effect when it is closed.

    ``open_write`` hands streaming writers (``json.dump``, loops of
    ``f.write``) a real file object in live mode.  The contract has no
    streaming write, so in preview mode the content accumulates here and the
    single resulting ``write`` carries the byte count the file would have had.
    """

    def __init__(self, handle, path, mode, resource=None, skip_if_current=None):
        super().__init__()
        self._handle = handle
        self._path = path
        self._append = "a" in mode
        self._resource = resource
        self._skip_if_current = skip_if_current

    def close(self):
        if self.closed:
            return
        content = self.getvalue()
        if self._append and os.path.exists(self._path):
            with open(self._path, encoding="utf-8") as f:
                content = f.read() + content
        self._handle.write(
            self._path, content,
            resource=self._resource, skip_if_current=self._skip_if_current,
        )
        super().close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def open_write(path, mode="w", *, encoding=None, newline=None, resource=None,
               skip_if_current=None):
    """Open *path* for writing and return the file object.

    A thin ``open`` wrapper for streaming writers (``json.dump``, loops of
    ``f.write``).  Use it as a context manager, exactly like ``open``.
    Whole-content writers should prefer :func:`write_text` /
    :func:`atomic_write_text`.
    """
    if "w" not in mode and "a" not in mode and "x" not in mode:
        raise ValueError(f"open_write requires a write mode, got {mode!r}")
    h = _handle()
    if h is None:
        return _direct.open_write(path, mode, encoding=encoding, newline=newline)
    return _RecordedWriter(h, _p(path), mode, resource, skip_if_current)


def open_exclusive(path, *, file_mode=0o644, encoding="utf-8"):
    """Create *path* open for writing, raising ``FileExistsError`` if it exists.

    The exclusive create is what closes the TOCTOU between an ``exists()``
    check and the write that follows it, so call sites branch on
    ``FileExistsError`` rather than re-checking.
    """
    h = _handle()
    if h is None:
        return _direct.open_exclusive(path, file_mode=file_mode, encoding=encoding)
    return _RecordedWriter(h, _p(path), "x")


def write_text(path, content, *, encoding="utf-8", newline=None):
    """Write *content* to *path*, truncating any existing file."""
    h = _handle()
    if h is None:
        _direct.write_text(path, content, encoding=encoding, newline=newline)
        return
    h.write(_p(path), content)


def append_text(path, content, *, encoding="utf-8"):
    """Append *content* to *path*, creating it when absent."""
    h = _handle()
    if h is None:
        _direct.append_text(path, content, encoding=encoding)
        return
    existing = ""
    if os.path.exists(path):
        with open(path, encoding=encoding) as f:
            existing = f.read()
    h.write(_p(path), existing + content)


def append_lines(path, lines, *, encoding="utf-8"):
    """Append *lines* to *path* as ONE append, each on its own line.

    This is the append every record-file writer here uses -- the changelog
    JSONL files and the lineage record alike -- and it carries the whole batch
    in a single :func:`append_text`.  Prior content is never read back and
    rewritten, so a concurrent writer cannot be clobbered and an already-written
    line is never modified.

    The one thing read first is the existing file's final byte: when the file is
    non-empty and does not end in a newline -- an interrupted write, a hand edit
    -- a separating newline leads the batch, so the new content starts its own
    line instead of being concatenated onto the damaged one.  The damaged line
    stays damaged; the file's reader is what names it.  Reading one byte is
    legal in every effects mode: it observes the world without changing it.

    An empty *lines* writes nothing at all, so a preview does not record an
    append that would carry no content.
    """
    body = "".join(line + "\n" for line in lines)
    if not body:
        return
    append_text(path, _newline_separator(path) + body, encoding=encoding)


def _newline_separator(path):
    """``"\\n"`` when *path* holds content not ending in a newline, else ``""``."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size == 0:
        return ""
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        last = f.read(1)
    return "" if last == b"\n" else "\n"


def write_bytes(path, data):
    """Write *data* to *path*, truncating any existing file."""
    h = _handle()
    if h is None:
        _direct.write_bytes(path, data)
        return
    h.write(_p(path), data)


def atomic_write_text(
    path, content, *, encoding="utf-8", preserve_mode=False, file_mode=None,
    resource=None, skip_if_current=None
):
    """Write *content* to *path* atomically (temp file + :func:`os.replace`).

    A crash mid-write can never leave a truncated file: the content lands in a
    sibling temp file that is renamed over the target in one directory
    operation.  Because the rename is a directory operation it also succeeds
    when *path* itself is read-only (0o444 changelog files), with no unlock
    step -- which is why this one never routes through the handle in live mode:
    the contract's ``write`` is a plain write and would fail on those files.

    Permission bits of the result, in precedence order:

    * *file_mode*, when given, is applied verbatim.
    * *preserve_mode* keeps an existing target's ORIGINAL bits -- a
      deliberately locked file (a 0o444 released changelog, say) must not
      silently become writable.
    * otherwise the umask-derived default, matching plain ``open(path, "w")``.
    """
    if file_mode is not None and preserve_mode:
        raise ValueError("pass either file_mode or preserve_mode, not both")
    h = _handle()
    if h is None:
        _direct.atomic_write_text(
            path,
            content,
            encoding=encoding,
            preserve_mode=preserve_mode,
            file_mode=file_mode,
        )
        return
    h.write(_p(path), content, resource=resource, skip_if_current=skip_if_current)
    if file_mode is not None:
        h.chmod(_p(path), file_mode)


# ---------------------------------------------------------------------------
# Filesystem effects -- temporary files and directories
# ---------------------------------------------------------------------------


def _preview_temp_path(prefix, suffix, dir):
    """A stable, obviously-synthetic path for a temp entry nobody creates.

    Preview mode has to hand the caller a *path string* rather than the
    ``Unsettled`` carrier: call sites join names onto it and pass it as a
    subprocess ``cwd``, and a carrier would truncate the preview at the first
    ``os.path.join`` instead of at the effect that actually matters.  The
    counter keeps two staging directories in one preview distinguishable in
    the would-do log.
    """
    parent = _p(dir) if dir is not None else _direct.temp_root()
    name = f"{prefix or 'tmp'}preview{next(_preview_temp_seq)}{suffix or ''}"
    return os.path.join(parent, name)


@contextmanager
def observe_scratch_dirs():
    """Make this block's scratch DIRECTORIES real in every mode.

    The scoped exception to "preview mode records everything".  It exists for
    observation -- the read-only phase of a reconciler (see
    :mod:`rlsbl.preview_apply`), which really runs under ``--dry-run`` because
    its subprocesses are allowlisted observes.  An observation that clones a
    remote into a temp directory needs that directory to EXIST: a recorded
    ``mkdir`` hands back a synthetic path, the allowlisted ``git clone`` aimed
    at it really runs and fails on a missing parent, and the preview reports a
    failure that is about the preview rather than about the subject.

    The boundary, and nothing wider:

    * Only :func:`mkdtemp` creates -- one brand-new directory the process owns,
      never a path the caller names.
    * Only :func:`rmtree` removes, and only a path this block's own
      :func:`mkdtemp` returned (or something inside one).  Every other
      filesystem operation keeps its normal mode behavior, so a write the
      observation aims INTO the scratch directory is still recorded.
    * Nothing here reaches project state, so the would-do log stays a list of
      genuinely planned mutations instead of carrying a ``mkdir``/``remove``
      pair for a directory no apply would ever create.
    """
    token = _OBSERVE_SCRATCH.set(set())
    try:
        yield
    finally:
        _OBSERVE_SCRATCH.reset(token)


def observe_scratch_owns(path) -> bool:
    """True when *path* is scratch the running observation itself created."""
    tracked = _OBSERVE_SCRATCH.get()
    if not tracked:
        return False
    real = os.path.realpath(_p(path))
    return any(
        real == owned or real.startswith(owned.rstrip(os.sep) + os.sep)
        for owned in tracked
    )


def mkdtemp(*, prefix=None, suffix=None, dir=None):
    """Create a temporary directory and return its path.

    Live mode creates it.  Preview mode creates NOTHING: the directory is
    recorded as a ``mkdir`` and the synthetic path comes back, so the writes
    and runs the caller aims at it are recorded against it too and the
    matching ``rmtree`` is recorded rather than performed.  Calling
    ``tempfile.mkdtemp`` directly could not do that -- it creates its
    directory in every mode, which is how ``claim-name --dry-run`` used to
    leave a real staging directory behind on every preview.

    Inside :func:`observe_scratch_dirs` the directory is real in every mode
    and is tracked, so the block's own :func:`rmtree` can remove it.
    """
    tracked = _OBSERVE_SCRATCH.get()
    if tracked is not None:
        path = _direct.mkdtemp(prefix=prefix, suffix=suffix, dir=dir)
        tracked.add(os.path.realpath(path))
        return path
    h = _handle()
    if h is None:
        return _direct.mkdtemp(prefix=prefix, suffix=suffix, dir=dir)
    path = _preview_temp_path(prefix, suffix, dir)
    h.mkdir(path)
    return path


def temp_file(content="", *, prefix=None, suffix=None, dir=None, encoding="utf-8"):
    """Create a temporary file holding *content* and return its path.

    The caller owns the result and deletes it when done (``delete=False``
    semantics).  Preview mode creates nothing and records the write.
    """
    h = _handle()
    if h is None:
        return _direct.temp_file(
            content, prefix=prefix, suffix=suffix, dir=dir, encoding=encoding
        )
    path = _preview_temp_path(prefix, suffix, dir)
    h.write(path, content)
    return path


@contextmanager
def observe_scratch_files(items, *, dir=None):
    """Materialize scratch files that exist ONLY as operands of an observe.

    *items* is a sequence of ``(content, suffix)`` pairs; the paths are
    yielded in the same order and deleted when the block exits.

    Real in every mode, like the advisory lock and for the same reason: the
    consumer is an allowlisted observe (``git merge-file -p``), which really
    executes under --dry-run and would read nothing but absent paths from a
    recorded stand-in -- reporting a merge conflict that does not exist.  A
    preview may not fabricate its own inputs.  These files are scratch the
    block owns end to end: created here, deleted here, never named in the
    would-do log because nothing about them survives the call.
    """
    paths = []
    try:
        for content, suffix in items:
            paths.append(_direct.temp_file(content, suffix=suffix, dir=dir))
        yield paths
    finally:
        for path in paths:
            try:
                _direct.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Filesystem effects -- directories, moves, deletions, modes
# ---------------------------------------------------------------------------


def makedirs(path, *, exist_ok=False):
    """Create *path* and any missing parents.

    The default mirrors ``os.makedirs`` exactly (an existing *path* raises)
    so translating a call site never changes its behavior.
    """
    h = _handle()
    if h is None:
        _direct.makedirs(path, exist_ok=exist_ok)
        return
    h.mkdir(_p(path))


def mkdir(path):
    """Create a single directory *path* (parents must already exist)."""
    h = _handle()
    if h is None:
        _direct.mkdir(path)
        return
    h.mkdir(_p(path))


def rename(src, dst):
    """Rename *src* to *dst*, failing if *dst* exists (POSIX: overwrites)."""
    h = _handle()
    if h is None:
        _direct.rename(src, dst)
        return
    h.rename(_p(src), _p(dst))


def replace(src, dst):
    """Atomically move *src* onto *dst*, overwriting *dst* if it exists."""
    h = _handle()
    if h is None:
        _direct.replace(src, dst)
        return
    h.rename(_p(src), _p(dst))


def remove(path, *, missing_ok=False):
    """Delete the file at *path*."""
    h = _handle()
    if h is None:
        _direct.remove(path, missing_ok=missing_ok)
        return
    h.remove(_p(path))


def rmdir(path):
    """Remove the empty directory at *path*."""
    h = _handle()
    if h is None:
        _direct.rmdir(path)
        return
    h.remove(_p(path))


def removedirs(path):
    """Remove *path* and then each now-empty parent directory."""
    h = _handle()
    if h is None:
        _direct.removedirs(path)
        return
    h.remove(_p(path))


def rmtree(path, *, ignore_errors=False):
    """Recursively delete the directory tree at *path*.

    Real in every mode when *path* is scratch the running observation created
    (see :func:`observe_scratch_dirs`); recorded like any other deletion
    otherwise.
    """
    if observe_scratch_owns(path):
        _direct.rmtree(path, ignore_errors=ignore_errors)
        return
    h = _handle()
    if h is None:
        _direct.rmtree(path, ignore_errors=ignore_errors)
        return
    h.remove(_p(path))


def chmod(path, mode):
    """Set the permission bits of *path*."""
    h = _handle()
    if h is None:
        _direct.chmod(path, mode)
        return
    h.chmod(_p(path), mode)


def copy_file(src, dst):
    """Copy *src* to *dst*, preserving metadata (``shutil.copy2``)."""
    h = _handle()
    if h is None:
        return _direct.copy_file(src, dst)
    # The contract has no copy: reading the source is not an effect, writing
    # the destination is the one that gets recorded.
    with open(src, "rb") as f:
        h.write(_p(dst), f.read())
    return dst


def copytree(src, dst, *, dirs_exist_ok=False, ignore=None, symlinks=False):
    """Recursively copy the directory tree *src* to *dst*."""
    h = _handle()
    if h is None:
        return _direct.copytree(
            src, dst, dirs_exist_ok=dirs_exist_ok, ignore=ignore, symlinks=symlinks
        )
    # One mkdir plus one write per file, so the preview names every path the
    # copy would create rather than a single opaque "copy tree" line.
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        skip = ignore(dirpath, dirnames + filenames) if ignore else ()
        dirnames[:] = [d for d in dirnames if d not in skip]
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        h.mkdir(target_dir)
        for name in filenames:
            if name in skip:
                continue
            with open(os.path.join(dirpath, name), "rb") as f:
                h.write(os.path.join(target_dir, name), f.read())
    return dst


# ---------------------------------------------------------------------------
# Process infrastructure -- executed in EVERY mode
#
# The advisory lock in :mod:`rlsbl.lock` is not a previewable effect: it is the
# process-level mutual exclusion that keeps two concurrent rlsbl runs from
# corrupting each other's state, and a preview needs it exactly as much as a
# live run does -- two previews racing on the same project read the same
# half-written files a live race would.  Mechanically it cannot be recorded
# either: ``fcntl.flock`` needs a real file descriptor and ``release_lock``
# reads ``.name`` off the handle, so routing the lock through the recording
# seam crashed every preview that took it (``io.UnsupportedOperation:
# fileno``).
#
# The lock file is scratch owned by the running process -- created on acquire,
# deleted on release, never part of the project state a preview reports on --
# so executing these in preview mode records nothing and leaves nothing
# behind.  It is one of the module's filesystem exceptions; the others serve
# an allowlisted observe (:func:`observe_scratch_files` for the files it reads,
# :func:`observe_scratch_dirs` for the directory it writes into).  The module
# docstring lists them all, and like them, this one is declared by name rather
# than inferred: nothing here ever tries the handle first and falls back.
# ---------------------------------------------------------------------------


def lock_makedirs(path):
    """Create the advisory lock's containing directory (real in every mode)."""
    _direct.makedirs(path, exist_ok=True)


def lock_open(path):
    """Open the advisory lock file, returning a REAL file object in every mode.

    The caller flocks the returned object's descriptor, so a recorded stand-in
    would be useless: see the section comment above.
    """
    return _direct.open_write(path, "w")


def lock_remove(path):
    """Delete the advisory lock file (real in every mode).

    ``FileNotFoundError`` propagates, which is the caller's "already gone"
    signal.
    """
    _direct.remove(path)


def lock_rmdir(path):
    """Remove the advisory lock's directory when empty (real in every mode).

    ``OSError`` propagates when the directory is not empty, which is the
    caller's "somebody else's files live here" signal.
    """
    _direct.rmdir(path)

