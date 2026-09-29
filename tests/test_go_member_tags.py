"""Every Go member of a releasable gets its own module-proxy tag.

The Go module proxy resolves a module at ``<path>`` from a tag
``<path>/v<version>``, so a releasable whose members are several Go modules
owes one tag per module. A releasable tagged in the Go path form used to get
its primary tag alone -- ``gfx/v0.2.0`` and never ``gfx/shader/v0.2.0`` -- so
the nested module's publish failed after tagging.

A past release owes the tags of the members its own release record lists (the
paths it recorded tree hashes for), so a member added later is never owed a
tag at a commit that predates it.
"""

import json

from conftest import make_nested_workspace
from rlsbl.release_file import write_archived_release_file
from rlsbl.targets.base import BaseTarget
from rlsbl.targets.refs import ref_context
from rlsbl.workspace import get_releasable_dir

TREE = "f" * 40
SHA = "a" * 40


def _shared(tmp_path, fmt="gfx/v{version}"):
    root = tmp_path / "ws"
    make_nested_workspace(root, "shared", commit=False)
    rel_dir = get_releasable_dir(str(root), "gfx")
    (root / ".rlsbl-monorepo" / "releasables" / "gfx").mkdir(parents=True, exist_ok=True)
    with open(f"{rel_dir}/config.json", "w") as f:
        json.dump({"publish_mode": "ci"}, f)
    toml = root / ".rlsbl-monorepo" / "workspace.toml"
    toml.write_text(toml.read_text().replace("gfx/v{version}", fmt))
    return root, rel_dir


def _refs(root, rel_dir, version, fmt="gfx/v{version}"):
    return BaseTarget().expected_refs(version, ref_context(
        repo_root=str(root),
        primary_tag_format=fmt,
        releasable_name="gfx",
        member_package_paths=["gfx", "gfx/shader"],
        releasable_config_dir=rel_dir,
    ))


def test_a_path_tagged_releasable_owes_every_go_members_tag(tmp_path):
    root, rel_dir = _shared(tmp_path)
    refs = _refs(root, rel_dir, "0.2.0")
    assert refs.primary == "gfx/v0.2.0"
    assert refs.companions == ("gfx/shader/v0.2.0",)


def test_a_name_tagged_releasable_still_owes_every_go_members_tag(tmp_path):
    fmt = "{name}@v{version}"
    root, rel_dir = _shared(tmp_path, fmt)
    refs = _refs(root, rel_dir, "0.2.0", fmt)
    assert refs.primary == "gfx@v0.2.0"
    assert refs.companions == ("gfx/v0.2.0", "gfx/shader/v0.2.0")


def test_a_past_release_owes_only_the_members_its_record_lists(tmp_path):
    root, rel_dir = _shared(tmp_path)
    releases = f"{rel_dir}/releases"
    write_archived_release_file(
        releases, "0.1.0", bump="minor", include=["go"], description="First.",
        candidate_sha=SHA, tree_hashes={"gfx": TREE},
    )
    assert _refs(root, rel_dir, "0.1.0").companions == ()


def test_a_past_release_listing_the_member_owes_its_tag(tmp_path):
    root, rel_dir = _shared(tmp_path)
    releases = f"{rel_dir}/releases"
    write_archived_release_file(
        releases, "0.1.0", bump="minor", include=["go"], description="First.",
        candidate_sha=SHA, tree_hashes={"gfx": TREE, "gfx/shader": TREE},
    )
    assert _refs(root, rel_dir, "0.1.0").companions == ("gfx/shader/v0.1.0",)
