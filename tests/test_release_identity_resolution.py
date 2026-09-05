"""``_resolve_release_identity``: one project, one target, one context, one record dir.

The resolution behind the ref checks and ``rlsbl release reconcile``. Its
workspace branch has three shapes, and the third -- a workspace whose
``[[releasables]]`` section is empty, so no member belongs to a releasable --
is the one nothing else in the suite covers. It is reachable: an empty
releasables section is a workspace that has been initialized and has not
declared a releasable yet, which the loader accepts.

What must hold in every shape: the release-archive directory the caller is
handed and the ``releases_dirs`` the returned context reads ``shipped_as``
from are the SAME directory. They are derived on different paths, and a
disagreement would have one half of a ref question answered about one project
and the other half about another.
"""

import json
import os
from pathlib import Path

from conftest import make_workspace

from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.checks._common import _resolve_release_identity
from rlsbl.workspace import load_releasables, load_workspace


def _member(repo, path, name):
    member = repo / path
    (member / ".rlsbl").mkdir(parents=True)
    (member / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n",
        encoding="utf-8",
    )
    (member / "pyproject.toml").write_text(
        '[project]\nname = "%s"\nversion = "0.1.0"\n' % name, encoding="utf-8",
    )
    return member


def _ctx(repo, member):
    projects = load_workspace(str(repo))
    return WorkspaceCheckContext(
        project_root=Path(str(member)),
        workspace_root=Path(str(repo)),
        config={},
        projects=projects,
        graph=None,
        releasables=load_releasables(str(repo), projects),
    )


def test_a_member_of_a_releasable_reads_the_releasables_own_state(tmp_path):
    repo = tmp_path / "ws"
    member = _member(repo, "packages/core", "core")
    (repo / ".rlsbl-monorepo" / "releasables" / "core" / "changes").mkdir(parents=True)
    make_workspace(
        repo,
        [{"path": "packages/core", "name": "core", "releasable": "core"}],
        releasables=[{"name": "core"}],
    )

    _target, ref_ctx, releases_dir = _resolve_release_identity(_ctx(repo, member))

    assert ref_ctx.releasable_name == "core"
    assert ref_ctx.releases_dirs == (releases_dir,), (
        "the archives the caller reads and the archives the context reads "
        "shipped_as from must be the same directory"
    )


def test_a_workspace_with_no_releasables_reads_the_members_own_state(tmp_path):
    """The empty-releasables branch: the member IS its own release state."""
    repo = tmp_path / "ws"
    member = _member(repo, "packages/tool", "tool")
    (member / ".rlsbl" / "changes").mkdir(parents=True)
    make_workspace(
        repo,
        [{"path": "packages/tool", "name": "tool", "releasable": False}],
        releasables=[],
    )

    ctx = _ctx(repo, member)
    assert ctx.releasables == [], "the fixture must exercise the empty branch"

    _target, ref_ctx, releases_dir = _resolve_release_identity(ctx)

    assert ref_ctx.releasable_name is None
    assert ref_ctx.monorepo_name == "tool"
    assert os.path.realpath(releases_dir) == os.path.realpath(
        str(member / ".rlsbl" / "releases")
    )
    assert tuple(os.path.realpath(d) for d in ref_ctx.releases_dirs) == (
        os.path.realpath(releases_dir),
    ), (
        "the archives the caller reads and the archives the context reads "
        "shipped_as from must be the same directory"
    )
    assert tuple(os.path.realpath(p) for p in ref_ctx.transition_record_paths) == (
        os.path.realpath(str(member / ".rlsbl" / "transitions.jsonl")),
    )


def test_a_standalone_project_reads_its_own_state(tmp_path):
    project = tmp_path / "solo"
    (project / ".rlsbl").mkdir(parents=True)
    (project / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n",
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        '[project]\nname = "solo"\nversion = "0.1.0"\n', encoding="utf-8",
    )
    from rlsbl.context import ProjectContext

    ctx = ProjectContext(project_root=project, workspace_root=None, config={})
    _target, ref_ctx, releases_dir = _resolve_release_identity(ctx)

    assert tuple(os.path.realpath(d) for d in ref_ctx.releases_dirs) == (
        os.path.realpath(releases_dir),
    )
