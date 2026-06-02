"""Shared pytest fixtures for the rlsbl test suite."""

import json
import os
import subprocess
import time

import pytest

from rlsbl.context import ProjectContext
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE


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


# ---------------------------------------------------------------------------
# Utility functions (imported explicitly by test modules)
# ---------------------------------------------------------------------------


def run_git(repo, *args):
    """Run a git command in the given repo directory."""
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def git_head(repo):
    """Get HEAD hash."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def make_commit(repo, filename="file.txt", message="change"):
    """Make a commit and return its hash."""
    filepath = repo / filename
    filepath.write_text(f"content-{time.monotonic_ns()}\n")
    run_git(repo, "add", filename)
    run_git(repo, "commit", "-q", "-m", message)
    return git_head(repo)


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
        if proj.get("changelog_exempt"):
            lines.append("changelog_exempt = true")
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


@pytest.fixture(autouse=True)
def _default_push_timeout(monkeypatch):
    """Set a default push timeout for all tests via the env var.

    Production code requires explicit push_timeout config (no implicit
    default). Tests need a value so release-flow tests don't fail with
    'push_timeout not configured'. The env var has highest precedence,
    so individual tests can still override via monkeypatch or by setting
    the env var themselves.
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
        json.dumps({"private": False}) + "\n"
    )

    (go_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (go_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (go_dir / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False}) + "\n"
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
