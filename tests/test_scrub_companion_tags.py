"""A scrub's GitHub Release step leaves a Go companion tag alone.

A releasable whose Go members nest owes each nested module its own tag at the
released version (``gfx/shader/v0.1.0`` beside ``gfx/v0.1.0``), which the
release pushes and creates no GitHub Release for. A history rewrite re-points
both tags; the Release step then asked whose notes the companion carries,
found no scheme rendering it, and refused the whole scrub with the tags
already force-pushed.
"""

import json
from pathlib import Path

from conftest import make_nested_workspace
from rlsbl.commands.release_reconcile import tag_scheme_index, update_github_releases
from rlsbl.context import ProjectContext
from rlsbl.release_file import write_archived_release_file
from rlsbl.workspace import get_releasable_dir, load_workspace

SHA = "a" * 40
TREE = "f" * 40


def _released_nested_workspace(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "shared")
    rel_dir = get_releasable_dir(str(root), "gfx")
    Path(rel_dir).mkdir(parents=True, exist_ok=True)
    with open(f"{rel_dir}/config.json", "w") as f:
        json.dump({"publish_mode": "ci"}, f)
    write_archived_release_file(
        f"{rel_dir}/releases", "0.1.0", bump="minor", include=["go"],
        description="First.", candidate_sha=SHA,
        tree_hashes={"gfx": TREE, "gfx/shader": TREE},
    )
    return root


def test_the_companion_tag_gets_no_release_and_the_primary_does(tmp_path):
    root = _released_nested_workspace(tmp_path)
    projects = load_workspace(str(root))
    ctx = ProjectContext(project_root=root / "gfx", workspace_root=root, config={})
    calls = []

    def gh(args, config=None):
        calls.append(list(args))
        if args[:2] == ["release", "view"]:
            return json.dumps({"body": "old"})
        return ""

    tags = [
        {"refname": "refs/tags/gfx/v0.1.0", "old_sha": "b" * 40, "new_sha": SHA},
        {"refname": "refs/tags/gfx/shader/v0.1.0", "old_sha": "b" * 40, "new_sha": SHA},
    ]
    written = update_github_releases(
        tags, ctx=ctx, project_root=root / "gfx", workspace_projects=projects,
        tag_schemes=tag_scheme_index(str(root), projects), gh=gh,
        gh_installed=lambda: True, gh_auth=lambda: True,
        extract_entry=lambda *_a, **_k: "- first",
    )

    assert written == 1
    touched = {arg for call in calls for arg in call if "v0.1.0" in arg}
    assert touched == {"gfx/v0.1.0"}
