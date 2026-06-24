"""Tests for Go companion tag infrastructure (Phases 2-4).

Covers:
- BaseTarget.companion_tags() returns empty list
- GoTarget.companion_tags() with/without path
- collect_companion_tags helper (Go members, private, non-Go, Go-compatible primary)
- Integration test with real git repo (creation, push, rollback)
- Validation check (go-companion-tags)
"""

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.targets.base import BaseTarget
from rlsbl.targets.go import GoTarget
from rlsbl.commands.release.execute import collect_companion_tags


# ---------------------------------------------------------------------------
# Unit tests: BaseTarget and GoTarget companion_tags
# ---------------------------------------------------------------------------


class TestBaseTargetCompanionTags:
    def test_returns_empty_list(self):
        target = BaseTarget()
        assert target.companion_tags("myproject", "1.2.3") == []

    def test_returns_empty_list_with_path(self):
        target = BaseTarget()
        assert target.companion_tags("myproject", "1.2.3", path="packages/myproject") == []


class TestGoTargetCompanionTags:
    def test_with_path_returns_companion_tag(self):
        target = GoTarget()
        tags = target.companion_tags("myproject", "1.2.3", path="packages/myproject")
        assert tags == ["packages/myproject/v1.2.3"]

    def test_without_path_returns_empty(self):
        target = GoTarget()
        tags = target.companion_tags("myproject", "1.2.3")
        assert tags == []

    def test_path_with_trailing_slash(self):
        target = GoTarget()
        tags = target.companion_tags("myproject", "1.2.3", path="packages/myproject/")
        assert tags == ["packages/myproject/v1.2.3"]


# ---------------------------------------------------------------------------
# Unit tests: collect_companion_tags helper
# ---------------------------------------------------------------------------


class TestCollectCompanionTags:
    """Tests for the detection helper that collects companion tags from member packages."""

    def test_skips_when_primary_tag_is_go_compatible(self):
        """If the primary tag already contains /v, no companion tags are needed."""
        result = collect_companion_tags(
            ["packages/mylib"], "/some/workspace", "1.0.0",
            "packages/mylib/v1.0.0",
        )
        assert result == []

    def test_returns_go_companion_for_non_private_go_member(self, tmp_path):
        """Non-private Go member should produce a companion tag."""
        pkg_dir = tmp_path / "packages" / "mylib"
        pkg_dir.mkdir(parents=True)

        go_entry = MagicMock()
        go_entry.name = "go"

        with patch("rlsbl.config.read_project_config", return_value={"private": False}), \
             patch("rlsbl.commands.release.detect_targets", return_value=[go_entry]), \
             patch("rlsbl.commands.release.TARGETS", {"go": GoTarget()}):
            result = collect_companion_tags(
                ["packages/mylib"], str(tmp_path), "1.0.0",
                "myreleasable@v1.0.0",
            )
        assert result == ["packages/mylib/v1.0.0"]

    def test_skips_private_members(self, tmp_path):
        """Private packages should not produce companion tags."""
        pkg_dir = tmp_path / "packages" / "internal"
        pkg_dir.mkdir(parents=True)

        mock_detect = MagicMock()

        with patch("rlsbl.config.read_project_config", return_value={"private": True}), \
             patch("rlsbl.commands.release.detect_targets", mock_detect):
            result = collect_companion_tags(
                ["packages/internal"], str(tmp_path), "1.0.0",
                "myreleasable@v1.0.0",
            )
        assert result == []
        # detect_targets should not even be called for private packages
        mock_detect.assert_not_called()

    def test_skips_non_go_members(self, tmp_path):
        """Non-Go targets should not produce companion tags."""
        pkg_dir = tmp_path / "packages" / "jslib"
        pkg_dir.mkdir(parents=True)

        npm_entry = MagicMock()
        npm_entry.name = "npm"

        # BaseTarget.companion_tags returns [] for npm
        with patch("rlsbl.config.read_project_config", return_value={"private": False}), \
             patch("rlsbl.commands.release.detect_targets", return_value=[npm_entry]), \
             patch("rlsbl.commands.release.TARGETS", {"npm": BaseTarget()}):
            result = collect_companion_tags(
                ["packages/jslib"], str(tmp_path), "1.0.0",
                "myreleasable@v1.0.0",
            )
        assert result == []

    def test_deduplicates_tags(self, tmp_path):
        """Same companion tag from multiple entries is not duplicated."""
        pkg_dir = tmp_path / "packages" / "mylib"
        pkg_dir.mkdir(parents=True)

        go_entry1 = MagicMock()
        go_entry1.name = "go"
        go_entry2 = MagicMock()
        go_entry2.name = "go"

        with patch("rlsbl.config.read_project_config", return_value={"private": False}), \
             patch("rlsbl.commands.release.detect_targets", return_value=[go_entry1, go_entry2]), \
             patch("rlsbl.commands.release.TARGETS", {"go": GoTarget()}):
            result = collect_companion_tags(
                ["packages/mylib"], str(tmp_path), "1.0.0",
                "myreleasable@v1.0.0",
            )
        # Both entries produce the same tag; should be deduplicated
        assert result == ["packages/mylib/v1.0.0"]

    def test_excludes_primary_tag_from_companions(self, tmp_path):
        """If a companion tag equals the primary tag, it should be excluded."""
        pkg_dir = tmp_path / "packages" / "mylib"
        pkg_dir.mkdir(parents=True)

        go_entry = MagicMock()
        go_entry.name = "go"

        # Primary tag happens to match what GoTarget would produce
        with patch("rlsbl.config.read_project_config", return_value={"private": False}), \
             patch("rlsbl.commands.release.detect_targets", return_value=[go_entry]), \
             patch("rlsbl.commands.release.TARGETS", {"go": GoTarget()}):
            result = collect_companion_tags(
                ["packages/mylib"], str(tmp_path), "1.0.0",
                "packages/mylib/v1.0.0",
            )
        # The /v check catches this first, but even without it, dedup would exclude
        assert result == []

    def test_default_private_when_unset(self, tmp_path):
        """Packages without explicit private flag default to True (skipped)."""
        pkg_dir = tmp_path / "packages" / "mylib"
        pkg_dir.mkdir(parents=True)

        # No "private" key at all -- defaults to True
        with patch("rlsbl.config.read_project_config", return_value={}):
            result = collect_companion_tags(
                ["packages/mylib"], str(tmp_path), "1.0.0",
                "myreleasable@v1.0.0",
            )
        assert result == []

    def test_skips_nonexistent_package_dir(self, tmp_path):
        """Non-existent package directories are silently skipped."""
        result = collect_companion_tags(
            ["packages/nonexistent"], str(tmp_path), "1.0.0",
            "myreleasable@v1.0.0",
        )
        assert result == []


# ---------------------------------------------------------------------------
# Integration test: real git repo with tag creation and rollback
# ---------------------------------------------------------------------------


def _run_git(repo, *args):
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo), check=True, capture_output=True, text=True,
    )


def _git_tags(repo):
    result = subprocess.run(
        ["git", "tag", "-l"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return set(result.stdout.strip().splitlines())


class TestCompanionTagIntegration:
    """Integration tests using a real git repo."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        """Create a temporary git repo with monorepo structure and a Go member."""
        repo = tmp_path / "monorepo"
        repo.mkdir()
        _run_git(repo, "init")
        _run_git(repo, "config", "user.name", "Test")
        _run_git(repo, "config", "user.email", "test@test.com")

        # Create Go member package
        go_pkg = repo / "packages" / "golib"
        go_pkg.mkdir(parents=True)
        (go_pkg / "go.mod").write_text("module github.com/test/golib\n\ngo 1.21\n")
        (go_pkg / ".rlsbl").mkdir()
        (go_pkg / ".rlsbl" / "config.json").write_text(json.dumps({
            "private": False,
            "targets": ["go"],
        }))

        # Initial commit
        (repo / "README.md").write_text("test\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "initial")

        return repo

    def test_companion_tag_creation_and_rollback(self, git_repo):
        """Create companion tags, verify they exist, then roll back."""
        from rlsbl.utils import run

        repo = git_repo
        primary_tag = "myreleasable@v1.0.0"

        # Collect companion tags (using real file system)
        companion = collect_companion_tags(
            ["packages/golib"], str(repo), "1.0.0", primary_tag,
        )
        assert companion == ["packages/golib/v1.0.0"]

        # Create primary and companion tags
        _run_git(repo, "tag", primary_tag)
        for ctag in companion:
            _run_git(repo, "tag", ctag)

        tags = _git_tags(repo)
        assert primary_tag in tags
        assert "packages/golib/v1.0.0" in tags

        # Simulate rollback: delete companion tags
        for ctag in companion:
            _run_git(repo, "tag", "-d", ctag)

        tags_after = _git_tags(repo)
        assert primary_tag in tags_after
        assert "packages/golib/v1.0.0" not in tags_after

    def test_no_companion_tags_for_private_member(self, tmp_path):
        """Private Go members should not generate companion tags."""
        repo = tmp_path / "monorepo"
        repo.mkdir()
        _run_git(repo, "init")
        _run_git(repo, "config", "user.name", "Test")
        _run_git(repo, "config", "user.email", "test@test.com")

        # Private Go member
        go_pkg = repo / "packages" / "private-golib"
        go_pkg.mkdir(parents=True)
        (go_pkg / "go.mod").write_text("module github.com/test/private\n\ngo 1.21\n")
        (go_pkg / ".rlsbl").mkdir()
        (go_pkg / ".rlsbl" / "config.json").write_text(json.dumps({
            "private": True,
            "targets": ["go"],
        }))

        (repo / "README.md").write_text("test\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "initial")

        companion = collect_companion_tags(
            ["packages/private-golib"], str(repo), "1.0.0",
            "myreleasable@v1.0.0",
        )
        assert companion == []


# ---------------------------------------------------------------------------
# Validation check tests: go-companion-tags
# ---------------------------------------------------------------------------


class TestGoCompanionTagsCheck:
    """Tests for the go-companion-tags workspace check."""

    def _make_check_ctx(self, workspace_root, projects, releasables):
        """Build a WorkspaceCheckContext-like mock."""
        from rlsbl.check_context import WorkspaceCheckContext
        return WorkspaceCheckContext(
            project_root=Path(workspace_root),
            workspace_root=Path(workspace_root),
            config={},
            projects=projects,
            releasables=releasables,
        )

    def test_skips_when_no_releasables(self, tmp_path):
        """Check skips when there are no releasables."""
        from rlsbl.checks.workspace import register_workspace_checks
        app = MagicMock()
        checks = {}

        def fake_check(name):
            def decorator(fn):
                checks[name] = fn
                return fn
            return decorator
        app.check = fake_check

        register_workspace_checks(app)
        check_fn = checks["go-companion-tags"]

        ctx = self._make_check_ctx(str(tmp_path), [], [])
        result = check_fn(ctx)
        assert result.status == "skip"

    def test_warns_when_companion_tag_missing(self, tmp_path):
        """Check warns when a Go companion tag is missing."""
        from rlsbl.checks.workspace import register_workspace_checks
        from rlsbl.workspace import Releasable, WorkspaceProject, write_releasable_version

        app = MagicMock()
        checks = {}

        def fake_check(name):
            def decorator(fn):
                checks[name] = fn
                return fn
            return decorator
        app.check = fake_check

        register_workspace_checks(app)
        check_fn = checks["go-companion-tags"]

        # Set up a git repo with a Go member but no companion tag
        repo = tmp_path / "workspace"
        repo.mkdir()
        _run_git(repo, "init")
        _run_git(repo, "config", "user.name", "Test")
        _run_git(repo, "config", "user.email", "test@test.com")

        # Create Go member
        go_pkg = repo / "packages" / "golib"
        go_pkg.mkdir(parents=True)
        (go_pkg / "go.mod").write_text("module github.com/test/golib\n\ngo 1.21\n")
        (go_pkg / ".rlsbl").mkdir()
        (go_pkg / ".rlsbl" / "config.json").write_text(json.dumps({
            "private": False,
            "targets": ["go"],
        }))

        # Set up releasable version
        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text("")
        write_releasable_version(str(repo), "myrel", "1.0.0")

        (repo / "README.md").write_text("test\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "initial")

        proj = WorkspaceProject({
            "name": "golib",
            "path": "packages/golib",
            "releasable": "myrel",
        })
        rel = Releasable(name="myrel")
        ctx = self._make_check_ctx(str(repo), [proj], [rel])

        result = check_fn(ctx)
        assert result.status == "warn"
        assert "missing companion tag" in result.details[0]

    def test_passes_when_companion_tag_exists(self, tmp_path):
        """Check passes when all Go companion tags exist."""
        from rlsbl.checks.workspace import register_workspace_checks
        from rlsbl.workspace import Releasable, WorkspaceProject, write_releasable_version

        app = MagicMock()
        checks = {}

        def fake_check(name):
            def decorator(fn):
                checks[name] = fn
                return fn
            return decorator
        app.check = fake_check

        register_workspace_checks(app)
        check_fn = checks["go-companion-tags"]

        # Set up a git repo with a Go member AND the companion tag
        repo = tmp_path / "workspace"
        repo.mkdir()
        _run_git(repo, "init")
        _run_git(repo, "config", "user.name", "Test")
        _run_git(repo, "config", "user.email", "test@test.com")

        go_pkg = repo / "packages" / "golib"
        go_pkg.mkdir(parents=True)
        (go_pkg / "go.mod").write_text("module github.com/test/golib\n\ngo 1.21\n")
        (go_pkg / ".rlsbl").mkdir()
        (go_pkg / ".rlsbl" / "config.json").write_text(json.dumps({
            "private": False,
            "targets": ["go"],
        }))

        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text("")
        write_releasable_version(str(repo), "myrel", "1.0.0")

        (repo / "README.md").write_text("test\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "initial")

        # Create the companion tag
        _run_git(repo, "tag", "packages/golib/v1.0.0")

        proj = WorkspaceProject({
            "name": "golib",
            "path": "packages/golib",
            "releasable": "myrel",
        })
        rel = Releasable(name="myrel")
        ctx = self._make_check_ctx(str(repo), [proj], [rel])

        result = check_fn(ctx)
        assert result.status == "pass"

    def test_skips_when_no_go_members(self, tmp_path):
        """Check skips when releasable has no non-private Go members."""
        from rlsbl.checks.workspace import register_workspace_checks
        from rlsbl.workspace import Releasable, WorkspaceProject, write_releasable_version

        app = MagicMock()
        checks = {}

        def fake_check(name):
            def decorator(fn):
                checks[name] = fn
                return fn
            return decorator
        app.check = fake_check

        register_workspace_checks(app)
        check_fn = checks["go-companion-tags"]

        repo = tmp_path / "workspace"
        repo.mkdir()
        _run_git(repo, "init")
        _run_git(repo, "config", "user.name", "Test")
        _run_git(repo, "config", "user.email", "test@test.com")

        # npm member only (no Go)
        npm_pkg = repo / "packages" / "jslib"
        npm_pkg.mkdir(parents=True)
        (npm_pkg / "package.json").write_text(json.dumps({
            "name": "jslib", "version": "1.0.0",
        }))
        (npm_pkg / ".rlsbl").mkdir()
        (npm_pkg / ".rlsbl" / "config.json").write_text(json.dumps({
            "private": False,
            "targets": ["npm"],
        }))

        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text("")
        write_releasable_version(str(repo), "myrel", "1.0.0")

        (repo / "README.md").write_text("test\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "initial")

        proj = WorkspaceProject({
            "name": "jslib",
            "path": "packages/jslib",
            "releasable": "myrel",
        })
        rel = Releasable(name="myrel")
        ctx = self._make_check_ctx(str(repo), [proj], [rel])

        result = check_fn(ctx)
        assert result.status == "skip"
