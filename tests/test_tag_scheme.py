"""One exact tag scheme per project: a tag is a project's only if its format renders it.

A tag glob is a listing aid, not an ownership rule: ``kernel/v*`` also matches
``kernel/vulkan/v0.1.0``, the tag of a member nested inside ``kernel``, and the
root releasable's ``v*`` matches ``vendor/v0.1.0``. A tag belongs to a scheme
exactly when rendering the scheme's format at the version the tag carries
yields the tag itself.
"""

import pytest

from conftest import make_nested_workspace, run_git
from rlsbl.tag_glob import TagMode, TagScheme


# ---------------------------------------------------------------------------
# The scheme object
# ---------------------------------------------------------------------------


class TestTagScheme:
    def test_render_and_list_glob(self):
        scheme = TagScheme.from_format("{name}@v{version}", "www")
        assert scheme.render("1.2.3") == "www@v1.2.3"
        assert scheme.list_glob() == "www@v*"

    def test_a_nested_members_tag_is_not_the_parents(self):
        kernel = TagScheme.from_format("kernel/v{version}", "kernel")
        assert kernel.owns("kernel/v0.1.0")
        assert not kernel.owns("kernel/vulkan/v0.1.0")

    def test_the_root_scheme_owns_no_go_path_tag(self):
        root = TagScheme.from_format("v{version}", "root")
        assert root.owns("v0.1.0")
        assert not root.owns("vendor/v0.1.0")

    def test_a_non_version_tail_is_not_owned(self):
        scheme = TagScheme.from_format("{name}@v{version}", "lib")
        assert not scheme.owns("lib@vlatest")
        assert not scheme.owns("lib@v1.2")

    def test_prerelease_ownership_follows_the_mode(self):
        scheme = TagScheme.from_format("gfx/v{version}", "gfx")
        assert scheme.owns("gfx/v0.2.0-rc.1", mode=TagMode.PRERELEASE_INCLUSIVE)
        assert not scheme.owns("gfx/v0.2.0-rc.1", mode=TagMode.FINAL_ONLY)
        assert scheme.version_of(
            "gfx/v0.2.0-rc.1", mode=TagMode.PRERELEASE_INCLUSIVE,
        ) == "0.2.0-rc.1"

    def test_the_glob_and_the_format_are_one_scheme(self):
        from_format = TagScheme.from_format("kernel/v{version}", "kernel")
        assert TagScheme.from_glob(from_format.list_glob()) == from_format

    def test_a_glob_with_no_single_version_slot_is_refused(self):
        with pytest.raises(ValueError):
            TagScheme.from_glob("kernel/v")
        with pytest.raises(ValueError):
            TagScheme.from_glob("*/v*")


# ---------------------------------------------------------------------------
# The release record's tag evidence
# ---------------------------------------------------------------------------


def test_a_parents_empty_record_is_not_accused_by_a_nested_members_tag(tmp_path):
    """`kernel` has never released; its nested `vulkan` has. No refusal."""
    from rlsbl.release_record import unreleased_range

    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    run_git(root, "tag", "kernel/vulkan/v0.1.0")
    releases = root / ".rlsbl-monorepo" / "releasables" / "kernel" / "releases"
    releases.mkdir(parents=True)
    assert unreleased_range(str(releases), tag_glob="kernel/v*", cwd=str(root)) == "HEAD"


def test_the_parents_own_tag_still_refuses_an_empty_record(tmp_path):
    from rlsbl.errors import ReleaseRecordError
    from rlsbl.release_record import unreleased_range

    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    run_git(root, "tag", "kernel/v0.1.0")
    releases = root / ".rlsbl-monorepo" / "releasables" / "kernel" / "releases"
    releases.mkdir(parents=True)
    with pytest.raises(ReleaseRecordError) as exc:
        unreleased_range(str(releases), tag_glob="kernel/v*", cwd=str(root))
    assert "kernel/v0.1.0" in str(exc.value)


def test_the_root_scheme_is_not_accused_by_a_top_level_go_members_tag(tmp_path):
    from rlsbl.release_record import unreleased_range

    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    run_git(root, "tag", "vendor/v0.1.0")
    releases = root / ".rlsbl" / "releases"
    releases.mkdir(parents=True)
    assert unreleased_range(str(releases), tag_glob="v*", cwd=str(root)) == "HEAD"


# ---------------------------------------------------------------------------
# A dev-node root member owns no tags
# ---------------------------------------------------------------------------


def test_a_targetless_dev_node_root_owns_no_bare_version_tag(tmp_path):
    """Bare `v0.x` tags from before a conversion are not the dev-node root's."""
    from rlsbl.context import resolve_release_scope

    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    _project, tag_glob, _changes, _scope = resolve_release_scope(root)
    assert tag_glob == "root@v*"


# ---------------------------------------------------------------------------
# releasable-residue reads tags through the scheme
# ---------------------------------------------------------------------------


def test_releasable_residue_does_not_blame_a_member_for_its_nested_members_tag(
    tmp_path,
):
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.checks.workspace import _unreleasing_member_state
    from rlsbl.workspace import load_workspace

    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    # kernel opts out of versioning; its nested vulkan keeps releasing.
    toml = root / ".rlsbl-monorepo" / "workspace.toml"
    toml.write_text(toml.read_text().replace(
        'releasable = "kernel"', "releasable = false",
    ))
    run_git(root, "tag", "kernel/vulkan/v0.1.0")
    ctx = WorkspaceCheckContext(
        project_root=root, workspace_root=root, config={},
        projects=load_workspace(str(root)),
    )
    findings, _unreadable = _unreleasing_member_state(ctx)
    assert not any("kernel/vulkan/v0.1.0" in str(f) for f in findings), findings


# ---------------------------------------------------------------------------
# Extract classifies tags by scheme
# ---------------------------------------------------------------------------


def _tagged_go_workspace(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go")
    for tag in ("kernel/v0.1.0", "kernel/vulkan/v0.1.0", "kernel/vulkan/v0.2.0"):
        run_git(root, "tag", tag)
    return root


def test_extract_of_a_parent_does_not_take_its_nested_members_tags(tmp_path):
    from rlsbl.commands.monorepo.extract_cmd import _plan_tags

    root = _tagged_go_workspace(tmp_path)
    plan = _plan_tags(
        str(root), "kernel/v*", {"kernel/vulkan/v*", "draw/v*", "draw/cmd/v*"},
        "kernel/v{version}", "v{version}", "kernel", "0.1.0",
    )
    assert plan.own_tags == ("kernel/v0.1.0",)
    assert set(plan.pruned) == {"kernel/vulkan/v0.1.0", "kernel/vulkan/v0.2.0"}
    assert [old for old, _new in plan.translations] == ["kernel/v0.1.0"]


def test_extract_of_a_nested_member_keeps_its_own_tags(tmp_path):
    from rlsbl.commands.monorepo.extract_cmd import _plan_tags

    root = _tagged_go_workspace(tmp_path)
    plan = _plan_tags(
        str(root), "kernel/vulkan/v*", {"kernel/v*", "draw/v*", "draw/cmd/v*"},
        "kernel/vulkan/v{version}", "v{version}", "vulkan", "0.2.0",
    )
    assert set(plan.own_tags) == {"kernel/vulkan/v0.1.0", "kernel/vulkan/v0.2.0"}
    assert plan.pruned == ("kernel/v0.1.0",)


# ---------------------------------------------------------------------------
# The publish router's job conditions
# ---------------------------------------------------------------------------


def _router_jobs(tmp_path, members):
    from ruamel.yaml import YAML

    from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router
    from rlsbl.workspace_types import Releasable

    projects = []
    releasables = []
    for path, name, fmt in members:
        wf = tmp_path / path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "publish.yml").write_text(
            "name: publish\non: release\njobs:\n  publish:\n"
            "    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        projects.append({
            "name": name, "path": path, "releasable": name,
            "_ci_files": [f"{name}-ci-go.yml"],
        })
        releasables.append(Releasable(name=name, tag_format=fmt))
    router = generate_inline_publish_router(
        projects, str(tmp_path), releasables=releasables,
    )
    return YAML(typ="safe").load(router)["jobs"]


def test_a_parents_publish_jobs_do_not_run_on_a_nested_members_tag(tmp_path):
    jobs = _router_jobs(tmp_path, [
        ("kernel", "kernel", "kernel/v{version}"),
        ("kernel/vulkan", "vulkan", "kernel/vulkan/v{version}"),
    ])
    kernel_if = jobs["kernel-publish"]["if"]
    vulkan_if = jobs["vulkan-publish"]["if"]
    assert "startsWith(inputs.tag || github.ref_name, 'kernel/v')" in kernel_if
    assert "!startsWith(inputs.tag || github.ref_name, 'kernel/vulkan/v')" in kernel_if
    assert "!startsWith" not in vulkan_if
