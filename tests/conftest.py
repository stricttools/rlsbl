"""Shared pytest fixtures for the rlsbl test suite."""

import json
import os
import subprocess
import time
from unittest.mock import patch

import pytest

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

    Always ensures ``coverage_unit`` is present in the config (defaults to
    ``"commit"``) so that ``read_coverage_unit()`` does not raise in tests
    that don't explicitly test coverage_unit behavior.
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
    config.setdefault("coverage_unit", "commit")
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


@pytest.fixture(scope="session")
def safegit_bin(tmp_path_factory):
    """Build the pinned safegit release once and return the binary path.

    The pin is derived from SAFEGIT_MIN_VERSION (the exact version the scrub
    flow declares as its minimum), installed via
    ``go install github.com/smm-h/safegit@v<pin>`` into a shared directory.
    Module-proxy installs are reproducible, so this pins properly on every
    machine and in CI.

    NO skip-if-absent: if the Go toolchain or network is unavailable the
    tests FAIL. CI runners have Go preinstalled.

    Cross-worker/session safety: the build directory lives in the shared
    pytest temp root (one level above the per-session basetemp, which is also
    shared by xdist workers) and is guarded by an O_EXCL lock file plus a
    .done marker, so concurrent workers build exactly once.
    """
    from rlsbl.commands.release_scrub import SAFEGIT_MIN_VERSION

    pin = "v" + ".".join(str(p) for p in SAFEGIT_MIN_VERSION)
    shared_root = tmp_path_factory.getbasetemp().parent
    gobin = shared_root / f"safegit-{pin}"
    binary = gobin / "safegit"
    done_marker = gobin / ".done"
    lock_path = shared_root / f"safegit-{pin}.lock"

    deadline = time.monotonic() + 600
    while not done_marker.exists():
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
            if not done_marker.exists():
                gobin.mkdir(parents=True, exist_ok=True)
                env = {**os.environ, "GOBIN": str(gobin)}
                subprocess.run(
                    ["go", "install", f"github.com/smm-h/safegit@{pin}"],
                    env=env, check=True, timeout=600,
                    capture_output=True, text=True,
                )
                done_marker.touch()
        finally:
            os.close(fd)
            os.unlink(str(lock_path))

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
                if os.path.exists(target_file):
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

    _uf_entry = json.dumps({"commits": ["abc1234"], "user_facing": True, "description": "test", "type": "feature"}) + "\n"
    (python_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (python_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(_uf_entry)
    (python_dir / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
    )

    (go_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (go_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(_uf_entry)
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
