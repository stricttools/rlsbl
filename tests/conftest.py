"""Shared pytest fixtures for the rlsbl test suite."""

import json
import os
import shutil
import subprocess
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Three-layer test sandbox
#
# Layer 1 (the ``stricttest`` pytest plugin, always-on): the env-poisoning
# floor, the socket guard, the autouse tmp-cwd chdir, the push guard, the
# TMPDIR-inside-repo refusal and the bare-run threshold. The plugin binds them
# in ``pytest_load_initial_conftests`` -- before this module is even imported --
# and is configured entirely from ``[tool.pytest.ini_options]`` in
# pyproject.toml. rlsbl distributes that floor's outer layer, so it runs the
# published plugin instead of a private copy; the guards below are the ones
# that are genuinely rlsbl-specific and have no plugin equivalent.
#
# Layer 2 (scripts/test.sh): a bwrap sandbox that runs the FULL suite with the
# real repo bound read-only, a writable ephemeral copy as cwd, private tmpfs
# TMPDIR, and no network. It exports ``STRICTTEST_SANDBOX=1``, which lifts the
# plugin's bare-run refusal (``stricttest_sandbox_required = true``) for
# full-ish runs while keeping small targeted runs bare-runnable.
#
# Layer 3 (CI): the CI workflow runs the suite job inside the same bwrap
# sandbox via scripts/test.sh.
# ---------------------------------------------------------------------------

# Repo root (parent of the tests/ directory holding this conftest).
_REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Repo-root litter guard: no test may leave a NEW entry directly under the
# repository root.
#
# Forensics: a ``MagicMock/mkdtemp()/`` directory tree sat at the repo root for
# weeks. A test mocked ``tempfile.mkdtemp``, production code interpolated the
# resulting MagicMock into a path, and the relative string
# ``MagicMock/mkdtemp()`` was then created against the process cwd. Nothing
# failed -- git does not even report an empty directory in ``status`` -- so the
# litter was only ever found by eye.
#
# This guard turns that whole class into a loud session failure: snapshot the
# root's direct entries at session start, compare at session end, fail naming
# the intruders. Only DIRECT children are compared, which is exactly where the
# class lands (a relative path resolved against a cwd at or above the root),
# and keeps the cost at two ``listdir`` calls per session.
# ---------------------------------------------------------------------------

# Tool artifacts that may legitimately materialize at the repo root during a
# run (pytest's cache, coverage data, lint/type caches, strictcli's coverage
# recorder). Everything else appearing mid-run is litter. Kept deliberately
# tight -- build outputs and vendored trees are NOT allowlisted, because no
# test has any business creating them either.
_ROOT_LITTER_ALLOWED = frozenset({
    "__pycache__",
    ".coverage",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".strictcli",
})


def _repo_root_entries():
    """Names of the direct children of the repo root (empty set if unreadable)."""
    try:
        return set(os.listdir(_REPO_ROOT))
    except OSError:
        return set()


@pytest.fixture(scope="session", autouse=True)
def _guard_repo_root_litter():
    """Fail the session if a test created a new entry at the repository root."""
    before = _repo_root_entries()
    yield
    intruders = sorted(
        name
        for name in _repo_root_entries() - before
        if name not in _ROOT_LITTER_ALLOWED and not name.startswith(".coverage.")
    )
    if intruders:
        listing = "\n".join(f"  {_REPO_ROOT / name}" for name in intruders)
        pytest.fail(
            "Test run littered the repository root with new entries:\n"
            f"{listing}\n\n"
            "Something wrote a path relative to the process cwd instead of an "
            "anchored temp location -- classically a mocked path helper (e.g. a "
            "MagicMock returned by a patched tempfile.mkdtemp) interpolated into "
            "a path string. Find the test, anchor the write to tmp_path, and "
            "delete the litter with saferm.",
            pytrace=False,
        )


from githarness import git as _git
from rlsbl.context import ProjectContext
from rlsbl.workspace import (
    DEFAULT_TAG_FORMAT,
    WORKSPACE_DIR,
    Releasable,
    save_workspace,
    write_releasable_version,
    get_releasable_changes_dir,
    get_releasable_dir,
)


class _CliCtx:
    """A stand-in for the strictcli dispatch Context in direct handler calls.

    Command handlers read the framework-owned reserved flags off the context
    (``ctx.dry_run`` / ``ctx.approve_consequential`` / ``ctx.quiet`` /
    ``ctx.json``) instead of receiving them as kwargs, so a test that calls a
    handler directly needs an object that carries them.  ``effects`` deliberately raises: minting effects needs a
    real dispatch, and a test that wants a preview must go through
    ``app.test(["--dry-run", ...])`` so the framework records and renders it.
    """

    def __init__(self, dry_run=False, approve_consequential=False,
                 quiet=False, verbose=False, json=False, unset=()):
        self.dry_run = dry_run
        self.approve_consequential = approve_consequential
        self.quiet = quiet
        self.verbose = verbose
        self.json = json
        self.payload_value = None
        self._unset = set(unset)

    def unset(self, name):
        """Answer the framework-minted ``--unset-<prop>`` of an update command.

        A cleared property and an untouched one both deliver None, so this is
        the only thing that tells them apart. ``unset=("description",)`` on the
        constructor is how a direct handler call states a clear.
        """
        return name in self._unset or name.replace("_", "-") in self._unset

    def payload(self, value):
        """Capture the machine payload the handler supplies (contract §19.4).

        The real framework validates it against the command's declared schema
        and emits it only in machine mode; a direct handler call just records
        it so the test can assert on it.
        """
        self.payload_value = value

    @property
    def effects(self):
        raise AssertionError(
            "cli_ctx() has no effects handle: drive this through "
            'app.test(["--dry-run", ...]) to exercise a preview'
        )


def cli_ctx(dry_run=False, approve_consequential=False, quiet=False,
            verbose=False, json=False, unset=()):
    """Build a stand-in dispatch context for a direct command-handler call."""
    return _CliCtx(dry_run=dry_run, approve_consequential=approve_consequential,
                   quiet=quiet, verbose=verbose, json=json, unset=unset)


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


def issue_phase_a_steps(steps, *, ctx=None, git_root=".", log=None,
                        preview=False):
    """Issue Phase-A plan steps through the real executor and return its log.

    The Phase-A writers are a builder/executor PAIR: a pure function derives
    the operands (the member/version plan, the selfdoc.json bytes) and a typed
    step carries them to a handler that issues the write. A test that wants the
    write has to go through both halves, and this is the executor half --
    ``execute_phase_a_plan`` with the slice of ``BuildInputs`` the write
    handlers read.

    Only steps with no ``marks`` and no ``capture`` are appropriate here: the
    executor would otherwise touch the release-state file, which these
    write-only steps never do.
    """
    from rlsbl.commands.release import phase_a

    said = []
    log = log or said.append
    inp = types.SimpleNamespace(
        log=log, ctx=ctx, git_root=str(git_root),
        state_path=None, completed=set(),
    )
    plan = phase_a.PhaseAPlan(steps=list(steps), files_to_commit=[])
    phase_a.execute_phase_a_plan(plan, inp, preview=preview)
    return said


def sync_member_versions(member_package_paths, monorepo_root, new_version,
                         files_to_commit, git_root, log, ctx,
                         exclude_path=None, releasable_config_dir=None):
    """Derive and issue a releasable's member version sync: builder, then executor.

    The Phase-A pair that replaced the old single-pass writer.
    ``_sync_member_package_versions_plan`` resolves each member's config and
    targets, hard-errors on a declared target whose manifest is missing, and
    predicts the version files; the ``WRITE_MEMBER_VERSIONS`` plan step carries
    those entries to the executor, which issues the writes.

    ``files_to_commit`` is extended with the builder's predicted paths, which is
    what the real builder does with the same entries.
    """
    from rlsbl.commands.release import phase_a
    from rlsbl.commands.release.execute import _sync_member_package_versions_plan

    entries = _sync_member_package_versions_plan(
        member_package_paths, monorepo_root, new_version, git_root,
        exclude_path=exclude_path,
        releasable_config_dir=releasable_config_dir,
    )
    for entry in entries:
        for fpath in entry["files"]:
            if fpath not in files_to_commit:
                files_to_commit.append(fpath)
    if entries:
        issue_phase_a_steps(
            [phase_a.PlanStep(
                kind=phase_a.WRITE_MEMBER_VERSIONS,
                release_step="VERSION_BUMPED",
                summary=f"sync {len(entries)} member package(s) -> {new_version}",
                payload={"entries": entries, "version": new_version},
            )],
            ctx=ctx, git_root=git_root, log=log,
        )
    return entries


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


def _normalize_member_path(path):
    """Normalize a workspace member path, mapping the root spellings to ".".

    A workspace may declare the repository root itself as a member
    (``path = "."``). ``""``, ``"."`` and ``"./"`` all mean that member, so
    they are folded to the single spelling rlsbl's loaders expect.
    """
    stripped = str(path).strip()
    if stripped in ("", ".", "./"):
        return "."
    return stripped.rstrip("/")


# The project keys workspace.toml carries, i.e. everything WorkspaceProject
# reads back and save_workspace serializes. A key outside this set is a typo,
# and a typo that serialized silently would produce a workspace not describing
# what the test declared.
_WORKSPACE_PROJECT_KEYS = frozenset({
    "path",
    "name",
    "library",
    "dev_node",
    "dev_only",
    "releasable",
    "depends_on",
    "import_name",
    "registry_name",
    "subtree_remote",
})


#: The root member this helper supplies when a test does not declare one.
#: Every workspace has one (the loader refuses a workspace without it) and its
#: kind is a real decision, so the default is the conservative one: a dev node,
#: whose residual files need no changelog coverage. A test that cares declares
#: its own root member instead.
DEFAULT_ROOT_MEMBER = {
    "path": ".",
    "name": "root",
    "dev_only": True,
    "releasable": False,
}


def declared_members(projects):
    """The members a test declared: everything but the root member.

    Every workspace carries a root member, supplied by ``make_workspace`` /
    ``with_root_member`` when the test does not declare one. A test asserting
    on the members IT set up filters it out with this.
    """
    return [p for p in projects if _normalize_member_path(p["path"]) != "."]


def with_root_member(projects, *, releasable=False):
    """Return *projects* as a member list a loaded workspace will accept.

    For tests that call ``save_workspace`` directly, which the loader now holds
    to the full model:

    - the default root member is appended when none is declared (appending,
      rather than prepending, keeps every positional assertion about the
      caller's own members valid);
    - a member with no ``releasable`` key gets *releasable* (``False`` by
      default -- a bare ``save_workspace`` writes no releasables, so standing
      outside every releasable is the only consistent answer).
    """
    from rlsbl.workspace import WorkspaceProject as _WP

    prepared = []
    has_root = False
    for proj in projects:
        if _normalize_member_path(proj["path"]) == ".":
            has_root = True
        if "releasable" not in proj:
            data = dict(proj.to_dict() if isinstance(proj, _WP) else proj)
            data["releasable"] = releasable
            proj = _WP(data) if isinstance(proj, _WP) else data
        prepared.append(proj)

    if has_root:
        return prepared
    root = dict(DEFAULT_ROOT_MEMBER)
    if prepared and isinstance(prepared[0], _WP):
        root = _WP(root)
    return [*prepared, root]


#: A hand-written root-member block, for tests that build workspace.toml as raw
#: TOML text rather than through ``make_workspace``.
ROOT_MEMBER_TOML = (
    '[[projects]]\n'
    'path = "."\n'
    'name = "root"\n'
    'dev_only = true\n'
    'releasable = false\n'
)


def workspace_toml(body="", *, releasables=(), root_member=ROOT_MEMBER_TOML):
    """Assemble a loadable workspace.toml body around a hand-written *body*.

    The loader refuses a workspace with no root member and one with no
    ``[[releasables]]`` section, and almost no test that writes raw TOML is
    about either. This supplies both around whatever the test actually wants to
    say.

    Args:
        body: the test's own TOML (typically ``[[projects]]`` blocks).
        releasables: the releasables to declare. Each item is a name string or
            a ``{"name": ..., "tag_format": ...}`` dict. Empty (the default)
            writes ``releasables = []`` -- an explicit-mode workspace with no
            releasables yet.
        root_member: the root-member block to prepend, or ``""`` for none
            (which is what a test asserting the no-root-member error wants).

    A *body* that already declares its own releasables section or its own root
    member keeps it: neither is added twice, so this can be applied to every
    hand-written workspace body in the suite without reading each one.
    """
    declares_releasables = "[[releasables]]" in body or "releasables =" in body
    declares_root = 'path = "."' in body or "path = '.'" in body
    # A body that declares `projects` as an inline array cannot also carry a
    # [[projects]] table -- that is a duplicate key, not a member list.
    if declares_root or "projects =" in body:
        root_member = ""

    # Order is load-bearing in TOML: a top-level key written after a table
    # header belongs to that table. The bare `releasables = []` key therefore
    # goes first, the body next (its own top-level keys stay top-level), and
    # the added tables last.
    head = []
    tail = []
    if releasables is None:
        # An explicit "emit nothing": the caller is testing what happens to a
        # workspace with no releasables section at all.
        declares_releasables = True
    if not declares_releasables:
        if releasables:
            for rel in releasables:
                if isinstance(rel, str):
                    rel = {"name": rel}
                block = f'[[releasables]]\nname = "{rel["name"]}"\n'
                if rel.get("tag_format"):
                    block += f'tag_format = "{rel["tag_format"]}"\n'
                tail.append(block)
        else:
            head.append("releasables = []\n")
    if body:
        head.append(body if body.endswith("\n") else body + "\n")
    if root_member:
        tail.insert(0, root_member)
    return "\n".join(head + tail)


def _member_is_releasable(entry):
    """Would rlsbl consider this member entry releasable?"""
    if entry.get("releasable") is False:
        return False
    if entry.get("dev_node"):
        return False
    if entry.get("dev_only") and not isinstance(entry.get("releasable"), str):
        return False
    return True


def make_workspace(root, projects, releasables=None):
    """Create a .rlsbl-monorepo/workspace.toml with the given project list.

    Serialization goes through rlsbl's own ``save_workspace``, so the file is
    byte-identical to what the tools write and no key can be recognized here
    but dropped there (or the reverse).

    Two parts of the workspace model are supplied when a test omits them,
    because the loader refuses a workspace that lacks either and almost no
    test is about them:

    - **the root member.** When no member declares ``path = "."``, the
      :data:`DEFAULT_ROOT_MEMBER` dev node is prepended. Declare your own root
      member to override it.
    - **explicit mode.** When ``releasables`` is omitted, one releasable per
      releasable-eligible member is derived, named after the member, and the
      member gets the matching ``releasable`` key. Pass ``releasables``
      (possibly ``[]``) to say exactly which ones exist.

    Args:
        root: repository root (Path).
        projects: list of project dicts. Every key ``save_workspace``
            serializes is accepted -- ``path``, ``name``, ``library``,
            ``dev_node``, ``dev_only``, ``releasable``, ``depends_on``,
            ``import_name``, ``registry_name`` and ``subtree_remote`` -- and any
            other key is a ``ValueError``. A project may declare the repository
            root itself as a member with ``path = "."`` (``""`` and ``"./"``
            are accepted spellings of it); at most one root member is allowed.
        releasables: an explicit-mode ``[[releasables]]`` section, emitted ahead
            of the projects. Each item may be a ``Releasable``, a dict
            (``{"name": ..., "tag_format": ...}``) or a bare name string.
            ``tag_format`` is written only when it differs from the default.
            Omit it to have one derived per releasable member.

    In explicit mode every releasable project must carry a ``releasable``
    key (a name, or ``False`` to stand outside every releasable) -- that is
    rlsbl's rule, not this helper's, and ``load_releasables`` enforces it.
    """
    rels = None
    if releasables is not None:
        rels = []
        for rel in releasables:
            if isinstance(rel, str):
                rels.append(Releasable(name=rel))
            elif isinstance(rel, dict):
                rels.append(Releasable(
                    name=rel["name"],
                    tag_format=rel.get("tag_format", DEFAULT_TAG_FORMAT),
                ))
            else:
                rels.append(rel)

    root_members = [
        p for p in projects if _normalize_member_path(p["path"]) == "."
    ]
    if len(root_members) > 1:
        raise ValueError(
            "at most one workspace member may be the repository root "
            f"(path \".\"); got {[p['name'] for p in root_members]}"
        )

    from rlsbl.workspace import WorkspaceProject as _WP

    prepared = []
    for proj in projects:
        if isinstance(proj, _WP):
            proj = proj.to_dict()
        unknown = sorted(set(proj) - _WORKSPACE_PROJECT_KEYS)
        if unknown:
            raise ValueError(
                f"project {proj.get('name')!r}: unknown workspace key(s) "
                f"{', '.join(unknown)}. workspace.toml carries only "
                f"{', '.join(sorted(_WORKSPACE_PROJECT_KEYS))}; a key outside "
                f"that set would be written but never read back."
            )
        entry = dict(proj)
        entry["path"] = _normalize_member_path(proj["path"])
        prepared.append(entry)

    if not root_members:
        prepared.insert(0, dict(DEFAULT_ROOT_MEMBER))

    if rels is None:
        rels = []
        seen = set()
        for entry in prepared:
            if not _member_is_releasable(entry):
                continue
            name = entry.get("releasable")
            if not isinstance(name, str):
                if _normalize_member_path(entry["path"]) == ".":
                    # A root member's releasable can never inherit a tag format
                    # (the loader refuses that), and the kind of the root member
                    # is a real decision -- so an undeclared one is a dev node.
                    entry["releasable"] = False
                    entry.setdefault("dev_only", True)
                    continue
                name = entry["name"]
                entry["releasable"] = name
            if name not in seen:
                seen.add(name)
                rels.append(Releasable(name=name))

    save_workspace(str(root), prepared, releasables=rels)


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
        # Resolved at CALL time, not fixture-setup time: this fixture sits
        # one layer above subprocess, so a test that patches subprocess.run
        # itself must still win here.
        return real_subprocess.run(cmd, *args, **kwargs)

    # Patch the PRIMITIVE, not the chokepoint's public surface: patching
    # rlsbl.effects.run would replace the mode routing itself, so a --dry-run
    # test would execute every subprocess for real through this shim instead
    # of recording it on the effects handle.  rlsbl._effects_direct.run is the
    # layer that actually reaches subprocess, and it is only consulted in live
    # mode -- which is exactly what this fixture is neutralizing.
    with patch("rlsbl._effects_direct.run", side_effect=_mock_run):
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


# There is deliberately no push-timeout fixture here. One used to set
# RLSBL_PUSH_TIMEOUT on every test "for determinism"; rlsbl stopped reading
# that variable (and the rest of its family) when timeouts became
# config-and-flag only, so the fixture had been setting an environment
# variable nothing consulted. Determinism comes from the resolution order
# itself: --push-timeout beats the config key beats the shipped default, and
# no step of it can be reached from the environment.


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

    # Create workspace.toml. make_workspace derives one releasable per member
    # (named after it) and the mandatory root member.
    make_workspace(tmp_path, projects)

    # Each derived releasable needs its own state: the version the member
    # publishes and the changelog its entries go into.
    for proj in projects:
        write_releasable_version(str(tmp_path), proj["name"], "0.1.0")
        rel_changes = get_releasable_changes_dir(str(tmp_path), proj["name"])
        os.makedirs(rel_changes, exist_ok=True)
        Path(rel_changes, "unreleased.jsonl").write_text("")

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
    for proj in projects:
        Path(
            get_releasable_changes_dir(str(tmp_path), proj["name"]),
            "unreleased.jsonl",
        ).write_text(_uf_entry)
    run_git(tmp_path, "add", "python/.rlsbl/changes/unreleased.jsonl")
    run_git(tmp_path, "add", "go/.rlsbl/changes/unreleased.jsonl")
    run_git(tmp_path, "add", WORKSPACE_DIR)
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
        "path": ".",
        "name": "root",
        "dev_only": True,
        "releasable": False,
    },
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


# ---------------------------------------------------------------------------
# Releasable state directories (.rlsbl-monorepo/releasables/<name>/)
#
# The releasable model keeps a releasable's whole release state OUTSIDE the
# member packages: version, changes/, releases/ and config.json all live under
# ``.rlsbl-monorepo/releasables/<name>/``. Fixtures that predate the model tend
# to declare ``[[releasables]]`` but still put changelog state in per-package
# ``<pkg>/.rlsbl/changes/``, which is the pre-releasable layout and exercises a
# different code path. The helpers below build the real layout.
# ---------------------------------------------------------------------------

# A filled-in release file, as `release init` scaffolds it plus operator edits.
# Passed as the ``release_file``/archive body when a fixture wants one on disk.
DEFAULT_RELEASE_FILE = (
    "format_version = 1\n"
    'bump = "patch"\n'
    'description = "Test release"\n'
    'context = ""\n'
    "include = []\n"
    "exclude = []\n"
)


# The config every releasable carries when a fixture does not state one.
# ``publish_mode`` is a required key in a real releasable config, so ``{}`` is
# a shape rlsbl never produces; ``"none"`` is the stance a test wants, since it
# suppresses publishing to every public registry.
DEFAULT_RELEASABLE_CONFIG = {"publish_mode": "none"}


def jsonl_line(entry):
    """Render one changelog entry as a single JSONL line (no trailing newline).

    Accepts a ``ChangelogEntry`` (serialized through the real serializer, so the
    line carries ``format_version``), a plain dict (JSON-encoded as given, for
    tests that need a malformed or legacy line), or an already-serialized string.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return json.dumps(entry)
    from rlsbl.changelog.schema import serialize_entry

    return serialize_entry(entry)


def write_jsonl(path, entries, *, lock=False):
    """Write ``entries`` as JSONL to ``path`` (creating parent dirs).

    ``lock`` chmods the file 444, matching how rlsbl locks a released
    version's JSONL file.
    """
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(jsonl_line(entry) + "\n")
    if lock:
        os.chmod(path, 0o444)


def _refuse_locked_rewrite(path):
    """Hard-error when ``path`` exists and is locked against rewriting.

    Released changelog files and archived release files are chmod 444 the
    moment a release finalizes them, and rlsbl never rewrites one in place.
    A fixture that unlocked and rewrote such a file would let a test quietly
    corrupt the premise it is asserting against, so this refuses loudly
    instead.
    """
    if os.path.exists(path) and not os.access(path, os.W_OK):
        raise ValueError(
            f"make_releasable_state would rewrite already-released state at "
            f"{path}, which is locked (0444). The fixture never overwrites "
            f"released state: a released version's files are immutable, "
            f"exactly as rlsbl locks them. Write every version of a releasable "
            f"in ONE call, or name a version that has not been released yet."
        )


def make_releasable_state(
    root,
    name,
    *,
    version="0.1.0",
    config=None,
    unreleased_entries=None,
    versioned_entries=None,
    release_file=None,
    archived_releases=None,
    hooks=None,
    lock_versioned=True,
):
    """Create a releasable's state directory with the real releasable layout.

    Writes ``.rlsbl-monorepo/releasables/<name>/`` containing:

    - ``version``                      -- the releasable's current version
    - ``changes/unreleased.jsonl``     -- always created (empty by default)
    - ``changes/<v>.jsonl`` + ``.md``  -- one pair per ``versioned_entries``
      key. The ``.md`` is rendered by rlsbl's own ``generate_version_file``
      over that JSONL plus the version's archived release file, so its content
      and its 644 mode are exactly what a real release leaves behind.
    - ``releases/``                    -- always created; holds ``unreleased.toml``
      when ``release_file`` is given and ``v<version>.toml`` archives
    - ``config.json``                  -- releasable-level config
    - ``hooks/<name>``                 -- executable hook scripts

    Args:
        root: the monorepo root (str or Path).
        name: releasable name.
        version: value for the ``version`` file.
        config: dict written to ``config.json``. Defaults to
            ``DEFAULT_RELEASABLE_CONFIG``; an explicitly empty dict is honored.
        unreleased_entries: entries for ``changes/unreleased.jsonl``
            (ChangelogEntry, dict or raw-line str -- see ``jsonl_line``).
        versioned_entries: dict mapping version string to its entry list,
            written as released (locked) ``<version>.jsonl`` files.
        release_file: TOML body for ``releases/unreleased.toml``; omitted
            when None. ``DEFAULT_RELEASE_FILE`` is a ready-made body.
        archived_releases: dict mapping version string to the archived
            ``releases/v<version>.toml`` body. Every version in
            ``versioned_entries`` gets a default archive automatically (a
            released version always has one); entries here add to or override
            those defaults.
        hooks: dict mapping hook file name to script content (chmod 755).
        lock_versioned: chmod 444 the versioned ``.jsonl`` files, as rlsbl
            locks them at finalization. The generated ``.md`` siblings stay
            writable either way -- rlsbl never locks those.

    Returns:
        Path to the releasable's state directory.
    """
    root = str(root)
    rel_dir = get_releasable_dir(root, name)
    changes_dir = get_releasable_changes_dir(root, name)
    releases_dir = os.path.join(rel_dir, "releases")
    versioned_entries = versioned_entries or {}
    archives = {ver: DEFAULT_RELEASE_FILE for ver in versioned_entries}
    archives.update(archived_releases or {})

    # Released state is immutable in rlsbl: a version's .jsonl and its archived
    # release file are locked at finalization and never rewritten. Refuse
    # up front -- BEFORE any write -- so a second call naming an already-written
    # version fails with a sentence instead of a bare PermissionError, and
    # leaves the state dir exactly as it was.
    for ver in versioned_entries:
        _refuse_locked_rewrite(os.path.join(changes_dir, f"{ver}.jsonl"))
    for ver in archives:
        _refuse_locked_rewrite(os.path.join(releases_dir, f"v{ver}.toml"))

    os.makedirs(rel_dir, exist_ok=True)

    write_releasable_version(root, name, version)

    os.makedirs(changes_dir, exist_ok=True)
    write_jsonl(
        os.path.join(changes_dir, "unreleased.jsonl"), unreleased_entries or []
    )

    for ver, entries in versioned_entries.items():
        write_jsonl(
            os.path.join(changes_dir, f"{ver}.jsonl"),
            entries,
            lock=lock_versioned,
        )

    os.makedirs(releases_dir, exist_ok=True)
    if release_file is not None:
        with open(
            os.path.join(releases_dir, "unreleased.toml"), "w", encoding="utf-8"
        ) as f:
            f.write(release_file)

    for ver, body in archives.items():
        archive_path = os.path.join(releases_dir, f"v{ver}.toml")
        with open(archive_path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(archive_path, 0o444)

    # The per-version .md is the GENERATOR's output over the JSONL and the
    # archive that were just written -- never a hand-rolled stub, which would
    # drift from the real format the moment the generator changes. Runs after
    # the archives, because the description/context/bump come from them.
    # generate_version_file creates a brand-new .md with the umask-derived
    # mode (644), which is what production leaves behind: the .md is a
    # regenerated derivative, not a locked record like the .jsonl.
    from rlsbl.changelog.generate import (
        _read_release_metadata_full,
        generate_version_file,
    )

    for ver in versioned_entries:
        ver_desc, ver_ctx, ver_bump = _read_release_metadata_full(
            root, ver, releases_dir=releases_dir,
        )
        generate_version_file(
            changes_dir, ver,
            description=ver_desc, context=ver_ctx, bump_type=ver_bump or None,
        )

    with open(os.path.join(rel_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(
            DEFAULT_RELEASABLE_CONFIG if config is None else config,
            f, indent=2,
        )
        f.write("\n")

    if hooks:
        hooks_dir = os.path.join(rel_dir, "hooks")
        os.makedirs(hooks_dir, exist_ok=True)
        for hook_name, hook_content in hooks.items():
            hook_path = os.path.join(hooks_dir, hook_name)
            with open(hook_path, "w", encoding="utf-8") as f:
                f.write(hook_content)
            os.chmod(hook_path, 0o755)

    return Path(rel_dir)


def make_releasable_monorepo(root, **kwargs):
    """Build an explicit-mode releasable monorepo at ``root``.

    Same keyword arguments as ``_create_multi_releasable_monorepo``; the only
    difference is that ``root`` need not exist yet and need not be the pytest
    ``tmp_path`` itself. Use this when the test also needs a sibling directory
    outside the repo (an extract target, a source repo to absorb).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    return _create_multi_releasable_monorepo(root, **kwargs)


def _create_multi_releasable_monorepo(
    tmp_path,
    *,
    releasables=None,
    projects=None,
    releasable_configs=None,
    hook_configs=None,
    releasable_changes=None,
    releasable_releases=None,
    initial_version="0.1.0",
    write_initial_release_state=True,
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
            written to ``<releasable_dir>/config.json``. A releasable not named
            here gets ``DEFAULT_RELEASABLE_CONFIG``.
        hook_configs: dict mapping releasable name to hook config dict,
            written to ``<releasable_dir>/hooks/`` directory files.
        releasable_changes: dict mapping releasable name to its changelog
            content: ``{"unreleased": [entries], "versions": {ver: [entries]}}``.
            Absent releasables get an empty ``unreleased.jsonl``.
        releasable_releases: dict mapping releasable name to its release files:
            ``{"unreleased": <toml body>, "archives": {ver: <toml body>}}``.
            Released versions named in ``releasable_changes`` are archived
            automatically.
        initial_version: version string for all releasables (default "0.1.0").
        write_initial_release_state: when True (the default), every releasable
            gets the full released trio for ``initial_version`` -- a locked
            ``changes/<v>.jsonl`` with one user-facing entry over a commit that
            resolves, its generated ``.md``, and the ``releases/v<v>.toml``
            archive -- so the tag the factory creates stands over real released
            state, as it always does in a real repo. A releasable that names
            ``initial_version`` in ``releasable_changes["versions"]`` keeps its
            own entries. Pass False to get the DAMAGED shape on purpose: a
            tagged version with no state behind it, which real rlsbl never
            produces and only a test about that damage should ask for.

    Returns:
        SimpleNamespace with root, releasables, projects, and per-project dirs.
    """
    from types import SimpleNamespace

    if releasables is None:
        releasables = list(_DEFAULT_RELEASABLES)
    if projects is None:
        projects = [dict(p) for p in _DEFAULT_PROJECTS]
    elif not any(_normalize_member_path(p["path"]) == "." for p in projects):
        # Every workspace has a root member; supply the default dev node when
        # the caller did not declare one (see make_workspace).
        projects = [dict(DEFAULT_ROOT_MEMBER), *projects]
    if releasable_configs is None:
        releasable_configs = {}
    if hook_configs is None:
        hook_configs = {}
    if releasable_changes is None:
        releasable_changes = {}
    if releasable_releases is None:
        releasable_releases = {}

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

    # Create per-project directories with minimal project files. The root
    # member is skipped: it owns the repository root, which already exists and
    # must not be given a package manifest or a .rlsbl/ of its own.
    project_dirs = {}
    for proj in projects:
        if _normalize_member_path(proj["path"]) == ".":
            project_dirs[proj["name"]] = tmp_path
            continue
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

    # The commit the default released changelog entry points at. Every hash in
    # a real released JSONL resolves, so the fixture's does too.
    initial_sha = git_head(tmp_path)

    # Set up per-releasable state directories (version, changes, releases,
    # config.json, hooks) -- the real releasable layout.
    for rel in releasables:
        changes = releasable_changes.get(rel.name, {})
        releases = releasable_releases.get(rel.name, {})
        versions = dict(changes.get("versions") or {})
        if write_initial_release_state and initial_version not in versions:
            from rlsbl.changelog.schema import ChangelogEntry

            versions[initial_version] = [
                ChangelogEntry(
                    commits=[initial_sha],
                    user_facing=True,
                    description=f"Initial {rel.name} release",
                    type="feature",
                ),
            ]
        make_releasable_state(
            tmp_path,
            rel.name,
            version=initial_version,
            config=releasable_configs.get(rel.name),
            unreleased_entries=changes.get("unreleased"),
            versioned_entries=versions or None,
            release_file=releases.get("unreleased"),
            archived_releases=releases.get("archives"),
            hooks=hook_configs.get(rel.name),
        )

    # Commit all workspace and project files
    run_git(tmp_path, "add", WORKSPACE_DIR)
    for proj in projects:
        if _normalize_member_path(proj["path"]) == ".":
            continue
        run_git(tmp_path, "add", proj["path"])
    run_git(tmp_path, "commit", "-q", "-m", "add multi-releasable monorepo")

    # Tag each releasable at the initial version
    for rel in releasables:
        tag = rel.effective_tag_format.format(name=rel.name, version=initial_version)
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
