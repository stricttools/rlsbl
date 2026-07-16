"""Tests for batch_limits config inheritance and exclusion writes in releasable mode (Phase 1f).

Covers:
1. cmd_add in releasable mode inherits releasable-level batch_limits
2. Auto-created exclusions go to releasable-level config.json (not per-package)
3. Standalone mode still writes exclusions to per-package config.json (regression)
"""

import json
import os

import pytest

from conftest import run_git as _run_git, make_commit as _make_commit
from rlsbl.changelog.files import get_changes_dir, read_unreleased
from rlsbl.commands.changelog_cmd import cmd_add
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    get_releasable_changes_dir,
    get_releasable_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_explicit_workspace(root, releasables, projects):
    """Create a workspace.toml with [[releasables]] and [[projects]] sections."""
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for rel in releasables:
        lines.append("[[releasables]]")
        lines.append(f'name = "{rel["name"]}"')
        lines.append("")

    for proj in projects:
        lines.append("[[projects]]")
        lines.append(f'path = "{proj["path"]}"')
        lines.append(f'name = "{proj["name"]}"')
        if "releasable" in proj:
            val = proj["releasable"]
            if isinstance(val, str):
                lines.append(f'releasable = "{val}"')
            elif val is False:
                lines.append("releasable = false")
        if proj.get("dev_only"):
            lines.append("dev_only = true")
        lines.append("")

    (ws_dir / WORKSPACE_FILE).write_text("\n".join(lines))


def _setup_releasable_repo(tmp_path, monkeypatch, *, batch_limit=3):
    """Create a git repo with a releasable workspace, per-releasable config,
    and a member project. Returns (repo_root, project_dir).

    The releasable-level config has batch_limits.max_commits_per_entry set
    to ``batch_limit``. The per-package config has NO batch_limits key,
    so the only way cmd_add gets the correct limit is via inheritance.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@test.local")
    _run_git(repo, "config", "user.name", "Test")

    # Initial commit
    (repo / "README.md").write_text("# test\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "initial")

    # Create workspace with one releasable and one member project
    _make_explicit_workspace(repo, [{"name": "www"}], [
        {"path": "app", "name": "app", "releasable": "www"},
    ])

    # Create the project directory
    proj_dir = repo / "app"
    proj_dir.mkdir()

    # Per-package .rlsbl/config.json (no batch_limits -- must inherit)
    pkg_rlsbl = proj_dir / ".rlsbl"
    pkg_rlsbl.mkdir()
    (pkg_rlsbl / "config.json").write_text(json.dumps({"publish_mode": "ci"}) + "\n")

    # Releasable-level config with batch_limits
    rel_dir = get_releasable_dir(str(repo), "www")
    os.makedirs(rel_dir, exist_ok=True)
    rel_config = {
        "publish_mode": "ci",
        "batch_limits": {
            "max_commits_per_entry": batch_limit,
            "max_entries_per_commit": 5,
            "exclusions": [],
        },
    }
    with open(os.path.join(rel_dir, "config.json"), "w") as f:
        json.dump(rel_config, f, indent=2)
        f.write("\n")

    # Create releasable-level changes directory with empty unreleased.jsonl
    changes_dir = get_releasable_changes_dir(str(repo), "www")
    os.makedirs(changes_dir, exist_ok=True)
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
        f.write("")

    # Commit all setup files
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-q", "-m", "add workspace")

    # Tag so there is an unreleased range
    _run_git(repo, "tag", "www-v0.1.0")

    return repo, proj_dir


class TestReleasableBatchLimitsInheritance:
    """cmd_add in releasable mode inherits batch_limits from releasable-level config."""

    def test_inherits_releasable_batch_limit(self, tmp_path, monkeypatch):
        """cmd_add uses the releasable-level max_commits_per_entry when per-package has none."""
        repo, proj_dir = _setup_releasable_repo(tmp_path, monkeypatch, batch_limit=3)

        # Create 5 commits in the project directory (exceeds limit of 3)
        shas = []
        for i in range(5):
            shas.append(_make_commit(proj_dir, f"file{i}.txt", f"change {i}"))

        flags = {
            "commits": ",".join(shas),
            "description": "Big batch feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
            "allow-batch": False,
        }

        # Should fail because releasable limit is 3 and we have 5 commits
        with pytest.raises(SystemExit) as exc_info:
            cmd_add(flags, project_root=proj_dir)
        assert exc_info.value.code == 1

    def test_within_releasable_limit_succeeds(self, tmp_path, monkeypatch):
        """cmd_add succeeds when commits are within the releasable-level limit."""
        repo, proj_dir = _setup_releasable_repo(tmp_path, monkeypatch, batch_limit=10)

        # Create 5 commits (within limit of 10)
        shas = []
        for i in range(5):
            shas.append(_make_commit(proj_dir, f"file{i}.txt", f"change {i}"))

        flags = {
            "commits": ",".join(shas),
            "description": "Batch feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
            "allow-batch": False,
        }

        cmd_add(flags, project_root=proj_dir)

        changes_dir = get_releasable_changes_dir(str(repo), "www")
        entries = read_unreleased(changes_dir)
        assert len(entries) == 1
        assert len(entries[0].commits) == 5


class TestReleasableExclusionWrites:
    """Auto-created exclusions go to releasable-level config.json in releasable mode."""

    def test_exclusion_written_to_releasable_config(self, tmp_path, monkeypatch):
        """--allow-batch writes the exclusion to releasable-level config.json, not per-package."""
        repo, proj_dir = _setup_releasable_repo(tmp_path, monkeypatch, batch_limit=3)

        # Create 5 commits (exceeds limit of 3)
        shas = []
        for i in range(5):
            shas.append(_make_commit(proj_dir, f"file{i}.txt", f"change {i}"))

        flags = {
            "commits": ",".join(shas),
            "description": "Big batch feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
            "allow-batch": True,
        }
        cmd_add(flags, project_root=proj_dir)

        # Verify exclusion is in releasable-level config.json
        rel_dir = get_releasable_dir(str(repo), "www")
        rel_config_path = os.path.join(rel_dir, "config.json")
        rel_config = json.loads(open(rel_config_path).read())
        exclusions = rel_config["batch_limits"]["exclusions"]
        assert len(exclusions) == 1
        assert exclusions[0]["reason"] == "Big batch feature"

        # Verify per-package config.json does NOT have the exclusion
        pkg_config_path = proj_dir / ".rlsbl" / "config.json"
        pkg_config = json.loads(pkg_config_path.read_text())
        assert "batch_limits" not in pkg_config or \
               len(pkg_config.get("batch_limits", {}).get("exclusions", [])) == 0

    def test_entry_written_to_releasable_changes_dir(self, tmp_path, monkeypatch):
        """The changelog entry itself is written to the releasable changes dir."""
        repo, proj_dir = _setup_releasable_repo(tmp_path, monkeypatch, batch_limit=3)

        shas = []
        for i in range(5):
            shas.append(_make_commit(proj_dir, f"file{i}.txt", f"change {i}"))

        flags = {
            "commits": ",".join(shas),
            "description": "Big batch feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
            "allow-batch": True,
        }
        cmd_add(flags, project_root=proj_dir)

        changes_dir = get_releasable_changes_dir(str(repo), "www")
        entries = read_unreleased(changes_dir)
        assert len(entries) == 1
        assert len(entries[0].commits) == 5


class TestStandaloneExclusionRegression:
    """In standalone mode, exclusions still go to per-package config.json."""

    def test_standalone_exclusion_in_pkg_config(self, tmp_path, monkeypatch):
        """Standalone project writes exclusions to per-package .rlsbl/config.json."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")

        # Initial commit
        (repo / "README.md").write_text("# test\n")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-q", "-m", "initial")

        # Baseline version tag
        _run_git(repo, "tag", "v0.0.0")

        # Set up .rlsbl/changes
        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")

        # Config with low batch limit
        config = {
            "publish_mode": "ci",
            "batch_limits": {
                "max_commits_per_entry": 3,
                "max_entries_per_commit": 5,
                "exclusions": [],
            },
        }
        (repo / ".rlsbl" / "config.json").write_text(json.dumps(config, indent=2) + "\n")

        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-q", "-m", "setup")

        # Create 5 commits (exceeds limit of 3)
        shas = []
        for i in range(5):
            shas.append(_make_commit(repo, f"file{i}.txt", f"change {i}"))

        flags = {
            "commits": ",".join(shas),
            "description": "Big standalone batch",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
            "allow-batch": True,
        }
        cmd_add(flags, project_root=repo)

        # Verify exclusion is in per-package config.json
        pkg_config = json.loads((repo / ".rlsbl" / "config.json").read_text())
        exclusions = pkg_config["batch_limits"]["exclusions"]
        assert len(exclusions) == 1
        assert exclusions[0]["reason"] == "Big standalone batch"
