"""The mirror requirement: a registry-less ecosystem needs a mirror to exist.

Some ecosystems have no package registry at all -- an SPM consumer names the
package's git URL and a version requirement, and the resolver reads plain
``vX.Y.Z`` tags off that repository. A monorepo member of that kind is
unconsumable as it stands: the workspace's tags carry a package prefix the
resolver does not understand, and the repository root is not the package. A
standalone mirror is what makes it consumable, so the binding is REQUIRED
rather than advisory, and the requirement is a check.
"""

import json

import pytest

from conftest import make_workspace

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.targets import targets_consumed_by_repository_url
from rlsbl.workspace import load_releasables, load_workspace


def _ctx(root):
    from pathlib import Path

    projects = load_workspace(str(root))
    return WorkspaceCheckContext(
        project_root=Path(str(root)),
        workspace_root=Path(str(root)),
        config={},
        projects=projects,
        graph=None,
        releasables=load_releasables(str(root), projects),
    )


def _run(root):
    return app._check_defs["mirror-required"].impl(_ctx(root))


def _swift_member(root, name="uikit", *, mirror=None):
    member = root / name
    member.mkdir(parents=True, exist_ok=True)
    (member / "Package.swift").write_text(f'let package = Package(name: "{name}")\n')
    (member / "VERSION").write_text("0.1.0\n")
    (member / ".rlsbl").mkdir(exist_ok=True)
    (member / ".rlsbl" / "config.json").write_text(
        json.dumps({"targets": ["swift"], "publish_mode": "none"}) + "\n"
    )
    releasable = {"name": name}
    if mirror:
        releasable["subtree_remote"] = mirror
    make_workspace(
        root,
        [{"path": name, "name": name, "releasable": name}],
        releasables=[releasable],
    )


def _npm_member(root, name="widget"):
    member = root / name
    member.mkdir(parents=True, exist_ok=True)
    (member / "package.json").write_text(
        json.dumps({"name": name, "version": "0.1.0"}) + "\n"
    )
    (member / ".rlsbl").mkdir(exist_ok=True)
    (member / ".rlsbl" / "config.json").write_text(
        json.dumps({"targets": ["npm"], "publish_mode": "none"}) + "\n"
    )
    make_workspace(
        root,
        [{"path": name, "name": name, "releasable": name}],
        releasables=[{"name": name}],
    )


class TestScope:
    def test_the_scope_is_the_registry_less_targets(self):
        """The check's scope is derived, never a target-name list here."""
        from rlsbl.checks import targets_for_check

        assert targets_for_check("mirror-required") == (
            targets_consumed_by_repository_url()
        )

    def test_registered_on_the_app(self):
        assert "mirror-required" in app._check_defs


class TestVerdicts:
    def test_unmirrored_swift_member_fails(self, tmp_path):
        _swift_member(tmp_path)
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "uikit" in result.message or "mirror" in result.message

    def test_mirrored_swift_member_passes(self, tmp_path):
        _swift_member(tmp_path, mirror="https://example.com/o/uikit.git")
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_registry_published_member_is_not_in_scope(self, tmp_path):
        _npm_member(tmp_path)
        result = _run(tmp_path)
        assert result.status == "skip"


class TestMessage:
    def test_the_failure_says_where_the_binding_goes(self, tmp_path):
        _swift_member(tmp_path)
        result = _run(tmp_path)
        assert result.status == "fail"
        joined = "\n".join(p.text for p in result.problems)
        assert "subtree_remote" in joined
        assert "uikit" in joined
        assert "[[releasables]]" in joined


def test_the_sync_command_no_longer_warns_about_swift(tmp_path):
    """The advisory warning is replaced by the check; it is not both."""
    import inspect

    from rlsbl.commands.monorepo import sync

    source = inspect.getsource(sync)
    assert "SPM consumers won't be able to resolve" not in source


@pytest.mark.parametrize("target_name", sorted(targets_consumed_by_repository_url()))
def test_registry_less_targets_declare_the_axis(target_name):
    from rlsbl.targets import TARGETS

    assert TARGETS[target_name].consumed_by_repository_url
