"""Shared pytest fixtures for the rlsbl test suite."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Three-layer test sandbox
#
# Layer 1 (this module, always-on): an env-poisoning floor installed in
# ``pytest_configure`` so it binds before any test body or fixture runs. It
# neutralizes every ambient credential / network / global-config vector so a
# test can never reach the real developer identity, SSH agent, GitHub token,
# or user/global git config -- see ``_install_env_poisoning_floor``.
#
# Layer 2 (scripts/test.sh): a bwrap sandbox that runs the FULL suite with the
# real repo bound read-only, a writable ephemeral copy as cwd, private tmpfs
# TMPDIR, and no network. It exports ``RLSBL_TEST_SANDBOX=1``. A bare-pytest
# refusal (``pytest_collection_modifyitems`` below) blocks full-suite runs that
# are NOT inside the sandbox, while keeping small targeted runs bare-runnable.
#
# Layer 3 (CI): the CI workflow runs the suite job inside the same bwrap
# sandbox via scripts/test.sh.
# ---------------------------------------------------------------------------

# Session temp dir holding the Layer-1 throwaway HOME / git config. Created in
# pytest_configure, removed in pytest_unconfigure. One per process (each xdist
# worker installs its own, fully isolated).
_SESSION_ENV_DIR = None

# Layer-2 bare-pytest refusal threshold. A run collecting MORE than this many
# tests is treated as a full-ish run and must go through scripts/test.sh (which
# sets RLSBL_TEST_SANDBOX=1). Small targeted runs (a single file, a -k slice)
# stay bare-runnable for fast inner-loop iteration -- the always-on Layer-1
# floor plus the push/chdir guards still protect those.
_SANDBOX_FULL_RUN_THRESHOLD = 50


def _install_env_poisoning_floor():
    """Layer 1: install the always-on env-poisoning floor (idempotent).

    Runs once per process from ``pytest_configure`` (including each xdist
    worker), before any test body or fixture executes. ``os.environ`` is
    mutated directly rather than via ``monkeypatch`` because the floor is
    session-wide and must outlive every individual test.
    """
    global _SESSION_ENV_DIR
    if _SESSION_ENV_DIR is not None:
        return

    real_home = os.environ.get("HOME", "")

    # Preserve the Go toolchain caches BEFORE repointing HOME. The module
    # cache, build cache, and GOPATH all default to locations under the real
    # HOME; a throwaway HOME would send Go into a cold rebuild and the
    # ``safegit_bin`` fixture (go install of the pinned safegit) would refetch
    # every dependency. Pin them explicitly at the real persistent locations.
    gopath = os.environ.get("GOPATH") or (f"{real_home}/go" if real_home else None)
    if gopath:
        os.environ["GOPATH"] = gopath
        os.environ.setdefault("GOMODCACHE", f"{gopath}/pkg/mod")
    if real_home:
        os.environ.setdefault("GOCACHE", f"{real_home}/.cache/go-build")

    # Preserve the Python user-site base BEFORE repointing HOME. Tools like
    # git-filter-repo install their importable module into
    # ``$HOME/.local/lib/pythonX/site-packages`` (user site, derived from HOME).
    # The extract / absorb / commit-map tests shell out to ``git filter-repo``
    # (a system-python script that does ``import git_filter_repo``); a throwaway
    # HOME would hide that module and the tool would die with ModuleNotFoundError.
    # Pinning PYTHONUSERBASE keeps the real user site reachable regardless of
    # HOME. This is toolchain preservation (like the Go caches above), not a
    # credential vector -- user site holds packages, not secrets. The rlsbl test
    # venv disables user site, so this cannot leak packages into the suite's own
    # imports; it only matters for the system-python subprocesses git spawns.
    if real_home:
        os.environ.setdefault("PYTHONUSERBASE", f"{real_home}/.local")

    session_dir = Path(tempfile.mkdtemp(prefix="rlsbl-test-env-"))
    _SESSION_ENV_DIR = session_dir

    # Throwaway HOME + XDG dirs so nothing reads (or writes) real dotfiles.
    home = session_dir / "home"
    home.mkdir()
    (session_dir / "xdg-config").mkdir()
    (session_dir / "xdg-data").mkdir()
    os.environ["HOME"] = str(home)
    os.environ["XDG_CONFIG_HOME"] = str(session_dir / "xdg-config")
    os.environ["XDG_DATA_HOME"] = str(session_dir / "xdg-data")

    # Throwaway git global + system config. Carries protocol.ssh.allow=never
    # and a session commit identity so real-git fixtures that skip per-repo
    # identity still commit.
    #
    # We deliberately do NOT set core.hooksPath here. core.hooksPath overrides
    # REPO-LOCAL hooks too, which would silently disable the suite's real
    # pre-push-hook tests (test_hook_v5_e2e, test_pre_push_check, ...). A global
    # config cannot inject hooks on its own, so simply having no hooks entry in
    # the throwaway global config is sufficient to keep real user/global hooks
    # from firing -- and omitting hooksPath is required to keep the repo-local
    # hook tests working.
    gitconfig = session_dir / "gitconfig"
    gitconfig.write_text(
        "[user]\n"
        "\tname = rlsbl-test\n"
        "\temail = rlsbl-test@example.invalid\n"
        '[protocol "ssh"]\n'
        "\tallow = never\n"
        "[init]\n"
        "\tdefaultBranch = main\n"
    )
    os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    os.environ["GIT_CONFIG_SYSTEM"] = str(gitconfig)

    # Transport lockdown: only the local ``file`` protocol may be used by git;
    # ssh / proxy invocations hard-fail; no interactive or credential prompt
    # can ever block a test or leak a real credential.
    os.environ["GIT_ALLOW_PROTOCOL"] = "file"
    os.environ["GIT_SSH_COMMAND"] = "/bin/false"
    os.environ["GIT_PROXY_COMMAND"] = "/bin/false"
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["GIT_ASKPASS"] = "/bin/false"

    # Kill ambient credentials outright: no SSH agent socket, no GitHub token.
    # Per-test ``mock_gh`` fixtures re-set a fake GITHUB_TOKEN via monkeypatch
    # for the tests that need one.
    os.environ.pop("SSH_AUTH_SOCK", None)
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GH_TOKEN", None)


def pytest_unconfigure(config):
    """Tear down the Layer-1 throwaway env directory for this process."""
    global _SESSION_ENV_DIR
    if _SESSION_ENV_DIR is not None:
        shutil.rmtree(_SESSION_ENV_DIR, ignore_errors=True)
        _SESSION_ENV_DIR = None


def _enforce_sandbox_threshold(count):
    """Raise ``UsageError`` if a bare run of ``count`` tests is too large.

    No-op when inside the sandbox (``RLSBL_TEST_SANDBOX=1``) or when the run is
    a small targeted slice (``count <= _SANDBOX_FULL_RUN_THRESHOLD``).
    """
    if os.environ.get("RLSBL_TEST_SANDBOX") == "1":
        return
    if count <= _SANDBOX_FULL_RUN_THRESHOLD:
        return
    raise pytest.UsageError(
        f"Refusing to run {count} tests bare (> {_SANDBOX_FULL_RUN_THRESHOLD}). "
        "A full-ish suite run must go through the bwrap sandbox:\n\n"
        "    scripts/test.sh\n\n"
        "The sandbox binds the real repo read-only, runs in a writable throwaway "
        "copy on a private tmpfs, and has no network -- so a stray real git push, "
        "an unanchored commit into the dev repo, or a live API call is physically "
        "impossible. Small targeted runs stay allowed bare for iteration speed "
        f"(<= {_SANDBOX_FULL_RUN_THRESHOLD} tests: a single file or a -k slice). "
        "To run the full suite, use scripts/test.sh."
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Layer 2: refuse a bare full-ish run outside the bwrap sandbox.

    Small targeted runs stay allowed bare so the inner development loop (run one
    file, a ``-k`` slice) is fast; the dangerous full-suite class -- real git,
    real subprocesses, thousands of fixtures -- is sandbox-only.

    ``trylast`` so this runs AFTER pytest's own ``-k`` / ``-m`` deselection,
    which mutates ``items`` in place -- the count then reflects the SELECTED
    tests, so a ``-k`` slice of a big file stays under the threshold and runs
    bare instead of being refused on the pre-deselection total.

    Enforcement is split by execution topology:
    - Single process (no xdist): ``items`` is the full set here -- enforce.
    - xdist worker (``PYTEST_XDIST_WORKER`` set): a shard; defer to the
      controller so the error surfaces once, not once per worker.
    - xdist controller (``numprocesses`` set): ``items`` is empty here because
      workers do the collecting -- defer to
      ``pytest_xdist_node_collection_finished`` which sees the real count.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    if getattr(config.option, "numprocesses", None):
        return
    _enforce_sandbox_threshold(len(items))


def pytest_xdist_node_collection_finished(node, ids):
    """Layer 2 (xdist controller): enforce the threshold once collection lands.

    The controller does not collect items itself; each worker reports its
    collected ``ids`` here. All workers collect the same full set, so the first
    report carries the true test count -- enforce on it.
    """
    _enforce_sandbox_threshold(len(ids))


# ---------------------------------------------------------------------------
# Structural push-guard: make a real ``git push`` to a NON-LOCAL remote
# impossible from the test suite.
#
# Forensics found a test that mocked the undo command's ``run``/``run_gh`` but
# NOT ``push_if_needed``/``get_current_branch``, so a full-suite run executed a
# REAL ``git push origin main`` from the real rlsbl dev repo. This guard closes
# that class of bug at the innermost real-execution boundary (``subprocess.Popen``,
# which ``subprocess.run`` funnels through). Tests that mock ``subprocess.run``
# in some namespace never reach Popen, so the guard composes with existing mock
# layering instead of fighting it. Local filesystem paths and ``file://`` URLs
# are ALLOWED -- the suite's fixtures push to local bare repos constantly.
#
# ``pytest.fail`` raises ``Failed`` (a ``BaseException`` subclass), so it slips
# past production ``except Exception`` handlers and surfaces loudly even when a
# caller would otherwise swallow the push error.
# ---------------------------------------------------------------------------

# Repo root (parent of the tests/ directory holding this conftest).
_REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config):
    """Session guard: refuse to run if the temp root is inside the repository.

    The Jul junk-commit incidents happened because a TMPDIR (or pytest
    basetemp) pointed inside the repo: fixtures created non-git directories
    there, and unanchored git commands walked UP into the real repo and
    committed junk. Fail loudly at startup rather than let that recur.
    """
    config.addinivalue_line(
        "markers",
        "repo_cwd: opt a test OUT of the autouse tmp-cwd isolation. Reserved "
        "for the irreducible CLI-wiring tests that dispatch commands through "
        "app.test() and must resolve the real rlsbl project from the process "
        "cwd (and record strictcli coverage into the App-construction repo).",
    )
    candidates = []
    basetemp = getattr(config.option, "basetemp", None)
    if basetemp:
        candidates.append(Path(basetemp))
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        candidates.append(Path(tmpdir))
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if resolved == _REPO_ROOT or _REPO_ROOT in resolved.parents:
            raise pytest.UsageError(
                f"TMPDIR/basetemp {resolved} is inside the repository "
                f"{_REPO_ROOT} -- refusing. Fixture temp dirs inside the repo "
                f"let unanchored git commands walk up into the real repo and "
                f"commit junk (the Jul junk-commit incidents). Point TMPDIR at a "
                f"location OUTSIDE the repository and re-run."
            )

    # Layer 1: bind the env-poisoning floor now that TMPDIR is proven safe.
    _install_env_poisoning_floor()


@pytest.fixture(autouse=True)
def _chdir_into_tmp(request, tmp_path, monkeypatch):
    """Autouse: never let a test run with the process cwd at the real repo.

    A test whose process cwd is the real repo can make every unanchored git
    command (status/commit/push, changelog regeneration, release-file
    scaffolding) operate on the dev repo. Chdir-ing each test into its own
    ``tmp_path`` makes implicit repo-cwd reliance a visible failure instead of
    silent real-repo pollution. Fixtures that chdir into their own tmp_path
    (the same ``tmp_path`` object) compose cleanly -- they land in the same
    directory. Tests that genuinely need the repo cwd must anchor explicitly, or
    opt out with ``@pytest.mark.repo_cwd`` (the CLI-wiring tests).
    """
    if request.node.get_closest_marker("repo_cwd") is not None:
        yield
        return
    monkeypatch.chdir(tmp_path)
    yield


def _remote_is_local(url: str | None) -> bool:
    """Classify a git remote URL/path as local (allowed) or non-local (blocked).

    Local: ``file://`` URLs and bare filesystem paths (absolute or relative).
    Non-local: any URL with a non-``file`` scheme (https://, ssh://, git://)
    and SCP-like syntax (``git@host:owner/repo``). ``None``/empty is treated as
    non-local (cannot prove locality -> block loudly).
    """
    if not url:
        return False
    if url.startswith("file://"):
        return True
    # Explicit scheme (scheme://...): only file:// is local.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        return url.startswith("file://")
    # SCP-like: [user@]host:path -- non-local. A bare Windows drive letter is
    # irrelevant on the Linux CI/dev hosts, so any ``host:`` form is remote.
    if re.match(r"^[^/\\]+@[^/\\]+:", url) or re.match(r"^[A-Za-z0-9.\-]+:", url):
        return False
    # Otherwise a filesystem path (absolute or relative) -- local.
    return True


def _extract_push_remote(cmd) -> str | None:
    """Return the remote argument of a ``git push`` command list, or None."""
    tokens = [str(t) for t in cmd]
    try:
        push_idx = tokens.index("push")
    except ValueError:
        return None
    for tok in tokens[push_idx + 1:]:
        if tok.startswith("-"):
            continue
        return tok
    return None


@pytest.fixture(autouse=True)
def _guard_nonlocal_push():
    """Autouse guard: block any real ``git push`` to a non-local remote."""
    real_popen = subprocess.Popen

    def _resolve_remote_url(remote: str, cwd) -> str | None:
        try:
            proc = real_popen(
                ["git", "remote", "get-url", remote],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
            )
            out, _ = proc.communicate(timeout=10)
            if proc.returncode == 0:
                return out.strip()
        except Exception:
            return None
        return None

    def guarded_popen(args, *a, **kw):
        cmd = args
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and \
                os.path.basename(str(cmd[0])) == "git" and "push" in [str(c) for c in cmd]:
            remote = _extract_push_remote(cmd)
            cwd = kw.get("cwd")
            if len(a) >= 9:  # positional cwd is the 9th arg of Popen.__init__
                cwd = a[8]
            # A bare remote NAME (no scheme, no ':' , no '/') must be resolved
            # to its URL; anything else is used as-is.
            if remote and not re.search(r"[:/\\]", remote):
                url = _resolve_remote_url(remote, cwd)
            else:
                url = remote
            if not _remote_is_local(url):
                pytest.fail(
                    "BLOCKED: real 'git push' to a non-local remote from the "
                    f"test suite. cmd={list(cmd)!r} remote={remote!r} "
                    f"resolved_url={url!r} cwd={cwd!r}. A test is exercising a "
                    "push path without mocking it; mock push_if_needed / the "
                    "push subprocess, or point origin at a local bare repo.",
                    pytrace=False,
                )
        return real_popen(args, *a, **kw)

    with patch("subprocess.Popen", side_effect=guarded_popen):
        yield

from githarness import git as _git
from rlsbl.context import ProjectContext
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    Releasable,
    save_workspace,
    write_releasable_version,
    get_releasable_changes_dir,
    get_releasable_dir,
)


def make_ctx(project_root, config=None):
    """Create a minimal ProjectContext for tests.

    If config is not provided, reads .rlsbl/config.json from project_root
    (returning {} if the file doesn't exist).
    """
    if isinstance(project_root, str):
        from pathlib import Path
        project_root = Path(project_root)
    if config is None:
        config_path = project_root / ".rlsbl" / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())
        else:
            config = {}
    return ProjectContext(project_root=project_root, workspace_root=None, config=config)


def capture_all_checks():
    """Register all rlsbl checks on a mock app and return a dict of {name: fn(ctx)}.

    Each captured function wraps the raw (ctx, reporter) impl to create the
    appropriate reporter, matching what strictcli's _CheckDef.impl does.
    """
    from strictcli import ErrorReporter, WarnReporter
    from rlsbl.checks import register_checks

    captured = {}

    def _make_registrar(reporter_cls):
        def registrar(name):
            def decorator(fn):
                def run(ctx):
                    return fn(ctx, reporter_cls())
                captured[name] = run
                return fn
            return decorator
        return registrar

    error_registrar = _make_registrar(ErrorReporter)
    warn_registrar = _make_registrar(WarnReporter)

    class MockApp:
        _checks_enabled = True

        def set_scope_adapter(self, adapter):
            pass

        def error_check(self, name):
            return error_registrar(name)

        def warn_check(self, name):
            return warn_registrar(name)

    register_checks(MockApp())
    return captured


# ---------------------------------------------------------------------------
# Utility functions (imported explicitly by test modules)
# ---------------------------------------------------------------------------


def run_git(repo, *args):
    """Run a git command in the given repo directory.

    Thin wrapper over githarness.git; returns None (callers use it purely
    for side effects) to preserve the historical signature.
    """
    _git(repo, *args)


def git_head(repo):
    """Get HEAD hash."""
    return _git(repo, "rev-parse", "HEAD")


def make_commit(repo, filename="file.txt", message="change"):
    """Make a commit and return its hash."""
    filepath = repo / filename
    filepath.write_text(f"content-{time.monotonic_ns()}\n")
    run_git(repo, "add", filename)
    run_git(repo, "commit", "-q", "-m", message)
    return git_head(repo)


# ---------------------------------------------------------------------------
# Pinned safegit binary for integration tests (real-binary harness)
# ---------------------------------------------------------------------------


# Directory (gitignored, repo-relative) where the sandbox pre-warm stages a
# safegit binary it built outside the sandbox. Kept in the repo tree because it
# is the only writable-then-readable channel into the bwrap sandbox: the
# pre-warm runs before the throwaway working copy is rsync'd, so whatever it
# leaves here rides along into the sandbox, which has neither network nor a
# view of a sibling safegit checkout.
SAFEGIT_STAGE_DIR = ".rlsbl-test-tools"

# Override for the local safegit source checkout used when the pinned version
# is not published yet.
SAFEGIT_SRC_ENV = "RLSBL_SAFEGIT_SRC"


def _safegit_local_source():
    """Return a local safegit source checkout, or None if none is reachable.

    Checked in order: an explicit RLSBL_SAFEGIT_SRC, a sibling of this repo,
    and ~/Projects/safegit. A candidate counts only if it really is the
    safegit module (its go.mod declares the module path).
    """
    candidates = []
    override = os.environ.get(SAFEGIT_SRC_ENV)
    if override:
        candidates.append(Path(override))
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root.parent / "safegit")
    candidates.append(Path.home() / "Projects" / "safegit")

    for candidate in candidates:
        gomod = candidate / "go.mod"
        try:
            if gomod.is_file() and "module github.com/smm-h/safegit" in gomod.read_text():
                return candidate
        except OSError:
            continue
    return None


def _acquire_safegit(pin, gobin, binary):
    """Put a safegit binary at ``binary`` at exactly version ``pin``.

    Returns None on success, or a human-readable reason string explaining
    precisely why the pinned version could not be obtained. Both acquisition
    routes announce themselves, so a run is never ambiguous about which
    safegit it exercised.
    """
    gobin.mkdir(parents=True, exist_ok=True)

    # 1. The published module, ALWAYS tried first. Reproducible everywhere --
    #    and offline inside the sandbox, where GOPROXY points at the
    #    pre-warmed module cache. Trying it before the staged binary is what
    #    keeps a locally-built stand-in from shadowing the real release the
    #    day the floor is published.
    env = {**os.environ, "GOBIN": str(gobin)}
    proxy = subprocess.run(
        ["go", "install", f"github.com/smm-h/safegit@{pin}"],
        env=env, timeout=600, capture_output=True, text=True,
    )
    if proxy.returncode == 0:
        return None

    # 2. A binary the sandbox pre-warm staged in the repo tree. Inside the
    #    sandbox this is the only reachable stand-in for an unpublished pin:
    #    no network, and no view of a sibling safegit checkout.
    staged = Path(__file__).resolve().parent.parent / SAFEGIT_STAGE_DIR / f"safegit-{pin}"
    if staged.is_file():
        shutil.copy2(str(staged), str(binary))
        os.chmod(str(binary), 0o755)
        print(
            f"[safegit_bin] {pin} is not published; using the pre-warm-staged "
            f"build at {staged}"
        )
        return None

    # 3. An unpublished pin outside the sandbox: build it from a local
    #    checkout, stamping the pinned version so the binary reports what the
    #    floor demands.
    source = _safegit_local_source()
    if source is None:
        return (
            f"safegit {pin} is unavailable. The module proxy cannot resolve "
            f"github.com/smm-h/safegit@{pin} -- rlsbl's SAFEGIT_MIN_VERSION "
            f"floor is not published yet -- and no local safegit source "
            f"checkout is reachable from here (set {SAFEGIT_SRC_ENV}=/path/to/"
            f"safegit, or run outside the sandbox where a sibling checkout is "
            f"visible). Real-binary safegit tests cannot run.\n"
            f"go install stderr: {proxy.stderr.strip()[-500:]}"
        )

    build = subprocess.run(
        ["go", "build", "-o", str(binary),
         "-ldflags", f"-X main.version={pin}", "."],
        cwd=str(source), env=env, timeout=600, capture_output=True, text=True,
    )
    if build.returncode != 0:
        return (
            f"safegit {pin} is unavailable. The module proxy cannot resolve "
            f"github.com/smm-h/safegit@{pin} (the floor is not published yet), "
            f"and building it from the local checkout at {source} failed.\n"
            f"go build stderr: {build.stderr.strip()[-500:]}"
        )
    print(
        f"[safegit_bin] {pin} is not published; built it from the local "
        f"checkout at {source} with -X main.version={pin}"
    )
    return None


@pytest.fixture(scope="session")
def safegit_bin(tmp_path_factory):
    """Provide the pinned safegit binary and return its path.

    The pin is derived from SAFEGIT_MIN_VERSION -- the exact version the scrub
    flow declares as its minimum. It is obtained from a pre-warm-staged copy,
    from the module proxy, or (when the floor is declared but not yet
    published) by building a local safegit checkout with the pinned version
    stamped in. See ``_acquire_safegit``.

    When none of those work the affected tests SKIP with a reason naming the
    unpublished floor -- they never silently pass against some other safegit.

    Cross-worker/session safety: the build directory lives in the shared
    pytest temp root (one level above the per-session basetemp, which is also
    shared by xdist workers) and is guarded by an O_EXCL lock file plus a
    .done / .unavailable marker, so concurrent workers build exactly once.
    """
    from rlsbl.commands.release_scrub import SAFEGIT_MIN_VERSION

    pin = "v" + ".".join(str(p) for p in SAFEGIT_MIN_VERSION)
    shared_root = tmp_path_factory.getbasetemp().parent
    gobin = shared_root / f"safegit-{pin}"
    binary = gobin / "safegit"
    done_marker = gobin / ".done"
    unavailable_marker = gobin / ".unavailable"
    lock_path = shared_root / f"safegit-{pin}.lock"

    deadline = time.monotonic() + 600
    while not (done_marker.exists() or unavailable_marker.exists()):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"timed out waiting for another worker to build safegit "
                    f"{pin} (stale lock? {lock_path})"
                )
            time.sleep(1)
            continue
        try:
            if not (done_marker.exists() or unavailable_marker.exists()):
                reason = _acquire_safegit(pin, gobin, binary)
                if reason is None:
                    done_marker.touch()
                else:
                    unavailable_marker.write_text(reason, encoding="utf-8")
        finally:
            os.close(fd)
            os.unlink(str(lock_path))

    if unavailable_marker.exists():
        pytest.skip(unavailable_marker.read_text(encoding="utf-8"))

    assert binary.exists(), f"safegit binary missing after build: {binary}"
    return binary


def make_workspace(root, projects):
    """Create a .rlsbl-monorepo/workspace.toml with the given project list."""
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    lines = []
    for proj in projects:
        lines.append("[[projects]]")
        lines.append(f'path = "{proj["path"]}"')
        lines.append(f'name = "{proj["name"]}"')
        if "watch" in proj:
            watch_items = ", ".join(f'"{w}"' for w in proj["watch"])
            lines.append(f"watch = [{watch_items}]")
        if proj.get("library"):
            lines.append("library = true")
        if proj.get("dev_node"):
            lines.append("dev_node = true")
        if proj.get("dev_only"):
            lines.append("dev_only = true")
        if "releasable" in proj:
            rel = proj["releasable"]
            if rel is False:
                lines.append("releasable = false")
            elif isinstance(rel, str):
                lines.append(f'releasable = "{rel}"')
        lines.append("")
    (ws_dir / WORKSPACE_FILE).write_text("\n".join(lines))


class FakeResponse:
    """Fake HTTP response for mocking urllib.request.urlopen.

    Supports context-manager protocol, .read(), and .getheader().
    ``data`` can be bytes (used as-is) or a dict (auto-JSON-encoded).
    """

    def __init__(self, data, status=200, headers=None):
        if isinstance(data, bytes):
            self._data = data
        else:
            # Assume dict/list — JSON-encode it
            self._data = json.dumps(data).encode()
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._data

    def getheader(self, name):
        # Case-insensitive header lookup
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture
def bypass_upfront_validation():
    """Patch batch release upfront validation functions so tests that test
    other behavior are not blocked by gh/clean-tree/branch checks."""
    with patch("rlsbl.commands.monorepo.batch_release.validate_gh_cli"), \
         patch("rlsbl.commands.monorepo.batch_release.validate_clean_tree", return_value=set()), \
         patch("rlsbl.commands.monorepo.batch_release.validate_branch_and_remote", return_value="main"):
        yield


@pytest.fixture(autouse=True)
def _mock_saferm():
    """Mock saferm and selfdoc subprocess calls across rlsbl modules.

    Intercepts subprocess.run calls where the first arg is 'saferm'
    (performs actual file deletion via os.unlink) or 'selfdoc' (no-op),
    and passes through all other subprocess calls to the real subprocess.run.

    Applied automatically to all tests so that:
    - saferm-dependent code paths work without saferm being installed
    - selfdoc subprocess calls don't fail when tests blanket-patch
      os.path.exists to True (which makes _run_root_selfdoc think
      selfdoc.json exists at a fake workspace root like /ws)
    """
    import subprocess as real_subprocess
    original_run = real_subprocess.run

    def _mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd:
            if cmd[0] == "saferm":
                target_file = cmd[-1]
                if os.path.isdir(target_file):
                    # saferm delete -r removes directories; mirror that here.
                    shutil.rmtree(target_file)
                elif os.path.exists(target_file):
                    os.unlink(target_file)
                return real_subprocess.CompletedProcess(args=cmd, returncode=0)
            if cmd[0] == "selfdoc":
                return real_subprocess.CompletedProcess(args=cmd, returncode=0)
        return original_run(cmd, *args, **kwargs)

    with patch("rlsbl.commands.init_cmd.subprocess.run", side_effect=_mock_run), \
         patch("rlsbl.commands.monorepo.sync.subprocess.run", side_effect=_mock_run), \
         patch("rlsbl.commands.release.subprocess.run", side_effect=_mock_run):
        yield


@pytest.fixture(autouse=True)
def _mock_remote_tag_commit():
    """Neutralize the pre-mutation remote tag collision probe by default.

    ``compute_release_version`` runs a live ``git ls-remote`` against origin to
    detect a remote tag colliding with the computed release tag (a real
    network call). Left unmocked, every release-flow and compute-version test
    would hit the actual origin of whatever repo the test process is in
    (typically the rlsbl dev repo) -- slow, flaky, and network-dependent.

    Default to ABSENT (no collision) so tests proceed offline. Tests that
    specifically exercise the collision / inconclusive paths patch
    ``rlsbl.commands.release.remote_tag_commit`` themselves (their inner patch
    nests over this one and wins for the test's duration).
    """
    from rlsbl.utils import RemoteTagResult, RemoteTagState

    with patch(
        "rlsbl.commands.release.remote_tag_commit",
        return_value=RemoteTagResult(RemoteTagState.ABSENT),
    ):
        yield


@pytest.fixture(autouse=True)
def _default_tag_push_plan():
    """Neutralize the commit-aware tag-push probe by default.

    The release PUSHED step calls ``resolve_tag_push_plan`` (which runs a live
    ``git ls-remote origin`` per tag) to decide whether the tag push is needed
    and to reject a divergent remote tag. Most release-flow tests mock the push
    and run in repos with no reachable ``origin``, so an unmocked probe would
    hard-error with "origin does not appear to be a git repository" -- exactly
    the state the old ``tag_exists_on_remote`` skip swallowed.

    Default to True ("push proceeds"), matching the pre-existing behavior these
    tests relied on. Tests that specifically exercise the plan (skip vs push vs
    divergence) patch ``rlsbl.commands.release.resolve_tag_push_plan`` with the
    value under test; their inner patch nests over this one. The helper's own
    unit tests call ``rlsbl.utils.resolve_tag_push_plan`` directly and are
    unaffected by this release-boundary patch.
    """
    with patch(
        "rlsbl.commands.release.resolve_tag_push_plan", return_value=True,
    ):
        yield


@pytest.fixture(autouse=True)
def _default_push_timeout(monkeypatch):
    """Pin the push timeout for all tests via the env var.

    Production code defaults to 120 when neither the env var nor the
    push_timeout config field is set, but pinning the env var keeps
    release-flow tests deterministic regardless of the host environment.
    The env var has highest precedence, so individual tests can still
    override via monkeypatch or by setting the env var themselves.
    """
    monkeypatch.setenv("RLSBL_PUSH_TIMEOUT", "120")


@pytest.fixture
def tmp_project(tmp_path, monkeypatch):
    """Create a temporary directory and chdir into it.

    Returns the Path object for the temp directory.
    Automatically restores the original cwd on teardown via monkeypatch.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def mock_git_repo(tmp_project):
    """Create a minimal git repo with an initial commit in a temp directory.

    Builds on tmp_project (already chdir'd into tmp_path).
    Returns the Path object for the repo root.
    """
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(tmp_project),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.local"],
        cwd=str(tmp_project),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_project),
        check=True,
    )
    # Create an initial commit so HEAD exists
    readme = tmp_project / "README.md"
    readme.write_text("# test\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(tmp_project),
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=str(tmp_project),
        check=True,
    )
    return tmp_project


@pytest.fixture
def project_context(mock_git_repo):
    from rlsbl.context import create_context
    return create_context(mock_git_repo)


@pytest.fixture
def mock_gh(monkeypatch):
    """Patch common gh/GitHub-related calls to prevent real API access.

    Patches:
    - GITHUB_TOKEN env var set to a fake value
    - urllib.request.urlopen returns FakeResponse with empty JSON object
    - subprocess.run for 'gh' commands returns a no-op CompletedProcess

    Returns a dict with references to the patches for further customization:
        {"urlopen_calls": list, "subprocess_calls": list}
    """
    monkeypatch.setenv("GITHUB_TOKEN", "fake-test-token")

    urlopen_calls = []
    subprocess_calls = []

    def fake_urlopen(req, timeout=None):
        urlopen_calls.append(req)
        return FakeResponse({})

    def fake_subprocess_run(cmd, *args, **kwargs):
        subprocess_calls.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    return {"urlopen_calls": urlopen_calls, "subprocess_calls": subprocess_calls}


@pytest.fixture
def monorepo_fixture(tmp_path, monkeypatch):
    """Create a full monorepo workspace with two subprojects, tags, and changelogs.

    Yields a SimpleNamespace with:
        root        -- Path to the repo root
        projects    -- list of project dicts
        python_dir  -- absolute Path to the python subproject
        go_dir      -- absolute Path to the go subproject
    """
    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)

    # Initialize git repo
    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    # Initial commit so HEAD exists
    readme = tmp_path / "README.md"
    readme.write_text("# monorepo test\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    # Define projects
    projects = [
        {"path": "python", "name": "mypylib"},
        {"path": "go", "name": "mygolib"},
    ]

    # Create workspace.toml
    make_workspace(tmp_path, projects)

    # Create subproject directories and changelog files
    python_dir = tmp_path / "python"
    go_dir = tmp_path / "go"

    (python_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (python_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (python_dir / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
    )

    (go_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (go_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (go_dir / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["plain"]}) + "\n"
    )

    # Create minimal project files
    (python_dir / "pyproject.toml").write_text(
        '[project]\nname = "mypylib"\nversion = "0.1.0"\n'
    )
    (go_dir / "VERSION").write_text("0.1.0\n")

    # Commit all subproject files
    run_git(tmp_path, "add", WORKSPACE_DIR)
    run_git(tmp_path, "add", "python")
    run_git(tmp_path, "add", "go")
    run_git(tmp_path, "commit", "-q", "-m", "add monorepo projects")

    # Tag both subprojects
    run_git(tmp_path, "tag", "mypylib@v0.1.0")
    run_git(tmp_path, "tag", "mygolib@v0.1.0")

    # Make a post-tag change so there's an unreleased commit, then add a
    # user-facing changelog entry covering it. This satisfies the
    # changelog-user-facing check which now runs pure in dry-run mode.
    (python_dir / "post_tag.txt").write_text("post-tag change\n")
    (go_dir / "post_tag.txt").write_text("post-tag change\n")
    run_git(tmp_path, "add", "python/post_tag.txt", "go/post_tag.txt")
    run_git(tmp_path, "commit", "-q", "-m", "post-tag change")
    _post_tag_sha = git_head(tmp_path)
    _uf_entry = json.dumps({"commits": [_post_tag_sha], "user_facing": True, "description": "test", "type": "feature"}) + "\n"
    (python_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(_uf_entry)
    (go_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(_uf_entry)
    run_git(tmp_path, "add", "python/.rlsbl/changes/unreleased.jsonl")
    run_git(tmp_path, "add", "go/.rlsbl/changes/unreleased.jsonl")
    run_git(tmp_path, "commit", "-q", "-m", "add changelog entries")

    yield SimpleNamespace(
        root=tmp_path,
        projects=projects,
        python_dir=python_dir,
        go_dir=go_dir,
    )


# ---------------------------------------------------------------------------
# Default structure for multi_releasable_monorepo fixture
# ---------------------------------------------------------------------------

# Two releasables, each with 2 member packages, plus one dev_only project.
_DEFAULT_RELEASABLES = [
    Releasable(name="alpha"),
    Releasable(name="beta"),
]

_DEFAULT_PROJECTS = [
    {
        "path": "libs/alpha-core",
        "name": "alpha-core",
        "releasable": "alpha",
    },
    {
        "path": "apps/alpha-web",
        "name": "alpha-web",
        "releasable": "alpha",
    },
    {
        "path": "libs/beta-api",
        "name": "beta-api",
        "releasable": "beta",
    },
    {
        "path": "apps/beta-cli",
        "name": "beta-cli",
        "releasable": "beta",
    },
    {
        "path": "tools/devtools",
        "name": "devtools",
        "dev_only": True,
        "releasable": False,
    },
]


def _create_multi_releasable_monorepo(
    tmp_path,
    *,
    releasables=None,
    projects=None,
    releasable_configs=None,
    hook_configs=None,
    initial_version="0.1.0",
):
    """Build a multi-releasable monorepo test repo.

    Factory function that creates the full directory structure, writes
    workspace.toml with ``[[releasables]]`` and ``[[projects]]`` sections,
    sets up per-releasable state directories (version, changes, config),
    and initializes a git repo with an initial commit and version tags.

    Args:
        tmp_path: the temporary directory root.
        releasables: list of Releasable instances (defaults to alpha + beta).
        projects: list of project dicts with at least path, name, releasable
            (defaults to 2 alpha members + 2 beta members + 1 dev_only).
        releasable_configs: dict mapping releasable name to config dict,
            written to ``<releasable_dir>/config.json``.
        hook_configs: dict mapping releasable name to hook config dict,
            written to ``<releasable_dir>/hooks/`` directory files.
        initial_version: version string for all releasables (default "0.1.0").

    Returns:
        SimpleNamespace with root, releasables, projects, and per-project dirs.
    """
    from types import SimpleNamespace

    if releasables is None:
        releasables = list(_DEFAULT_RELEASABLES)
    if projects is None:
        projects = [dict(p) for p in _DEFAULT_PROJECTS]
    if releasable_configs is None:
        releasable_configs = {}
    if hook_configs is None:
        hook_configs = {}

    # Initialize git repo
    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    # Initial commit so HEAD exists
    readme = tmp_path / "README.md"
    readme.write_text("# multi-releasable monorepo test\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    # Write workspace.toml via save_workspace (handles releasables + projects)
    save_workspace(str(tmp_path), projects, releasables=releasables)

    # Create per-project directories with minimal project files
    project_dirs = {}
    for proj in projects:
        proj_dir = tmp_path / proj["path"]
        proj_dir.mkdir(parents=True, exist_ok=True)
        # Create a minimal pyproject.toml for each project
        (proj_dir / "pyproject.toml").write_text(
            f'[project]\nname = "{proj["name"]}"\nversion = "{initial_version}"\n'
        )
        # Per-project .rlsbl/config.json (minimal)
        rlsbl_dir = proj_dir / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
        )
        project_dirs[proj["name"]] = proj_dir

    # Set up per-releasable state directories
    for rel in releasables:
        # Version file
        write_releasable_version(str(tmp_path), rel.name, initial_version)

        # Changes directory with empty unreleased.jsonl
        changes_dir = get_releasable_changes_dir(str(tmp_path), rel.name)
        os.makedirs(changes_dir, exist_ok=True)
        unreleased_path = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased_path, "w") as f:
            f.write("")

        # Releasable-level config.json
        rel_dir = get_releasable_dir(str(tmp_path), rel.name)
        config = releasable_configs.get(rel.name, {})
        config_path = os.path.join(rel_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")

        # Hook config (if provided) -- write hook scripts to hooks/ dir
        if rel.name in hook_configs:
            hooks_dir = os.path.join(rel_dir, "hooks")
            os.makedirs(hooks_dir, exist_ok=True)
            for hook_name, hook_content in hook_configs[rel.name].items():
                hook_path = os.path.join(hooks_dir, hook_name)
                with open(hook_path, "w") as f:
                    f.write(hook_content)
                os.chmod(hook_path, 0o755)

    # Commit all workspace and project files
    run_git(tmp_path, "add", WORKSPACE_DIR)
    for proj in projects:
        run_git(tmp_path, "add", proj["path"])
    run_git(tmp_path, "commit", "-q", "-m", "add multi-releasable monorepo")

    # Tag each releasable at the initial version
    for rel in releasables:
        tag = rel.tag_format.format(name=rel.name, version=initial_version)
        run_git(tmp_path, "tag", tag)

    # Make a post-tag commit so there is an unreleased range
    marker = tmp_path / "marker.txt"
    marker.write_text("post-tag marker\n")
    run_git(tmp_path, "add", "marker.txt")
    run_git(tmp_path, "commit", "-q", "-m", "post-tag commit")

    return SimpleNamespace(
        root=tmp_path,
        releasables=releasables,
        projects=projects,
        project_dirs=project_dirs,
        initial_version=initial_version,
    )


@pytest.fixture
def multi_releasable_monorepo(tmp_path, monkeypatch):
    """Create a multi-releasable monorepo with default structure.

    For custom configurations, use multi_releasable_monorepo_factory instead.

    Yields a SimpleNamespace with:
        root             -- Path to the repo root
        releasables      -- list of Releasable instances
        projects         -- list of project dicts
        project_dirs     -- dict mapping project name to its Path
        initial_version  -- the initial version string
    """
    monkeypatch.chdir(tmp_path)
    ns = _create_multi_releasable_monorepo(tmp_path)
    yield ns


@pytest.fixture
def multi_releasable_monorepo_factory(tmp_path, monkeypatch):
    """Factory fixture for creating customized multi-releasable monorepos.

    Returns a callable that accepts the same keyword arguments as
    ``_create_multi_releasable_monorepo`` (releasables, projects,
    releasable_configs, hook_configs, initial_version).

    Example::

        def test_custom(multi_releasable_monorepo_factory):
            ns = multi_releasable_monorepo_factory(
                releasable_configs={"alpha": {"batch_limits": {"max_commits_per_entry": 3}}},
            )
            assert ns.root.exists()
    """
    monkeypatch.chdir(tmp_path)

    def factory(**kwargs):
        return _create_multi_releasable_monorepo(tmp_path, **kwargs)

    return factory
