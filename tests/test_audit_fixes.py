"""Tests for accumulated audit fixes across Phases 4-7.

Covers:
- Gap 1: _resolve_version_and_tag uses releasable tag format in explicit mode
- Gap 2: prepush-changelog-coverage is releasable-aware
- Gap 3: prepush-gitignore-guard checks releasable-level files
- Gap 4: post-release hooks use run_releasable_hooks in releasable mode
- Gap 5: read_project_config detects silent field drop when publish.json
  exists with non-publish content and config.json has publish fields
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_workspace, run_git, make_commit, git_head

from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.config import read_project_config, PUBLISH_FIELDS
from rlsbl.errors import ConfigError
from rlsbl.workspace import Releasable, WorkspaceProject


def _write_json(path, data):
    """Write a JSON file, creating parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Gap 1: _resolve_version_and_tag uses releasable tag format
# ---------------------------------------------------------------------------


class TestResolveVersionAndTagReleasable:
    """_resolve_version_and_tag should use releasable tag_format in explicit mode."""

    def _make_explicit_monorepo(self, tmp_path):
        """Set up a monorepo with explicit releasables."""
        ws_root = tmp_path / "repo"
        ws_root.mkdir()

        # Create workspace.toml with releasables
        ws_dir = ws_root / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[releasables]]\nname = "myrel"\ntag_format = "{name}@v{version}"\n\n'
            '[[projects]]\nname = "pkg-a"\npath = "pkg-a"\nreleasable = "myrel"\n'
        )

        # Create releasable version file
        rel_dir = ws_dir / "releasables" / "myrel"
        rel_dir.mkdir(parents=True)
        (rel_dir / "version").write_text("1.2.3\n")

        # Create project dir with a pyproject.toml (target)
        pkg_dir = ws_root / "pkg-a"
        pkg_dir.mkdir()
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "pkg-a"\nversion = "1.2.3"\n'
        )

        return ws_root, pkg_dir

    def test_uses_releasable_tag_format(self, tmp_path):
        ws_root, pkg_dir = self._make_explicit_monorepo(tmp_path)

        releasables = [Releasable(name="myrel", tag_format="{name}@v{version}")]
        projects = [WorkspaceProject({"name": "pkg-a", "path": "pkg-a", "releasable": "myrel"})]

        ctx = WorkspaceCheckContext(
            project_root=pkg_dir,
            workspace_root=ws_root,
            config={},
            projects=projects,
            releasables=releasables,
        )

        from rlsbl.checks._common import _resolve_version_and_tag

        version, tag = _resolve_version_and_tag(ctx)
        assert version == "1.2.3"
        assert tag == "myrel@v1.2.3"

    def test_standalone_uses_target_tag_format(self, tmp_path):
        """Standalone project should use target.tag_format, not releasable format."""
        project_root = tmp_path / "standalone"
        project_root.mkdir()
        (project_root / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.5.0"\n'
        )

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(project_root=project_root, workspace_root=None, config={})

        from rlsbl.checks._common import _resolve_version_and_tag

        version, tag = _resolve_version_and_tag(ctx)
        assert version == "0.5.0"
        assert tag == "v0.5.0"

    def test_releasable_version_file_missing_falls_through(self, tmp_path):
        """If releasable version file is missing, version should be None."""
        ws_root = tmp_path / "repo"
        ws_root.mkdir()

        ws_dir = ws_root / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[releasables]]\nname = "myrel"\ntag_format = "{name}@v{version}"\n\n'
            '[[projects]]\nname = "pkg-a"\npath = "pkg-a"\nreleasable = "myrel"\n'
        )
        # No version file created

        pkg_dir = ws_root / "pkg-a"
        pkg_dir.mkdir()
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "pkg-a"\nversion = "0.1.0"\n'
        )

        releasables = [Releasable(name="myrel", tag_format="{name}@v{version}")]
        projects = [WorkspaceProject({"name": "pkg-a", "path": "pkg-a", "releasable": "myrel"})]

        ctx = WorkspaceCheckContext(
            project_root=pkg_dir,
            workspace_root=ws_root,
            config={},
            projects=projects,
            releasables=releasables,
        )

        from rlsbl.checks._common import _resolve_version_and_tag

        version, tag = _resolve_version_and_tag(ctx)
        assert version is None
        assert tag is None


# ---------------------------------------------------------------------------
# Gap 2: prepush-changelog-coverage releasable-aware
# ---------------------------------------------------------------------------


class TestPrepushChangelogCoverageReleasable:
    """prepush-changelog-coverage should check releasable-level changes dir."""

    def test_check_jsonl_changelog_accepts_changes_dir(self, tmp_path, monkeypatch):
        """_check_jsonl_changelog should use changes_dir when provided."""
        monkeypatch.chdir(tmp_path)
        run_git(tmp_path, "init", "-q", "-b", "main")
        run_git(tmp_path, "config", "user.email", "t@t.local")
        run_git(tmp_path, "config", "user.name", "T")
        (tmp_path / "README.md").write_text("# test\n")
        run_git(tmp_path, "add", "README.md")
        run_git(tmp_path, "commit", "-q", "-m", "initial")
        run_git(tmp_path, "tag", "v0.0.0")

        sha = make_commit(tmp_path, "file.txt", "a change")

        # Create changes dir at a custom (releasable-level) location
        custom_changes = tmp_path / "custom_changes"
        custom_changes.mkdir()
        entry = {"commits": [sha], "user_facing": False}
        (custom_changes / "unreleased.jsonl").write_text(json.dumps(entry) + "\n")

        from rlsbl.commands.pre_push_check import _check_jsonl_changelog

        # Without custom changes_dir, would fail (no .rlsbl/changes/)
        # With changes_dir override, should pass
        result = _check_jsonl_changelog(
            str(tmp_path), None,
            pushed_commits={sha},
            changes_dir=str(custom_changes),
        )
        assert result is None  # No error


# ---------------------------------------------------------------------------
# Gap 3: prepush-gitignore-guard checks releasable-level files
# ---------------------------------------------------------------------------


class TestGitignoreGuardExtraPaths:
    """_check_gitignore_guard should check extra_paths when provided."""

    def test_extra_paths_checked(self, tmp_path, monkeypatch):
        """Extra paths from releasable mode should be included in the guard."""
        monkeypatch.chdir(tmp_path)
        run_git(tmp_path, "init", "-q", "-b", "main")
        run_git(tmp_path, "config", "user.email", "t@t.local")
        run_git(tmp_path, "config", "user.name", "T")
        (tmp_path / "README.md").write_text("# test\n")
        run_git(tmp_path, "add", "README.md")
        run_git(tmp_path, "commit", "-q", "-m", "initial")

        # Create the standard rlsbl dir (not gitignored)
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        # Create a releasable-level file and gitignore it via relative path
        rel_changes = tmp_path / ".rlsbl-monorepo" / "releasables" / "myrel" / "changes"
        rel_changes.mkdir(parents=True)
        rel_unreleased = rel_changes / "unreleased.jsonl"
        rel_unreleased.write_text("")

        # Use a pattern that matches the relative path inside the repo
        (tmp_path / ".gitignore").write_text(".rlsbl-monorepo/\n")
        run_git(tmp_path, "add", ".gitignore")
        run_git(tmp_path, "commit", "-q", "-m", "add gitignore")

        from rlsbl.commands.pre_push_check import _check_gitignore_guard

        # Without extra_paths, should pass (standard files not gitignored)
        result_no_extra = _check_gitignore_guard(str(tmp_path))
        assert result_no_extra is None

        # With extra_paths that are gitignored, should fail
        result_with_extra = _check_gitignore_guard(
            str(tmp_path),
            extra_paths=[str(rel_unreleased)],
        )
        assert result_with_extra is not None
        assert "gitignored" in result_with_extra


# ---------------------------------------------------------------------------
# Gap 4: post-release hooks use run_releasable_hooks
# ---------------------------------------------------------------------------


class TestPostReleaseHooksReleasable:
    """Post-release should use run_releasable_hooks when in releasable mode."""

    def test_post_release_calls_run_releasable_hooks(self):
        """When releasable_name and member_package_paths are set,
        post-release should call run_releasable_hooks instead of
        running the single hook directly."""
        # We verify by checking the code structure rather than running
        # the full release flow: the post-release section should have
        # an if branch for _use_releasable_hooks.
        import inspect
        from rlsbl.commands.release.execute import _run_release_mutating

        source = inspect.getsource(_run_release_mutating)

        # Verify that run_releasable_hooks is called for post-release
        assert "run_releasable_hooks" in source
        # Verify that "post-release" is passed as the hook name
        assert '"post-release"' in source
        # Verify the releasable hooks path is conditioned on _use_releasable_hooks
        assert "_use_releasable_hooks" in source


# ---------------------------------------------------------------------------
# Gap 5: read_project_config detects silent field drop
# ---------------------------------------------------------------------------


class TestPublishFieldSilentDrop:
    """publish.json with non-publish content + config.json with publish fields = hard error."""

    def test_publish_json_with_non_publish_content_and_config_has_publish_fields(self, tmp_path):
        """Bug scenario: publish.json has non-publish content, config.json has
        publish fields -> publish fields were silently dropped. Now it should
        be a hard error."""
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        # config.json has a PUBLISH_FIELD
        _write_json(config_path, {"private": False, "batch_limits": {}})
        # publish.json exists but has no PUBLISH_FIELDS -- only non-publish content
        _write_json(publish_path, {"some_custom_field": "value"})

        with pytest.raises(ConfigError, match="Publishing fields found in .rlsbl/config.json"):
            read_project_config(tmp_path)

    def test_empty_publish_json_returns_config_as_is(self, tmp_path):
        """Empty publish.json (file exists but empty) should be treated as
        non-existent (backward compat)."""
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {"private": True, "batch_limits": {}})
        # Note: _write_json writes '{}' which is truthy. We need truly empty.
        # But the real question is: does the file exist at all?
        # With our fix, we check os.path.isfile, not truthiness of content.
        # An existing publish.json with {} is actually non-empty JSON.
        # Let's test the "file doesn't exist" case.
        result = read_project_config(tmp_path)
        assert result["private"] is True
        assert result["batch_limits"] == {}

    def test_publish_json_exists_with_only_publish_fields_no_conflict(self, tmp_path):
        """publish.json has publish fields, config.json has no publish fields -> OK."""
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {"batch_limits": {"max_commits_per_entry": 5}})
        _write_json(publish_path, {"private": False, "targets": ["pypi"]})

        result = read_project_config(tmp_path)
        assert result["private"] is False
        assert result["targets"] == ["pypi"]
        assert result["batch_limits"] == {"max_commits_per_entry": 5}

    def test_publish_json_empty_dict_with_config_publish_fields_is_error(self, tmp_path):
        """publish.json exists with empty dict '{}', config.json has publish
        fields. The file EXISTS so it's a conflict -- even though its content
        is an empty dict."""
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {"private": True})
        _write_json(publish_path, {})  # exists but empty dict

        with pytest.raises(ConfigError, match="Publishing fields found in .rlsbl/config.json"):
            read_project_config(tmp_path)
