"""Tests for rlsbl.ownership: the tool-owned exempt set and single-owner attribution."""

import itertools

import pytest

from rlsbl.ownership import (
    ROOT_MEMBER_NAME,
    ROOT_MEMBER_PATH,
    find_root_member,
    is_root_member,
    is_tool_owned_path,
    member_prefix,
    owner_name_of,
    owner_of,
    owner_names_of_files,
    tool_owned_rule,
    tool_owned_rules,
    unowned_paths,
)


def member(path, name=None):
    """A workspace member dict as the loader produces one."""
    return {"path": path, "name": name or (path.rstrip("/") or ROOT_MEMBER_NAME)}


ROOT = member(ROOT_MEMBER_PATH, ROOT_MEMBER_NAME)


# ---------------------------------------------------------------------------
# 3.7 -- the derived tool-owned exempt set
# ---------------------------------------------------------------------------


class TestToolOwnedSet:
    @pytest.mark.parametrize("path", [
        ".rlsbl/changes/unreleased.jsonl",
        ".rlsbl/changes/.validated",
        ".rlsbl/releases/unreleased.toml",
        ".rlsbl/releases/v1.2.3.toml",
        ".rlsbl/bases/ci.yml",
        ".rlsbl/lint/cache.json",
        ".rlsbl/version",
        "CHANGELOG.md",
        ".rlsbl-monorepo/workspace.toml",
        ".rlsbl-monorepo/snapshot.json",
        ".rlsbl-monorepo/releasables/core/changes/unreleased.jsonl",
        ".github/workflows/ci-router.yml",
    ])
    def test_tool_owned(self, path):
        assert is_tool_owned_path(path) is True
        assert tool_owned_rule(path)

    @pytest.mark.parametrize("path", [
        "packages/core/src/main.py",
        "README.md",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        ".github/workflows/publish.yml",
        ".rlsbl/config.json",
        ".rlsbl/hooks/pre-checks.sh",
        "docs/CHANGELOG.md.j2",
    ])
    def test_not_tool_owned(self, path):
        assert is_tool_owned_path(path) is False
        assert tool_owned_rule(path) is None

    @pytest.mark.parametrize("prefix", ["python", "packages/core", "a/b/c"])
    def test_rules_match_at_any_depth(self, prefix):
        assert is_tool_owned_path(f"{prefix}/.rlsbl/changes/unreleased.jsonl")
        assert is_tool_owned_path(f"{prefix}/CHANGELOG.md")
        assert is_tool_owned_path(f"{prefix}/.rlsbl/version")

    def test_rules_are_static(self):
        """No dynamic input: the same path answers the same way, always."""
        assert tool_owned_rules()
        for rule in tool_owned_rules():
            assert not rule.startswith("/")

    def test_workspace_machinery_stays_exempt(self):
        """The verify item for 3.7: workspace-machinery paths need no owner."""
        machinery = [
            ".rlsbl-monorepo/workspace.toml",
            ".rlsbl-monorepo/snapshot.json",
            ".rlsbl-monorepo/releasables/core/version",
            ".rlsbl-monorepo/releasables/core/releases/v0.1.0.toml",
            ".github/workflows/ci-router.yml",
        ]
        for path in machinery:
            assert is_tool_owned_path(path), path
            assert owner_of(path, [ROOT]) is None, path

    def test_changelog_exemption_alias_is_the_same_rule(self):
        from rlsbl.changelog.exemptions import is_changelog_path

        assert is_changelog_path is is_tool_owned_path


# ---------------------------------------------------------------------------
# 3.1 -- single-owner attribution
# ---------------------------------------------------------------------------


class TestMemberPrefix:
    def test_root_member_prefix_is_empty(self):
        """The defect: a "./" prefix matched nothing, so a root member owned nothing."""
        assert member_prefix(ROOT) == ""
        for spelling in ("", ".", "./"):
            assert member_prefix(member(spelling, "root")) == ""

    def test_declared_member_prefix(self):
        assert member_prefix(member("pkg")) == "pkg/"
        assert member_prefix(member("packages/core/")) == "packages/core/"

    def test_is_root_member(self):
        assert is_root_member(ROOT)
        assert not is_root_member(member("pkg"))

    def test_find_root_member(self):
        members = [member("pkg"), ROOT, member("other")]
        assert find_root_member(members) is ROOT
        assert find_root_member([member("pkg")]) is None


class TestOwnerOf:
    def test_root_member_owns_root_files(self):
        assert owner_name_of("README.md", [ROOT]) == "root"
        assert owner_name_of("scripts/build.sh", [ROOT]) == "root"

    def test_declared_member_beats_root(self):
        members = [ROOT, member("pkg")]
        assert owner_name_of("pkg/main.py", members) == "pkg"
        assert owner_name_of("other/main.py", members) == "root"

    def test_most_specific_wins(self):
        members = [ROOT, member("pkg"), member("pkg/inner")]
        assert owner_name_of("pkg/inner/a.py", members) == "pkg/inner"
        assert owner_name_of("pkg/a.py", members) == "pkg"

    def test_member_order_does_not_matter(self):
        base = [ROOT, member("pkg"), member("pkg/inner")]
        for perm in itertools.permutations(base):
            assert owner_name_of("pkg/inner/a.py", list(perm)) == "pkg/inner"
            assert owner_name_of("elsewhere/a.py", list(perm)) == "root"

    def test_sibling_prefix_is_not_a_match(self):
        members = [ROOT, member("pkg")]
        assert owner_name_of("pkg-extras/main.py", members) == "root"

    def test_file_equal_to_member_path(self):
        members = [ROOT, member("pkg")]
        assert owner_name_of("pkg", members) == "pkg"

    def test_tool_owned_paths_have_no_owner(self):
        members = [ROOT, member("pkg")]
        assert owner_of("pkg/CHANGELOG.md", members) is None
        assert owner_of(".rlsbl-monorepo/workspace.toml", members) is None

    def test_no_root_member_leaves_residual_unowned(self):
        members = [member("pkg")]
        assert owner_of("README.md", members) is None
        assert unowned_paths(["README.md", "pkg/a.py"], members) == ["README.md"]

    def test_owner_names_of_files(self):
        members = [ROOT, member("pkg"), member("lib")]
        names = owner_names_of_files(
            ["pkg/a.py", "lib/b.py", "README.md", "CHANGELOG.md"], members,
        )
        assert names == {"pkg", "lib", "root"}


class TestSingleOwnerInvariant:
    """Property: over arbitrary member layouts, every non-tool-owned file has
    exactly one owner as long as a root member is present."""

    LAYOUTS = [
        [ROOT],
        [ROOT, member("a")],
        [ROOT, member("a"), member("b")],
        [ROOT, member("a"), member("a/b")],
        [ROOT, member("a"), member("a/b"), member("a/b/c")],
        [ROOT, member("packages/one"), member("packages/two"), member("tools")],
    ]

    PATHS = [
        "README.md",
        "a.py",
        "a/x.py",
        "a/b/x.py",
        "a/b/c/x.py",
        "a-extras/x.py",
        "b/deep/nested/x.py",
        "packages/one/src/x.py",
        "packages/two/src/x.py",
        "packages/three/src/x.py",
        "tools/x.sh",
        ".github/workflows/ci.yml",
    ]

    @pytest.mark.parametrize("layout", LAYOUTS)
    def test_every_non_tool_owned_file_has_exactly_one_owner(self, layout):
        for path in self.PATHS:
            owners = [m for m in layout if _claims(m, path)]
            assert owners, f"{path} unowned in {[m['name'] for m in layout]}"
            resolved = owner_of(path, layout)
            assert resolved is not None
            # The resolver picks exactly one, and it is the most specific claim.
            deepest = max(owners, key=lambda m: len(member_prefix(m)))
            assert resolved["name"] == deepest["name"]
            assert unowned_paths([path], layout) == []

    @pytest.mark.parametrize("layout", LAYOUTS)
    def test_exemption_set_is_excluded_before_attribution(self, layout):
        for path in [
            "CHANGELOG.md",
            "a/CHANGELOG.md",
            ".rlsbl/changes/unreleased.jsonl",
            ".rlsbl-monorepo/snapshot.json",
        ]:
            assert owner_of(path, layout) is None


def _claims(m, path):
    prefix = member_prefix(m)
    if prefix == "":
        return True
    return path == prefix.rstrip("/") or path.startswith(prefix)
