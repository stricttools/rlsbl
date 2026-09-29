"""A member's CI filter leaves out the members nested inside it.

A parent's territory pattern (``draw/**``) also matched every nested member's
files, so a change inside ``draw/cmd`` ran ``draw``'s CI too. The filter now
excludes each nested member, as the root member's filter always excluded every
other member -- except a nested member whose territory holds one of the
member's dependencies. Excludes are final under ``some-with-excludes``, so an
exclude over a dependency could not be taken back by including the
dependency's own territory, and a skipped job deadlocks a release.
"""

from conftest import make_nested_workspace, make_workspace
from rlsbl.router_filters import RouterFilters, matches_filter


def _patterns(root, name, projects=None):
    from rlsbl.workspace import load_workspace

    projects = projects or load_workspace(str(root))
    filters = RouterFilters(str(root), projects)
    member = next(p for p in projects if p["name"] == name)
    return filters.patterns_for(member)


def test_a_parents_filter_excludes_its_nested_member(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "go", commit=False)
    patterns = _patterns(root, "draw")
    assert "draw/**" in patterns
    assert "!draw/cmd/**" in patterns
    assert not matches_filter("draw/cmd/main.go", patterns)
    assert matches_filter("draw/draw.go", patterns)


def test_a_nested_member_still_reacts_to_what_it_depends_on(tmp_path):
    root = tmp_path / "ws"
    projects = make_nested_workspace(root, "go", commit=False)
    # Go edges are declared (the workspace graph reads no go.mod requires).
    for proj in projects:
        if proj["name"] == "drawcmd":
            proj["depends_on"] = ["draw"]
    patterns = _patterns(root, "drawcmd", projects)
    assert matches_filter("draw/cmd/main.go", patterns)
    assert matches_filter("draw/draw.go", patterns)


def _chain(tmp_path, depends_on):
    root = tmp_path / "ws"
    for rel in ("a", "a/b", "a/b/c"):
        (root / rel).mkdir(parents=True, exist_ok=True)
        (root / rel / "pyproject.toml").write_text(
            f'[project]\nname = "{rel.replace("/", "-")}"\nversion = "0.1.0"\n'
        )
    members = [
        {"path": "a", "name": "a", "depends_on": depends_on},
        {"path": "a/b", "name": "b"},
        {"path": "a/b/c", "name": "c"},
    ]
    make_workspace(root, members)
    return root


def test_a_member_whose_territory_holds_a_dependency_is_not_excluded(tmp_path):
    root = _chain(tmp_path, ["c"])
    patterns = _patterns(root, "a")
    assert "!a/b/**" not in patterns
    assert matches_filter("a/b/c/mod.py", patterns)


def test_without_the_dependency_both_nested_members_are_excluded(tmp_path):
    root = _chain(tmp_path, [])
    patterns = _patterns(root, "a")
    assert "!a/b/**" in patterns
    assert not matches_filter("a/b/c/mod.py", patterns)


def test_the_root_keeps_a_member_that_holds_its_dependency(tmp_path):
    root = tmp_path / "ws"
    projects = make_nested_workspace(root, "go", commit=False)
    for proj in projects:
        if proj["name"] == "root":
            proj["depends_on"] = ["drawcmd"]
    patterns = _patterns(root, "root", projects)
    assert "!draw/**" not in patterns
    assert matches_filter("draw/cmd/main.go", patterns)
    assert "!kernel/**" in patterns
