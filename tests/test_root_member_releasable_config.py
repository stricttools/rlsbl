"""A root member's releasable config must reach the checks that read config.

A workspace whose only member is the repository root cannot carry a per-member
``.rlsbl/config.json`` -- ``root-rlsbl-conflict`` refuses a root ``.rlsbl/``
directory -- so everything that member declares (its ``targets``, its
``publish_mode``) is declared once, in its releasable's
``.rlsbl-monorepo/releasables/<name>/config.json``.

Reaching that file needs the member the context was built for. The check
context used to be built without one, so the releasable config directory
resolved to ``None`` and the merged config was never read. Every earlier
workspace hid it: each member had its own ``.rlsbl/config.json``, which is read
from the project root regardless.
"""

import json

import pytest

from conftest import capture_all_checks, workspace_toml

from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE, write_releasable_version


RELEASABLE = "app"


@pytest.fixture
def root_only_workspace(tmp_path):
    """A workspace whose single member is the root, versioned by a releasable.

    The member declares nothing locally: its targets and publish mode live in
    the releasable's config, which is the only place a root member has.
    """
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir()
    (ws_dir / WORKSPACE_FILE).write_text(
        workspace_toml(
            '[[projects]]\npath = "."\nname = "root"\nreleasable = "app"\n',
            releasables=[{"name": RELEASABLE, "tag_format": "v{version}"}],
            root_member="",
        )
    )

    write_releasable_version(str(tmp_path), RELEASABLE, "2.3.4")
    rel_dir = ws_dir / "releasables" / RELEASABLE
    (rel_dir / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]})
    )

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "2.3.4"\n'
    )
    return tmp_path


def _context_for(workspace_root, monkeypatch):
    """Build the check context `rlsbl check --releasable app` builds at the root."""
    import rlsbl

    monkeypatch.chdir(workspace_root)
    monkeypatch.setattr(rlsbl, "_check_releasable", RELEASABLE)
    return rlsbl._check_context_factory()


class TestTheCheckContextCarriesItsMember:

    def test_the_context_names_the_member_it_was_invoked_for(
        self, root_only_workspace, monkeypatch
    ):
        ctx = _context_for(root_only_workspace, monkeypatch)
        assert ctx.project is not None, "the context was built for no member"
        assert ctx.project["name"] == "root"
        assert ctx.project["releasable"] == RELEASABLE

    def test_the_releasable_config_dir_resolves_from_the_context(
        self, root_only_workspace, monkeypatch
    ):
        from rlsbl.targets import resolve_releasable_config_dir_for_ctx

        ctx = _context_for(root_only_workspace, monkeypatch)
        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        assert rel_dir is not None
        assert json.load(open(f"{rel_dir}/config.json"))["targets"] == ["pypi"]

    def test_the_merged_config_carries_the_releasable_keys(
        self, root_only_workspace, monkeypatch
    ):
        ctx = _context_for(root_only_workspace, monkeypatch)
        assert ctx.config["publish_mode"] == "ci"
        assert ctx.config["targets"] == ["pypi"]


class TestVersionConsistencyOnARootMember:

    def test_it_passes_on_a_root_member_configured_by_its_releasable(
        self, root_only_workspace, monkeypatch
    ):
        ctx = _context_for(root_only_workspace, monkeypatch)
        result = capture_all_checks()["version-consistency"](ctx)
        assert result.status == "pass", result.message
        assert "2.3.4" in result.message

    def test_it_reports_a_manifest_that_disagrees_with_the_releasable(
        self, root_only_workspace, monkeypatch
    ):
        (root_only_workspace / "pyproject.toml").write_text(
            '[project]\nname = "app"\nversion = "0.0.1"\n'
        )
        ctx = _context_for(root_only_workspace, monkeypatch)
        result = capture_all_checks()["version-consistency"](ctx)
        assert result.status == "fail", result.message
        assert "0.0.1" in result.message and "2.3.4" in result.message
