"""Tests for rlsbl.ownership: the tool-owned exempt set and single-owner attribution."""

import itertools

import pytest

from rlsbl.ownership import (
    ROOT_MEMBER_NAME,
    ROOT_MEMBER_PATH,
    OwnershipScope,
    find_root_member,
    is_root_member,
    is_tool_owned_path,
    member_prefix,
    owner_name_of,
    owner_of,
    owner_names_of_files,
    releasable_state_dir,
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
        """A member root carries its own state directory and changelog."""
        assert is_tool_owned_path(f"{prefix}/.rlsbl/changes/unreleased.jsonl")
        assert is_tool_owned_path(f"{prefix}/CHANGELOG.md")
        assert is_tool_owned_path(f"{prefix}/.rlsbl/version")

    @pytest.mark.parametrize("prefix", ["docs", "packages/core", "a/b/c"])
    def test_repository_root_rules_do_not_match_deeper(self, prefix):
        """The workspace directory and the router exist only at the root.

        rlsbl writes exactly one ``.rlsbl-monorepo/`` and one
        ``ci-router.yml``, both at the repository root, so a path of the same
        name deeper in the tree is somebody's hand-written file and needs an
        owner like any other.
        """
        nested_workspace = f"{prefix}/.rlsbl-monorepo/workspace.toml"
        nested_router = f"{prefix}/.github/workflows/ci-router.yml"
        assert is_tool_owned_path(nested_workspace) is False
        assert is_tool_owned_path(nested_router) is False
        assert owner_of(nested_workspace, [ROOT]) is ROOT
        assert owner_of(nested_router, [ROOT]) is ROOT
        # ...while the real ones, at the root, stay exempt.
        assert is_tool_owned_path(".rlsbl-monorepo/workspace.toml") is True
        assert is_tool_owned_path(".github/workflows/ci-router.yml") is True

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

    RELEASABLE_STATE_PATHS = [
        ".rlsbl-monorepo/releasables/one/changes/unreleased.jsonl",
        ".rlsbl-monorepo/releasables/one/releases/v0.1.0.toml",
        ".rlsbl-monorepo/releasables/one/version",
    ]

    @pytest.mark.parametrize("layout", LAYOUTS)
    def test_state_dirs_stay_memberless_and_are_claimed_by_one_scope(self, layout):
        """The scope-level rule never turns into a member claim.

        Over every layout: a releasable's state directory has no owning member
        (it is tool-owned), exactly the releasable whose name it carries claims
        it, and no other releasable does.
        """
        one = OwnershipScope.for_releasable(layout, layout, "one")
        two = OwnershipScope.for_releasable(layout, layout, "two")
        for path in self.RELEASABLE_STATE_PATHS:
            assert owner_of(path, layout) is None
            assert one.claims(path) is True
            assert two.claims(path) is False


class TestReleasableStateDirScope:
    """A releasable owns its own state directory, at the SCOPE level.

    Member attribution is unchanged -- a releasable is not a member, and the
    state directory stays tool-owned and memberless.  What changes is the
    question "does this releasable's changelog cover this file?", which the
    releasable answers "yes" for its own release machinery: archiving its
    release file and finalizing its changelog are commits about that
    releasable, and used to fall outside every scope.
    """

    MEMBERS = [ROOT, member("pkg-a"), member("pkg-b")]

    def rel_scope(self, name, *paths):
        return OwnershipScope.for_releasable(
            self.MEMBERS, [member(p) for p in paths], name,
        )

    def test_state_dir_is_derived_from_the_releasable_name(self):
        assert releasable_state_dir("core") == ".rlsbl-monorepo/releasables/core"

    @pytest.mark.parametrize("path", [
        ".rlsbl-monorepo/releasables/core/changes/unreleased.jsonl",
        ".rlsbl-monorepo/releasables/core/changes/0.2.0.jsonl",
        ".rlsbl-monorepo/releasables/core/releases/v0.2.0.toml",
        ".rlsbl-monorepo/releasables/core/version",
        ".rlsbl-monorepo/releasables/core/lineage.jsonl",
    ])
    def test_releasable_scope_claims_its_own_state_dir(self, path):
        scope = self.rel_scope("core", "pkg-a")
        assert scope.claims(path) is True
        assert scope.claims_any([path]) is True

    @pytest.mark.parametrize("path", [
        ".rlsbl-monorepo/releasables/other/releases/v0.2.0.toml",
        ".rlsbl-monorepo/releasables/core-extras/version",
        ".rlsbl-monorepo/workspace.toml",
        ".rlsbl-monorepo/snapshot.json",
    ])
    def test_releasable_scope_claims_nothing_else_under_the_workspace_dir(self, path):
        """Boundary-aware: a sibling releasable, and the workspace's own files."""
        scope = self.rel_scope("core", "pkg-a")
        assert scope.claims(path) is False

    def test_member_files_are_unaffected(self):
        scope = self.rel_scope("core", "pkg-a")
        assert scope.claims("pkg-a/main.py") is True
        assert scope.claims("pkg-b/main.py") is False
        assert scope.claims("README.md") is False

    def test_a_plain_member_scope_claims_no_state_dir(self):
        """Only a releasable claims a releasable state directory."""
        scope = OwnershipScope.for_members(self.MEMBERS, [member("pkg-a")])
        assert scope.claims(".rlsbl-monorepo/releasables/core/version") is False

    def test_member_attribution_is_untouched(self):
        """No phantom member: the state directory has no owner, as before."""
        path = ".rlsbl-monorepo/releasables/core/releases/v0.2.0.toml"
        assert is_tool_owned_path(path) is True
        assert owner_of(path, self.MEMBERS) is None
        scope = self.rel_scope("core", "pkg-a")
        assert scope.owner_name_of(path) is None

    def test_two_releasables_claim_only_their_own(self):
        core = self.rel_scope("core", "pkg-a")
        extras = self.rel_scope("extras", "pkg-b")
        core_file = ".rlsbl-monorepo/releasables/core/changes/unreleased.jsonl"
        extras_file = ".rlsbl-monorepo/releasables/extras/changes/unreleased.jsonl"
        assert core.claims(core_file) and not core.claims(extras_file)
        assert extras.claims(extras_file) and not extras.claims(core_file)


def _claims(m, path):
    prefix = member_prefix(m)
    if prefix == "":
        return True
    return path == prefix.rstrip("/") or path.startswith(prefix)
