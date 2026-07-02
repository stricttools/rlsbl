"""Tests for representative-write elimination in releasable releases.

A releasable release must not write representative-member state that
belongs at the releasable (or nowhere):

- .rlsbl/version marker: only refreshed for actually-scaffolded standalone
  projects (scaffold metadata present); never written in releasable mode.
- A private representative's manifests are never version-bumped or
  keyword-tagged; the releasable version file still updates.
- clean_stale_exclusions cleans the RELEASABLE config.json in releasable
  mode (where `changelog add --allow-batch` writes exclusions).
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.release import run_cmd
from rlsbl.context import ProjectContext, create_context
from rlsbl.release_file import ReleaseConfig
from rlsbl.utils import run as real_run
from rlsbl.workspace import (
    Releasable,
    get_releasable_changes_dir,
    get_releasable_dir,
    read_releasable_version,
    save_workspace,
    write_releasable_version,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_head(repo):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _rc(description=""):
    return ReleaseConfig(
        bump="patch",
        include=["npm"],
        exclude=[],
        description=description,
    )


def _fake_run_factory():
    def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
        if cmd == "gh":
            return ""
        if cmd == "git" and args and args[0] == "push":
            return ""
        if cmd == "git" and args and args[0] == "fetch":
            return ""
        if cmd == "git" and args and args[:2] == ["rev-list", "--count"] and any("origin/" in a for a in args):
            return "0"
        return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)
    return fake_run


def _release_patches(extra=()):
    patches = [
        patch("rlsbl.commands.release.check_gh_installed", return_value=True),
        patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        patch("rlsbl.commands.release.push_if_needed"),
        patch("rlsbl.commands.release.run_gh", return_value=""),
        patch("rlsbl.commands.release.run", side_effect=_fake_run_factory()),
        patch("rlsbl.commands.release.remote_branch_exists", return_value=True),
    ]
    patches.extend(extra)
    return patches


def _run_release(member_dir, root, extra_patches=()):
    ctx = create_context(Path(str(member_dir)), workspace_root=Path(str(root)))
    patches = _release_patches(extra_patches)
    for p in patches:
        p.start()
    try:
        run_cmd(
            _rc(),
            {"yes": True, "quiet": True, "skip-lock": True},
            ctx=ctx,
        )
    finally:
        for p in patches:
            p.stop()


def _setup_releasable_workspace(root, member_config=None,
                                releasable_config=None):
    """Workspace with releasable 'alpha' whose sole member is packages/core."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")

    core = root / "packages" / "core"
    core.mkdir(parents=True)
    (core / "package.json").write_text(
        json.dumps({"name": "core", "version": "1.0.0"}, indent=2) + "\n"
    )
    (core / ".rlsbl").mkdir()
    cfg = member_config or {"private": False, "targets": ["npm"], "pipelines": {}}
    (core / ".rlsbl" / "config.json").write_text(json.dumps(cfg) + "\n")

    save_workspace(
        str(root),
        [{"path": "packages/core", "name": "core", "releasable": "alpha"}],
        releasables=[Releasable(name="alpha")],
    )
    write_releasable_version(str(root), "alpha", "1.0.0")
    changes_dir = get_releasable_changes_dir(str(root), "alpha")
    os.makedirs(changes_dir, exist_ok=True)
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
        f.write("")
    rel_dir = get_releasable_dir(str(root), "alpha")
    with open(os.path.join(rel_dir, "config.json"), "w") as f:
        json.dump(releasable_config or {}, f)
        f.write("\n")

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    _git(root, "tag", "alpha@v1.0.0")

    (core / "feature.txt").write_text("new feature\n")
    _git(root, "add", "packages/core/feature.txt")
    _git(root, "commit", "-q", "-m", "add feature")
    feature_sha = _git_head(root)

    entry = {
        "commits": [feature_sha],
        "user_facing": True,
        "description": "**New feature.** A shiny new thing.",
        "type": "feature",
    }
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
        f.write(json.dumps(entry) + "\n")
    _git(root, "add", os.path.relpath(changes_dir, str(root)))
    _git(root, "commit", "-q", "-m", "changelog: add feature entry",
         "--trailer", "Autogenerated: true")

    return core


def _setup_standalone_npm(repo, scaffolded):
    """Standalone npm project at v1.0.0 with one covered unreleased commit."""
    (repo / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}, indent=2) + "\n"
    )
    (repo / "CHANGELOG.md").write_text(
        "<!-- Generated by rlsbl from .rlsbl/changes/ -- do not edit -->\n\n"
        "# Changelog\n\n## 1.0.0\n\n- Initial release.\n"
    )
    changes_dir = repo / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False, "targets": ["npm"]}) + "\n"
    )
    if scaffolded:
        # Scaffold metadata marks the project as actually scaffolded
        (repo / ".rlsbl" / "hashes.json").write_text("{}\n")
        (repo / ".rlsbl" / "version").write_text("0.0.1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "v1.0.0")

    (repo / "feature.txt").write_text("new feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "add feature")
    feature_sha = _git_head(repo)

    entry = {
        "commits": [feature_sha],
        "user_facing": True,
        "description": "**New feature.** A shiny new thing.",
        "type": "feature",
    }
    (changes_dir / "unreleased.jsonl").write_text(json.dumps(entry) + "\n")
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog: add feature entry",
         "--trailer", "Autogenerated: true")


def _run_standalone_release(repo):
    ctx = ProjectContext(
        project_root=Path(str(repo)),
        workspace_root=None,
        config={"private": False, "pipelines": {}},
    )
    patches = _release_patches()
    for p in patches:
        p.start()
    try:
        run_cmd(_rc(), {"yes": True, "quiet": True, "skip-lock": True}, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


@pytest.fixture
def mock_git_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@test.local")
    _git(tmp_path, "config", "user.name", "Test")
    return tmp_path


# ---------------------------------------------------------------------------
# 1.3a: .rlsbl/version marker
# ---------------------------------------------------------------------------


class TestVersionMarker:

    def test_releasable_release_writes_no_member_marker(self, tmp_project):
        """A releasable release never writes the representative member's
        .rlsbl/version marker (member dirs are not scaffolded)."""
        core = _setup_releasable_workspace(tmp_project)
        _run_release(core, tmp_project)
        assert not (core / ".rlsbl" / "version").exists(), (
            "releasable release must not write a member .rlsbl/version marker"
        )

    def test_unscaffolded_standalone_gets_no_marker(self, mock_git_repo):
        """A standalone project without scaffold metadata gets no marker."""
        _setup_standalone_npm(mock_git_repo, scaffolded=False)
        _run_standalone_release(mock_git_repo)
        assert not (mock_git_repo / ".rlsbl" / "version").exists(), (
            ".rlsbl/version must only be written for scaffolded projects"
        )

    def test_scaffolded_standalone_marker_refreshed(self, mock_git_repo):
        """A scaffolded standalone project (scaffold metadata present) still
        gets its marker refreshed to the current rlsbl version."""
        from rlsbl import __version__ as rlsbl_ver

        _setup_standalone_npm(mock_git_repo, scaffolded=True)
        _run_standalone_release(mock_git_repo)
        marker = mock_git_repo / ".rlsbl" / "version"
        assert marker.is_file()
        assert marker.read_text().strip() == rlsbl_ver


# ---------------------------------------------------------------------------
# 1.3b: private representative
# ---------------------------------------------------------------------------


class TestPrivateRepresentative:

    def test_private_representative_manifest_untouched(self, tmp_project):
        """A private representative's package.json is untouched by the
        version bump and by ecosystem keyword tagging, while the releasable
        version file still updates."""
        core = _setup_releasable_workspace(
            tmp_project,
            member_config={"private": True, "targets": ["npm"], "pipelines": {}},
        )
        before = (core / "package.json").read_text()

        # Force ecosystem tagging ON so the keyword-skip path is exercised.
        _run_release(
            core, tmp_project,
            extra_patches=(
                patch("rlsbl.commands.release.should_tag", return_value=True),
            ),
        )

        assert (core / "package.json").read_text() == before, (
            "private representative's manifest must not be bumped or "
            "keyword-tagged"
        )
        assert read_releasable_version(str(tmp_project), "alpha") == "1.0.1"

    def test_public_representative_manifest_still_bumped(self, tmp_project):
        """A non-private representative keeps the existing behavior."""
        core = _setup_releasable_workspace(tmp_project)
        _run_release(core, tmp_project)
        pkg = json.loads((core / "package.json").read_text())
        assert pkg["version"] == "1.0.1"


# ---------------------------------------------------------------------------
# 1.3c: clean_stale_exclusions targets the releasable config
# ---------------------------------------------------------------------------


class TestCleanStaleExclusionsReleasable:

    def test_stale_exclusion_cleaned_from_releasable_config(self, tmp_project):
        """In releasable mode, stale batch exclusions referencing
        unreleased.jsonl are cleaned from the RELEASABLE config.json (the
        file `changelog add --allow-batch` writes to)."""
        releasable_config = {
            "batch_limits": {
                "exclusions": [
                    {
                        "reason": "big batch",
                        "entries": [{"version": "unreleased", "line": 1}],
                    },
                ],
            },
        }
        core = _setup_releasable_workspace(
            tmp_project, releasable_config=releasable_config,
        )
        _run_release(core, tmp_project)

        rel_config_path = os.path.join(
            get_releasable_dir(str(tmp_project), "alpha"), "config.json",
        )
        with open(rel_config_path) as f:
            cfg = json.load(f)
        exclusions = cfg.get("batch_limits", {}).get("exclusions", [])
        assert exclusions == [], (
            "stale unreleased exclusions in the releasable config must be "
            "cleaned during release finalization"
        )
        # And the cleanup is committed (clean tree)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert status == ""
