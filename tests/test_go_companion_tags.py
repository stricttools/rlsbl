"""Tests for Go companion tag infrastructure.

Covers:
- BaseTarget.companion_tags() returns empty list
- GoTarget.companion_tags() with/without path
- The companion half of ``expected_refs`` (Go members, publish-suppressed,
  non-Go, Go-compatible primary). These tests used to exercise a standalone
  ``collect_companion_tags`` helper in the release flow; the rules it carried
  now live in ``BaseTarget._companion_refs`` and are reached through
  ``expected_refs``, which is the single authority for a version's ref set.
- Integration test with real git repo (creation, push, rollback)
- Validation check (go-companion-tags)
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.targets.base import BaseTarget
from rlsbl.targets.go import GoTarget
from rlsbl.targets.refs import ref_context


def _companions(member_paths, workspace_root, version, primary_tag,
                *, target=None, releasable_config_dir=None):
    """The companion refs ``expected_refs`` derives, for a stated primary tag.

    ``primary_tag_format`` is the honest way to state "this release's primary
    tag is X": the format is the naming authority, and rendering it at *version*
    is what produces the primary the companion rules are evaluated against.
    """
    tgt = target if target is not None else BaseTarget()
    expected = tgt.expected_refs(version, ref_context(
        repo_root=str(workspace_root),
        primary_tag_format=primary_tag.replace(version, "{version}"),
        releasable_name="rel",
        member_package_paths=member_paths,
        releasable_config_dir=releasable_config_dir,
    ))
    assert expected.primary == primary_tag
    return list(expected.companions)


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
# Unit tests: the companion half of expected_refs
# ---------------------------------------------------------------------------


class TestExpectedRefsCompanions:
    """The rules the release flow's companion-tag collector used to carry.

    They now belong to ``expected_refs``, so a ref the release creates and a ref
    a check looks for come from one derivation.
    """

    def test_skips_when_primary_tag_is_go_compatible(self):
        """If the primary tag already contains /v, no companion tags are needed."""
        assert _companions(
            ["packages/mylib"], "/some/workspace", "1.0.0",
            "packages/mylib/v1.0.0",
        ) == []

    def test_not_a_releasable_release_has_no_companions(self, tmp_path):
        """``member_package_paths=None`` -- not empty -- means no companions.

        This is the release flow's own guard: only a releasable release has
        members to ask.
        """
        expected = BaseTarget().expected_refs("1.0.0", ref_context(
            repo_root=str(tmp_path),
        ))
        assert expected.primary == "v1.0.0"
        assert expected.companions == ()

    def test_returns_go_companion_for_non_private_go_member(self, tmp_path):
        """Non-private Go member should produce a companion tag."""
        pkg_dir = tmp_path / "packages" / "mylib"
        pkg_dir.mkdir(parents=True)

        go_entry = MagicMock()
        go_entry.name = "go"

        with patch("rlsbl.config.read_project_config", return_value={"publish_mode": "ci"}), \
             patch("rlsbl.targets.detect_targets", return_value=[go_entry]), \
             patch("rlsbl.targets.TARGETS", {"go": GoTarget()}):
            result = _companions(
                ["packages/mylib"], str(tmp_path), "1.0.0",
                "myreleasable@v1.0.0",
            )
        assert result == ["packages/mylib/v1.0.0"]

    def test_skips_private_members(self, tmp_path):
        """Private packages should not produce companion tags."""
        pkg_dir = tmp_path / "packages" / "internal"
        pkg_dir.mkdir(parents=True)

        mock_detect = MagicMock()

        with patch("rlsbl.config.read_project_config", return_value={"publish_mode": "none"}), \
             patch("rlsbl.targets.detect_targets", mock_detect):
            result = _companions(
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
        with patch("rlsbl.config.read_project_config", return_value={"publish_mode": "ci"}), \
             patch("rlsbl.targets.detect_targets", return_value=[npm_entry]), \
             patch("rlsbl.targets.TARGETS", {"npm": BaseTarget()}):
            result = _companions(
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

        with patch("rlsbl.config.read_project_config", return_value={"publish_mode": "ci"}), \
             patch("rlsbl.targets.detect_targets", return_value=[go_entry1, go_entry2]), \
             patch("rlsbl.targets.TARGETS", {"go": GoTarget()}):
            result = _companions(
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
        with patch("rlsbl.config.read_project_config", return_value={"publish_mode": "ci"}), \
             patch("rlsbl.targets.detect_targets", return_value=[go_entry]), \
             patch("rlsbl.targets.TARGETS", {"go": GoTarget()}):
            result = _companions(
                ["packages/mylib"], str(tmp_path), "1.0.0",
                "packages/mylib/v1.0.0",
            )
        # The /v check catches this first, but even without it, dedup would exclude
        assert result == []

    def test_missing_publish_mode_is_hard_error(self, tmp_path):
        """A member with no publish_mode key is a hard error (required-read)."""
        from rlsbl.errors import ConfigError

        pkg_dir = tmp_path / "packages" / "mylib"
        pkg_dir.mkdir(parents=True)

        # No publish_mode key at all -- required-read raises, never silently skips.
        with patch("rlsbl.config.read_project_config", return_value={}):
            with pytest.raises(ConfigError):
                _companions(
                    ["packages/mylib"], str(tmp_path), "1.0.0",
                    "myreleasable@v1.0.0",
                )

    def test_corrupt_member_config_raises(self, tmp_path):
        """A member with a corrupt config.json must abort ref derivation with a
        hard error, mirroring _sync_member_package_versions_plan.
        Silently skipping would let a release proceed without the member's
        Go proxy tag while version sync aborts on the very same config."""
        from rlsbl.errors import ConfigError

        pkg_dir = tmp_path / "packages" / "mylib"
        (pkg_dir / ".rlsbl").mkdir(parents=True)
        (pkg_dir / ".rlsbl" / "config.json").write_text("{not valid json")

        with pytest.raises(ConfigError):
            _companions(
                ["packages/mylib"], str(tmp_path), "1.0.0",
                "myreleasable@v1.0.0",
            )

    def test_skips_nonexistent_package_dir(self, tmp_path):
        """Non-existent package directories are silently skipped."""
        assert _companions(
            ["packages/nonexistent"], str(tmp_path), "1.0.0",
            "myreleasable@v1.0.0",
        ) == []


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
            "publish_mode": "ci",
            "targets": ["go"],
        }))

        # Initial commit
        (repo / "README.md").write_text("test\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "initial")

        return repo

    def test_companion_tag_creation_and_rollback(self, git_repo):
        """Create companion tags, verify they exist, then roll back."""

        repo = git_repo
        primary_tag = "myreleasable@v1.0.0"

        # Derive the ref set (using real file system)
        companion = _companions(
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
            "publish_mode": "none",
            "targets": ["go"],
        }))

        (repo / "README.md").write_text("test\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "initial")

        companion = _companions(
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
        from conftest import capture_all_checks
        checks = capture_all_checks()
        check_fn = checks["go-companion-tags"]

        ctx = self._make_check_ctx(str(tmp_path), [], [])
        result = check_fn(ctx)
        assert result.status == "skip"

    def test_warns_when_companion_tag_missing(self, tmp_path):
        """Check warns when a Go companion tag is missing."""
        from rlsbl.workspace import Releasable, WorkspaceProject, write_releasable_version

        from conftest import capture_all_checks
        checks = capture_all_checks()
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
            "publish_mode": "ci",
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
        assert "missing companion tag" in result.problems[0].text

    def test_passes_when_companion_tag_exists(self, tmp_path):
        """Check passes when all Go companion tags exist."""
        from rlsbl.workspace import Releasable, WorkspaceProject, write_releasable_version

        from conftest import capture_all_checks
        checks = capture_all_checks()
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
            "publish_mode": "ci",
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
        from rlsbl.workspace import Releasable, WorkspaceProject, write_releasable_version

        from conftest import capture_all_checks
        checks = capture_all_checks()
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
            "publish_mode": "ci",
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

    def test_skips_when_the_primary_tag_is_already_go_compatible(self, tmp_path):
        """A path-scheme primary tag suppresses companions entirely.

        ``expected_refs`` is the single authority for a version's ref set, and
        it answers "no companions" when the primary tag already contains
        ``/v`` -- a release already tagged that way does not duplicate its own
        tag. A check
        that re-derives the member's ``companion_tags`` by hand does not know
        that rule and demands a tag the release never creates separately.
        """
        from rlsbl.workspace import Releasable, WorkspaceProject, write_releasable_version

        from conftest import capture_all_checks
        checks = capture_all_checks()
        check_fn = checks["go-companion-tags"]

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
            "publish_mode": "ci",
            "targets": ["go"],
        }))

        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text("")
        write_releasable_version(str(repo), "myrel", "1.0.0")

        (repo / "README.md").write_text("test\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "initial")

        # The releasable's own tag IS the Go module proxy tag, so no separate
        # companion exists to be missing -- and none is tagged here.
        proj = WorkspaceProject({
            "name": "golib",
            "path": "packages/golib",
            "releasable": "myrel",
        })
        rel = Releasable(name="myrel", tag_format="packages/golib/v{version}")
        ctx = self._make_check_ctx(str(repo), [proj], [rel])

        result = check_fn(ctx)
        assert result.status == "skip"

    def test_fails_on_corrupt_member_config(self, tmp_path):
        """A corrupt member config.json must produce a check FAILURE naming
        the member, not a silent skip.

        The release flow hard-errors on the same corrupt config
        (_sync_member_package_versions_plan propagates ConfigError); the check
        must not silently disagree about the member set.
        """
        from rlsbl.workspace import Releasable, WorkspaceProject, write_releasable_version

        from conftest import capture_all_checks
        checks = capture_all_checks()
        check_fn = checks["go-companion-tags"]

        repo = tmp_path / "workspace"
        repo.mkdir()
        _run_git(repo, "init")
        _run_git(repo, "config", "user.name", "Test")
        _run_git(repo, "config", "user.email", "test@test.com")

        go_pkg = repo / "packages" / "golib"
        go_pkg.mkdir(parents=True)
        (go_pkg / "go.mod").write_text("module github.com/test/golib\n\ngo 1.21\n")
        (go_pkg / ".rlsbl").mkdir()
        # Corrupt JSON: read_project_config raises ConfigError
        (go_pkg / ".rlsbl" / "config.json").write_text("{not valid json")

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
        assert result.status == "fail"
        assert "golib" in result.problems[0].text
        assert "config" in result.problems[0].text.lower()

    def test_fails_when_releasable_version_unreadable(self, tmp_path):
        """A releasable whose version file cannot be read must produce a
        check FAILURE naming the releasable, not a silent skip -- same
        no-silent-skip rule as the member-config-error failure."""
        from rlsbl.workspace import Releasable, WorkspaceProject

        from conftest import capture_all_checks
        checks = capture_all_checks()
        check_fn = checks["go-companion-tags"]

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
            "publish_mode": "ci",
            "targets": ["go"],
        }))

        # Workspace exists but the releasable version file is never written.
        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text("")

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
        assert result.status == "fail"
        assert "myrel" in result.problems[0].text
        assert "version" in result.problems[0].text.lower()

    def test_fails_when_member_targets_unresolvable(self, tmp_path):
        """A member whose config exists but resolves no targets anywhere must
        produce a check FAILURE naming the member (not crash, not skip).

        detect_targets raises ConfigError when a config file is present but
        the merged config has no targets key; the check must surface that
        as a per-member failure.
        """
        from rlsbl.workspace import Releasable, WorkspaceProject, write_releasable_version

        from conftest import capture_all_checks
        checks = capture_all_checks()
        check_fn = checks["go-companion-tags"]

        repo = tmp_path / "workspace"
        repo.mkdir()
        _run_git(repo, "init")
        _run_git(repo, "config", "user.name", "Test")
        _run_git(repo, "config", "user.email", "test@test.com")

        go_pkg = repo / "packages" / "golib"
        go_pkg.mkdir(parents=True)
        (go_pkg / "go.mod").write_text("module github.com/test/golib\n\ngo 1.21\n")
        (go_pkg / ".rlsbl").mkdir()
        # Config exists, no targets key anywhere -> detect_targets raises
        (go_pkg / ".rlsbl" / "config.json").write_text(json.dumps({
            "publish_mode": "ci",
        }))

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
        assert result.status == "fail"
        assert "golib" in result.problems[0].text
