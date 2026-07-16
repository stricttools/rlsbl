"""Tests for the shared member-resolution helper (rlsbl.member_context).

Four code paths must resolve a releasable member's effective config and
targets identically:

- _sync_member_package_versions (release execute)
- collect_companion_tags (release execute)
- the go-companion-tags workspace check
- resolve_target_paths (primary registry/path resolution)

Historically only version sync applied releasable-level config inheritance,
so a member whose ``private: false`` and ``targets`` live ONLY at the
releasable level was version-synced but skipped for companion tags, skipped
by the check, and had its primary path resolved without inheritance.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conftest import run_git
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.member_context import MemberContext, resolve_member_context
from rlsbl.commands.release.execute import (
    _sync_member_package_versions,
    collect_companion_tags,
    resolve_target_paths,
)
from rlsbl.workspace import (
    Releasable,
    WorkspaceProject,
    get_releasable_dir,
    write_releasable_version,
)


def _make_go_member_monorepo(tmp_path, member_config, releasable_config):
    """Create a git monorepo with one Go member and a releasable config dir.

    ``member_config`` is written to the member's .rlsbl/config.json
    (pass None to omit the file entirely). ``releasable_config`` is written
    to the releasable's config.json. Returns (repo, member_dir, rel_dir).
    """
    repo = tmp_path / "workspace"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")

    go_pkg = repo / "packages" / "golib"
    go_pkg.mkdir(parents=True)
    (go_pkg / "go.mod").write_text("module github.com/test/golib\n\ngo 1.21\n")
    (go_pkg / "VERSION").write_text("1.0.0\n")
    if member_config is not None:
        (go_pkg / ".rlsbl").mkdir()
        (go_pkg / ".rlsbl" / "config.json").write_text(json.dumps(member_config))

    ws_dir = repo / ".rlsbl-monorepo"
    ws_dir.mkdir()
    (ws_dir / "workspace.toml").write_text("")
    write_releasable_version(str(repo), "myrel", "1.0.0")
    rel_dir = get_releasable_dir(str(repo), "myrel")
    with open(os.path.join(rel_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(releasable_config, f)

    (repo / "README.md").write_text("test\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "initial")

    return repo, go_pkg, rel_dir


# ---------------------------------------------------------------------------
# Unit tests: resolve_member_context
# ---------------------------------------------------------------------------


class TestResolveMemberContext:
    def test_inherits_private_and_targets_from_releasable(self, tmp_path):
        """Releasable-level private:false and targets apply to a member with an empty config."""
        _repo, member, rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={},
            releasable_config={"publish_mode": "ci", "targets": ["go"]},
        )
        ctx = resolve_member_context(str(member), releasable_config_dir=rel_dir)
        assert ctx.publish_mode == "ci"
        assert [e.name for e in ctx.targets] == ["go"]
        assert ctx.targets[0].path == str(member)

    def test_member_config_overrides_releasable(self, tmp_path):
        """Per-package private:true wins over releasable-level private:false."""
        _repo, member, rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={"publish_mode": "none"},
            releasable_config={"publish_mode": "ci", "targets": ["go"]},
        )
        ctx = resolve_member_context(str(member), releasable_config_dir=rel_dir)
        assert ctx.publish_mode == "none"

    def test_publish_mode_required_when_unset_everywhere(self, tmp_path):
        """No publish_mode key anywhere -> required-read hard error (no default)."""
        from rlsbl.errors import ConfigError

        _repo, member, rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={},
            releasable_config={"targets": ["go"]},
        )
        ctx = resolve_member_context(str(member), releasable_config_dir=rel_dir)
        with pytest.raises(ConfigError):
            _ = ctx.publish_mode

    def test_without_releasable_config_dir_requires_publish_mode(self, tmp_path):
        """No inheritance and no per-package publish_mode -> hard error."""
        from rlsbl.errors import ConfigError

        _repo, member, _rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config=None,
            releasable_config={"publish_mode": "ci", "targets": ["go"]},
        )
        ctx = resolve_member_context(str(member))
        with pytest.raises(ConfigError):
            _ = ctx.publish_mode
        assert any(e.name == "go" for e in ctx.targets)  # auto-detected

    def test_targets_are_lazy(self, tmp_path):
        """Target detection must not run until .targets is accessed.

        A private member with a config file but no targets key anywhere would
        make detect_targets raise ConfigError; consumers that skip private
        members before touching targets must not be affected.
        """
        _repo, member, rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={"publish_mode": "none"},
            releasable_config={},  # no targets key at releasable level either
        )
        ctx = resolve_member_context(str(member), releasable_config_dir=rel_dir)
        # Constructing the context and checking privacy must not raise
        assert ctx.publish_mode == "none"
        # Accessing targets is what raises
        from rlsbl.targets import ConfigError

        with pytest.raises(ConfigError):
            _ = ctx.targets


# ---------------------------------------------------------------------------
# collect_companion_tags with releasable-level inheritance
# ---------------------------------------------------------------------------


class TestCollectCompanionTagsInheritance:
    def test_releasable_level_private_false_produces_companion_tag(self, tmp_path):
        """A member private/targeted only at the releasable level gets a companion tag."""
        repo, _member, rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={},
            releasable_config={"publish_mode": "ci", "targets": ["go"]},
        )
        result = collect_companion_tags(
            ["packages/golib"], str(repo), "1.0.0", "myrel@v1.0.0",
            releasable_config_dir=rel_dir,
        )
        assert result == ["packages/golib/v1.0.0"]

    def test_member_private_true_still_skipped(self, tmp_path):
        """Per-package private:true overrides releasable private:false."""
        repo, _member, rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={"publish_mode": "none"},
            releasable_config={"publish_mode": "ci", "targets": ["go"]},
        )
        result = collect_companion_tags(
            ["packages/golib"], str(repo), "1.0.0", "myrel@v1.0.0",
            releasable_config_dir=rel_dir,
        )
        assert result == []


# ---------------------------------------------------------------------------
# go-companion-tags check with releasable-level inheritance
# ---------------------------------------------------------------------------


def _get_check(name):
    from rlsbl.checks.workspace import register_workspace_checks

    app = MagicMock()
    checks = {}

    def fake_check(check_name):
        def decorator(fn):
            checks[check_name] = fn
            return fn
        return decorator

    app.check = fake_check
    register_workspace_checks(app)
    return checks[name]


class TestGoCompanionTagsCheckInheritance:
    def _ctx(self, root, projects, releasables):
        return WorkspaceCheckContext(
            project_root=Path(root),
            workspace_root=Path(root),
            config={},
            projects=projects,
            releasables=releasables,
        )

    def test_check_sees_member_published_only_at_releasable_level(self, tmp_path):
        """The check must apply releasable inheritance to the private flag.

        Member config is empty; private:false and targets live only at the
        releasable level. The companion tag is missing, so the check must
        WARN (not skip the member as private).
        """
        repo, _member, _rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={},
            releasable_config={"publish_mode": "ci", "targets": ["go"]},
        )
        check_fn = _get_check("go-companion-tags")
        proj = WorkspaceProject({
            "name": "golib",
            "path": "packages/golib",
            "releasable": "myrel",
        })
        ctx = self._ctx(str(repo), [proj], [Releasable(name="myrel")])
        result = check_fn(ctx)
        assert result.status == "warn"
        assert "missing companion tag" in result.details[0]

    def test_check_passes_when_tag_exists(self, tmp_path):
        repo, _member, _rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={},
            releasable_config={"publish_mode": "ci", "targets": ["go"]},
        )
        run_git(repo, "tag", "packages/golib/v1.0.0")
        check_fn = _get_check("go-companion-tags")
        proj = WorkspaceProject({
            "name": "golib",
            "path": "packages/golib",
            "releasable": "myrel",
        })
        ctx = self._ctx(str(repo), [proj], [Releasable(name="myrel")])
        result = check_fn(ctx)
        assert result.status == "pass"


# ---------------------------------------------------------------------------
# All paths agree on the member set
# ---------------------------------------------------------------------------


class TestMemberSetAgreement:
    def test_sync_companions_and_check_agree(self, tmp_path):
        """Version sync, companion tags, and the check all treat the member as published."""
        repo, member, rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={},
            releasable_config={"publish_mode": "ci", "targets": ["go"]},
        )

        # 1. Version sync considers the member published (already worked)
        files_to_commit = []
        _sync_member_package_versions(
            ["packages/golib"], str(repo), "2.0.0",
            files_to_commit, str(repo), lambda m: None, ctx=None,
            releasable_config_dir=rel_dir,
        )
        assert (member / "VERSION").read_text().strip() == "2.0.0"
        assert files_to_commit  # VERSION was recorded for commit

        # 2. Companion tag collection agrees
        companions = collect_companion_tags(
            ["packages/golib"], str(repo), "2.0.0", "myrel@v2.0.0",
            releasable_config_dir=rel_dir,
        )
        assert companions == ["packages/golib/v2.0.0"]

        # 3. The go-companion-tags check agrees (member is checked, tag missing)
        write_releasable_version(str(repo), "myrel", "2.0.0")
        check_fn = _get_check("go-companion-tags")
        proj = WorkspaceProject({
            "name": "golib",
            "path": "packages/golib",
            "releasable": "myrel",
        })
        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=[proj],
            releasables=[Releasable(name="myrel")],
        )
        result = check_fn(ctx)
        assert result.status == "warn"
        assert "packages/golib/v2.0.0" in result.details[0]


# ---------------------------------------------------------------------------
# resolve_target_paths with releasable-level targets
# ---------------------------------------------------------------------------


class TestResolveTargetPathsInheritance:
    def test_primary_path_respects_releasable_targets(self, tmp_path):
        """Releasable-level targets drive primary path resolution.

        The member's own config has no targets key; without inheritance,
        detect_targets raises ConfigError for a config file lacking targets.
        """
        _repo, member, rel_dir = _make_go_member_monorepo(
            tmp_path,
            member_config={"publish_mode": "ci"},
            releasable_config={"targets": ["go"]},
        )
        result = resolve_target_paths(str(member), releasable_config_dir=rel_dir)
        assert result == {"go": str(member)}

    def test_standalone_unchanged(self, tmp_path):
        """Without a releasable config dir, behavior is unchanged."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".rlsbl").mkdir()
        (proj / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["go"]})
        )
        (proj / "go.mod").write_text("module example.com/x\n")
        result = resolve_target_paths(str(proj))
        assert result == {"go": str(proj)}
