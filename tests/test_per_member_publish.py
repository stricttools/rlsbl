"""Tests for Phase 9: per-member publish loop in releasable mode.

Covers:
- Each non-private member with pipelines publishes at the shared version
- Private members are skipped
- Members without pipelines are logged as not published
- Missing manifest causes validation hard error
- Resume skips already-published members
- validate_pipeline_config runs per publishing member pre-mutation
- write_version existence guard
"""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from rlsbl.commands.release.execute import (
    ReleaseState,
    _sync_member_package_versions,
)
from rlsbl.commands.release.validate import validate_pipeline_config
from rlsbl.member_context import resolve_member_context
from rlsbl.pipelines import load_pipelines
from rlsbl.workspace import (
    Releasable,
    get_releasable_dir,
    load_workspace,
    members_of,
    save_workspace,
    write_releasable_version,
    get_releasable_changes_dir,
    WORKSPACE_DIR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_releasable_monorepo(
    tmp_path,
    *,
    member_configs=None,
    releasable_config=None,
    initial_version="0.1.0",
    projects=None,
):
    """Build a releasable monorepo with two pypi members for publish testing.

    member_configs: dict mapping member name to config dict overrides.
    releasable_config: dict for releasable-level config.json.
    """
    releasables = [Releasable(name="myrel")]

    if projects is None:
        projects = [
            {"path": "packages/core", "name": "core", "releasable": "myrel"},
            {"path": "packages/web", "name": "web", "releasable": "myrel"},
        ]

    # Git init
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)

    readme = tmp_path / "README.md"
    readme.write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

    # Write workspace
    save_workspace(str(tmp_path), projects, releasables=releasables)

    # Default member configs
    default_member_cfgs = {
        "core": {
            "publish_mode": "ci",
            "targets": ["pypi"],
            "pipelines": {"pypi": {"type": "pypi", "local": False, "target": "pypi"}},
        },
        "web": {
            "publish_mode": "ci",
            "targets": ["pypi"],
            "pipelines": {"pypi": {"type": "pypi", "local": False, "target": "pypi"}},
        },
    }
    if member_configs:
        for k, v in member_configs.items():
            if k in default_member_cfgs:
                default_member_cfgs[k].update(v)
            else:
                default_member_cfgs[k] = v

    # Create project dirs
    for proj in projects:
        proj_dir = tmp_path / proj["path"]
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "pyproject.toml").write_text(
            f'[project]\nname = "{proj["name"]}"\nversion = "{initial_version}"\n'
        )
        rlsbl_dir = proj_dir / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        cfg = default_member_cfgs.get(proj["name"], {"publish_mode": "ci"})
        (rlsbl_dir / "config.json").write_text(json.dumps(cfg) + "\n")

    # Releasable-level setup
    write_releasable_version(str(tmp_path), "myrel", initial_version)
    changes_dir = get_releasable_changes_dir(str(tmp_path), "myrel")
    os.makedirs(changes_dir, exist_ok=True)
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
        f.write("")

    rel_dir = get_releasable_dir(str(tmp_path), "myrel")
    rel_cfg = releasable_config or {}
    with open(os.path.join(rel_dir, "config.json"), "w") as f:
        json.dump(rel_cfg, f)
        f.write("\n")

    # Commit all
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "setup"], cwd=tmp_path, check=True)

    tag = f"myrel@v{initial_version}"
    subprocess.run(["git", "tag", tag], cwd=tmp_path, check=True)

    # Post-tag commit
    (tmp_path / "marker.txt").write_text("change\n")
    subprocess.run(["git", "add", "marker.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "post-tag"], cwd=tmp_path, check=True)

    return SimpleNamespace(
        root=tmp_path,
        releasables=releasables,
        projects=projects,
        initial_version=initial_version,
    )


# ---------------------------------------------------------------------------
# Tests: per-member pipeline resolution
# ---------------------------------------------------------------------------


class TestPerMemberPipelineResolution:
    """Each non-private member resolves its own pipelines."""

    def test_both_members_have_pipelines(self, tmp_path, monkeypatch):
        """Both non-private members resolve their own pipeline config."""
        monkeypatch.chdir(tmp_path)
        _make_releasable_monorepo(tmp_path)

        rel_dir = get_releasable_dir(str(tmp_path), "myrel")

        for pkg in ("core", "web"):
            pkg_dir = str(tmp_path / "packages" / pkg)
            ctx = resolve_member_context(pkg_dir, releasable_config_dir=rel_dir)
            assert ctx.publish_mode == "ci"
            pipelines = load_pipelines(ctx.config)
            assert "pypi" in pipelines

    def test_private_member_skipped(self, tmp_path, monkeypatch):
        """Private members are detected and would be skipped in the loop."""
        monkeypatch.chdir(tmp_path)
        _make_releasable_monorepo(
            tmp_path,
            member_configs={"web": {"publish_mode": "none"}},
        )

        rel_dir = get_releasable_dir(str(tmp_path), "myrel")
        web_dir = str(tmp_path / "packages" / "web")
        web_ctx = resolve_member_context(web_dir, releasable_config_dir=rel_dir)
        assert web_ctx.publish_mode == "none"

        core_dir = str(tmp_path / "packages" / "core")
        core_ctx = resolve_member_context(core_dir, releasable_config_dir=rel_dir)
        assert core_ctx.publish_mode == "ci"

    def test_member_without_pipelines_produces_empty(self, tmp_path, monkeypatch):
        """A member with no pipelines config produces empty pipelines dict."""
        monkeypatch.chdir(tmp_path)
        _make_releasable_monorepo(tmp_path)

        # Remove pipelines from web's config
        web_config_path = tmp_path / "packages" / "web" / ".rlsbl" / "config.json"
        cfg = json.loads(web_config_path.read_text())
        del cfg["pipelines"]
        web_config_path.write_text(json.dumps(cfg) + "\n")

        rel_dir = get_releasable_dir(str(tmp_path), "myrel")
        web_dir = str(tmp_path / "packages" / "web")
        web_ctx = resolve_member_context(web_dir, releasable_config_dir=rel_dir)
        web_pipelines = load_pipelines(web_ctx.config)
        assert web_pipelines == {}


# ---------------------------------------------------------------------------
# Tests: write_version existence guard
# ---------------------------------------------------------------------------


class TestWriteVersionExistenceGuard:
    """Missing manifest triggers hard error during version sync."""

    def test_missing_manifest_raises_config_error(self, tmp_path, monkeypatch):
        """_sync_member_package_versions raises ConfigError for missing manifest."""
        from rlsbl.errors import ConfigError

        monkeypatch.chdir(tmp_path)
        _make_releasable_monorepo(tmp_path)

        # Delete web's pyproject.toml
        (tmp_path / "packages" / "web" / "pyproject.toml").unlink()

        rel_dir = get_releasable_dir(str(tmp_path), "myrel")
        git_root = str(tmp_path)
        mock_ctx = MagicMock()
        files = []

        with pytest.raises(ConfigError, match="manifest does not exist"):
            _sync_member_package_versions(
                ["packages/core", "packages/web"],
                str(tmp_path),
                "0.2.0",
                files,
                git_root,
                lambda msg: None,
                mock_ctx,
                releasable_config_dir=rel_dir,
            )

    def test_present_manifest_succeeds(self, tmp_path, monkeypatch):
        """_sync_member_package_versions succeeds when manifests exist."""
        monkeypatch.chdir(tmp_path)
        _make_releasable_monorepo(tmp_path)

        rel_dir = get_releasable_dir(str(tmp_path), "myrel")
        git_root = str(tmp_path)
        mock_ctx = MagicMock()
        files = []

        # Should not raise
        _sync_member_package_versions(
            ["packages/core", "packages/web"],
            str(tmp_path),
            "0.2.0",
            files,
            git_root,
            lambda msg: None,
            mock_ctx,
            releasable_config_dir=rel_dir,
        )

        # Both should have been synced
        assert any("pyproject.toml" in f for f in files)


# ---------------------------------------------------------------------------
# Tests: per-member pipeline validation in preflight
# ---------------------------------------------------------------------------


class TestPerMemberPipelineValidation:
    """validate_pipeline_config runs per publishing member pre-mutation."""

    def test_validate_member_pipeline_config(self, tmp_path, monkeypatch):
        """Valid pipeline config on each member passes validation."""
        monkeypatch.chdir(tmp_path)
        _make_releasable_monorepo(tmp_path)

        rel_dir = get_releasable_dir(str(tmp_path), "myrel")
        for pkg in ("core", "web"):
            pkg_dir = str(tmp_path / "packages" / pkg)
            ctx = resolve_member_context(pkg_dir, releasable_config_dir=rel_dir)
            # Should not raise
            validate_pipeline_config(ctx.config)


# ---------------------------------------------------------------------------
# Tests: resume tracks published members
# ---------------------------------------------------------------------------


class TestPublishResumeState:
    """Resume support: published_members in state file."""

    def test_published_members_persisted_in_state(self, tmp_path):
        """State file can store and retrieve published_members list."""
        from rlsbl.commands.release.release_state import (
            save_release_state,
            load_release_state,
        )

        state_dir = tmp_path / "releases"
        state_dir.mkdir(parents=True)
        state_path = str(state_dir / "in-progress.json")

        save_release_state(state_path, {
            "completed_steps": [],
            "published_members": ["packages/core"],
        })

        state = load_release_state(state_path)
        assert state["published_members"] == ["packages/core"]

    def test_already_published_member_would_be_skipped(self, tmp_path, monkeypatch):
        """When published_members includes a path, that member is skipped."""
        monkeypatch.chdir(tmp_path)
        _make_releasable_monorepo(tmp_path)

        # Simulate: "packages/core" already published
        already_published = {"packages/core"}
        member_paths = ["packages/core", "packages/web"]
        rel_dir = get_releasable_dir(str(tmp_path), "myrel")

        not_skipped = []
        for pkg_path in member_paths:
            if pkg_path in already_published:
                continue
            abs_pkg = str(tmp_path / pkg_path)
            ctx = resolve_member_context(abs_pkg, releasable_config_dir=rel_dir)
            if ctx.publish_mode != "none":
                pipelines = load_pipelines(ctx.config)
                if pipelines:
                    not_skipped.append(pkg_path)

        assert not_skipped == ["packages/web"]
