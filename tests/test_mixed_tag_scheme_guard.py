"""Mixed monorepo tag-scheme guard (Phase 6.2).

Go publishes monorepo tags path-based (``{path}/v*``); every other target
uses ``{name}@v*``. A single monorepo member dir declaring both a Go target
and an @-style target has no single correct tag prefix -- the old code
silently picked ``detect_targets(...)[0]``, an ordering-dependent wrong
answer. This is now a hard error on two surfaces:

- ``_get_monorepo_tag_prefix`` raises ``ConfigError`` naming the dir and both
  schemes.
- The ``mixed-tag-schemes`` workspace check reports the same condition across
  all members.

A member with a SINGLE scheme (Go-only, or one/many @-style targets) is fine.
A STANDALONE dual-target project is legitimate (all targets share ``v{version}``)
and is untouched -- ``detect_targets`` never errors on it.
"""

import os

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.errors import ConfigError
from rlsbl.commands.monorepo.sync import _get_monorepo_tag_prefix
from rlsbl.targets import detect_targets
from rlsbl.workspace import Releasable

import pytest


def _write_go(dir_path, module="example.com/dual"):
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "go.mod"), "w", encoding="utf-8") as f:
        f.write(f"module {module}\n\ngo 1.21\n")


def _write_package_json(dir_path, name="dual"):
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "package.json"), "w", encoding="utf-8") as f:
        f.write(f'{{"name": "{name}", "version": "0.1.0"}}\n')


def _write_pyproject(dir_path, name="dual"):
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "pyproject.toml"), "w", encoding="utf-8") as f:
        f.write(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')


def _ctx(root, projects, releasables=()):
    return WorkspaceCheckContext(
        project_root=str(root),
        workspace_root=str(root),
        config={},
        projects=projects,
        graph=None,
        releasables=list(releasables),
    )


class TestGetMonorepoTagPrefixGuard:
    """_get_monorepo_tag_prefix is the release-router surface."""

    def test_mixed_go_and_at_style_is_hard_error(self, tmp_path):
        root = str(tmp_path)
        member = os.path.join(root, "dual")
        _write_go(member)
        _write_package_json(member)
        project = {"name": "dual", "path": "dual"}

        with pytest.raises(ConfigError) as exc:
            _get_monorepo_tag_prefix(project, root)

        msg = str(exc.value)
        # Names the dir and both schemes.
        assert "dual" in msg
        assert "path-style (go)" in msg
        assert "@-style (npm)" in msg

    def test_go_only_member_yields_path_style_prefix(self, tmp_path):
        root = str(tmp_path)
        member = os.path.join(root, "gopkg")
        _write_go(member)
        project = {"name": "gopkg", "path": "gopkg"}

        prefix = _get_monorepo_tag_prefix(project, root)
        assert prefix == "gopkg/v"

    def test_at_style_only_member_yields_at_style_prefix(self, tmp_path):
        root = str(tmp_path)
        member = os.path.join(root, "npmpkg")
        _write_package_json(member, name="npmpkg")
        project = {"name": "npmpkg", "path": "npmpkg"}

        prefix = _get_monorepo_tag_prefix(project, root)
        assert prefix == "npmpkg@v"

    def test_multiple_at_style_targets_are_not_mixed(self, tmp_path):
        """pypi + npm both use @-style -- legitimate, no error."""
        root = str(tmp_path)
        member = os.path.join(root, "dual")
        _write_pyproject(member)
        _write_package_json(member)
        project = {"name": "dual", "path": "dual"}

        prefix = _get_monorepo_tag_prefix(project, root)
        assert prefix == "dual@v"


class TestMixedTagSchemesCheck:
    """The mixed-tag-schemes workspace check is the diagnostic surface."""

    def test_severity_is_error(self):
        assert app._check_defs["mixed-tag-schemes"].severity == "error"

    def test_mixed_member_fails(self, mock_git_repo):
        member = os.path.join(str(mock_git_repo), "dual")
        _write_go(member)
        _write_package_json(member)
        result = app._check_defs["mixed-tag-schemes"].impl(
            _ctx(mock_git_repo, [{"name": "dual", "path": "dual"}])
        )
        assert result.status == "fail"
        blob = " ".join(p.text for p in result.problems) + result.message
        assert "dual" in blob
        assert "path-style (go)" in blob
        assert "@-style (npm)" in blob

    def test_go_only_member_passes(self, mock_git_repo):
        member = os.path.join(str(mock_git_repo), "gopkg")
        _write_go(member)
        result = app._check_defs["mixed-tag-schemes"].impl(
            _ctx(mock_git_repo, [{"name": "gopkg", "path": "gopkg"}])
        )
        assert result.status == "pass"

    def test_at_style_only_member_passes(self, mock_git_repo):
        member = os.path.join(str(mock_git_repo), "npmpkg")
        _write_package_json(member, name="npmpkg")
        result = app._check_defs["mixed-tag-schemes"].impl(
            _ctx(mock_git_repo, [{"name": "npmpkg", "path": "npmpkg"}])
        )
        assert result.status == "pass"


class TestStandaloneDualTargetUnaffected:
    """Standalone dual-target detection never errors (detect_targets level)."""

    def test_standalone_pypi_npm_detects_both_without_error(self, tmp_path):
        d = str(tmp_path / "standalone")
        _write_pyproject(d)
        _write_package_json(d)

        entries = detect_targets(d)
        names = {e.name for e in entries}
        assert names == {"pypi", "npm"}


class TestADeclaredTagFormatSettlesTheQuestion:
    """The check asks "which of the two schemes does this member tag under?".

    A releasable that DECLARES its ``tag_format`` has already answered, in the
    one place that decides -- so the member's target mix cannot be ambiguous
    any more.  The shape this matters for is a repository that used to be
    standalone and became a workspace: its root member carries a Go module at
    the repository root plus an npm and a PyPI launcher, and the releasable
    that owns it declares ``tag_format = "v{version}"`` because every one of
    its existing tags is spelled that way.  Without consulting the
    declaration, that member reads as go (path-style) mixed with npm and pypi
    (@-style) and every ``rlsbl check --tag workspace`` run reds.
    """

    def _root_member_with_three_targets(self, root):
        _write_go(str(root), module="example.com/tool")
        _write_package_json(str(root), name="tool")
        _write_pyproject(str(root), name="tool")
        return {"name": "root", "path": ".", "releasable": "tool"}

    def test_root_member_with_a_declared_format_passes(self, mock_git_repo):
        proj = self._root_member_with_three_targets(mock_git_repo)
        result = app._check_defs["mixed-tag-schemes"].impl(
            _ctx(
                mock_git_repo, [proj],
                releasables=[Releasable(name="tool", tag_format="v{version}")],
            )
        )
        assert result.status == "pass"

    def test_the_same_member_with_no_declared_format_still_fails(self, mock_git_repo):
        """Absence of a declaration leaves the question open, so it is asked."""
        member = os.path.join(str(mock_git_repo), "tool")
        _write_go(member, module="example.com/tool")
        _write_package_json(member, name="tool")
        _write_pyproject(member, name="tool")
        proj = {"name": "tool", "path": "tool", "releasable": "tool"}

        result = app._check_defs["mixed-tag-schemes"].impl(
            _ctx(mock_git_repo, [proj], releasables=[Releasable(name="tool")])
        )
        assert result.status == "fail"
        blob = " ".join(p.text for p in result.problems) + result.message
        assert "path-style (go)" in blob
        assert "@-style" in blob

    def test_a_mixed_member_whose_releasable_is_unknown_still_fails(self, mock_git_repo):
        """No releasable resolves -- nothing has answered, so the check asks."""
        member = os.path.join(str(mock_git_repo), "dual")
        _write_go(member)
        _write_package_json(member)
        result = app._check_defs["mixed-tag-schemes"].impl(
            _ctx(mock_git_repo, [{"name": "dual", "path": "dual"}])
        )
        assert result.status == "fail"
