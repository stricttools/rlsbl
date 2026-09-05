"""``expected_refs`` is the single authority for a version's full ref set.

Three groups compose into one answer -- the primary tag, the ecosystem's
companion tags, and the aliases this repository's own records attribute to the
version -- and the release's tag step creates exactly that set. These tests pin
both: the composition itself, and the equality between what the release pushes
and what the authority names.

The alias group has two sources. The ``boundary-alias`` events pinned here are
one; the archives' ``shipped_as`` fields are the other, pinned in
``tests/test_shipped_as_expected_refs.py`` together with the hard error the two
raise when they disagree about one version.
"""

import json
import os
from pathlib import Path

import pytest

from githarness import git as _git
from rlsbl.transition_record import (
    BoundaryAlias,
    BoundaryAliasEvent,
    append_events,
    get_transition_record_path,
)
from rlsbl.targets import TARGETS
from rlsbl.targets.base import BaseTarget
from rlsbl.targets.go import GoTarget
from rlsbl.targets.refs import ExpectedRefs, ref_context


# ---------------------------------------------------------------------------
# The value type
# ---------------------------------------------------------------------------


class TestExpectedRefsValue:

    def test_tags_is_primary_first_then_declaration_order(self):
        refs = ExpectedRefs(
            version="1.0.0", primary="v1.0.0",
            companions=("pkg/v1.0.0",), aliases=("old@v1.0.0",),
        )
        assert refs.tags == ("v1.0.0", "pkg/v1.0.0", "old@v1.0.0")

    def test_tags_deduplicates_across_groups(self):
        refs = ExpectedRefs(
            version="1.0.0", primary="v1.0.0",
            companions=("v1.0.0", "pkg/v1.0.0"), aliases=("pkg/v1.0.0",),
        )
        assert refs.tags == ("v1.0.0", "pkg/v1.0.0")


# ---------------------------------------------------------------------------
# The primary tag: three naming authorities, in precedence order
# ---------------------------------------------------------------------------


class TestPrimaryRef:

    def test_standalone_uses_the_targets_own_tag_format(self, tmp_path):
        refs = BaseTarget().expected_refs("1.2.3", ref_context(repo_root=str(tmp_path)))
        assert refs.primary == "v1.2.3"

    def test_a_monorepo_package_uses_the_targets_monorepo_format(self, tmp_path):
        refs = BaseTarget().expected_refs("1.2.3", ref_context(
            repo_root=str(tmp_path), monorepo_name="mylib",
            project_path="packages/mylib",
        ))
        assert refs.primary == "mylib@v1.2.3"

    def test_go_renders_its_path_scheme_for_a_monorepo_package(self, tmp_path):
        refs = GoTarget().expected_refs("1.2.3", ref_context(
            repo_root=str(tmp_path), monorepo_name="mylib",
            project_path="packages/mylib",
        ))
        assert refs.primary == "packages/mylib/v1.2.3"

    def test_a_releasables_tag_format_outranks_both(self, tmp_path):
        refs = GoTarget().expected_refs("1.2.3", ref_context(
            repo_root=str(tmp_path), monorepo_name="mylib",
            project_path="packages/mylib",
            primary_tag_format="{name}@v{version}", releasable_name="core",
        ))
        assert refs.primary == "core@v1.2.3"


# ---------------------------------------------------------------------------
# Recorded aliases
# ---------------------------------------------------------------------------


def _record_alias(path, alias_tag, aliased_tag, commit="a" * 40):
    append_events(path, [BoundaryAliasEvent(aliases=[BoundaryAlias(
        alias_tag=alias_tag, aliased_tag=aliased_tag, commit=commit,
    )])])


class TestRecordedAliases:

    def test_no_record_means_no_aliases(self, tmp_path):
        refs = BaseTarget().expected_refs("1.0.0", ref_context(repo_root=str(tmp_path)))
        assert refs.aliases == ()

    def test_both_tags_of_a_recorded_alias_belong_to_the_version(self, tmp_path):
        rel_dir = tmp_path / ".rlsbl-monorepo" / "releasables" / "core"
        rel_dir.mkdir(parents=True)
        _record_alias(
            get_transition_record_path(str(tmp_path), releasable_dir=str(rel_dir)),
            "core@v1.0.0", "old@v1.0.0",
        )

        refs = BaseTarget().expected_refs("1.0.0", ref_context(
            repo_root=str(tmp_path),
            primary_tag_format="{name}@v{version}", releasable_name="core",
            releasable_config_dir=str(rel_dir),
        ))
        assert refs.primary == "core@v1.0.0"
        # The alias tag IS the primary here, so the flat set carries it once.
        assert set(refs.aliases) == {"core@v1.0.0", "old@v1.0.0"}
        assert refs.tags == ("core@v1.0.0", "old@v1.0.0")

    def test_an_alias_for_another_version_is_not_this_versions_ref(self, tmp_path):
        rel_dir = tmp_path / ".rlsbl-monorepo" / "releasables" / "core"
        rel_dir.mkdir(parents=True)
        _record_alias(
            get_transition_record_path(str(tmp_path), releasable_dir=str(rel_dir)),
            "core@v1.0.0", "old@v1.0.0",
        )

        refs = BaseTarget().expected_refs("2.0.0", ref_context(
            repo_root=str(tmp_path),
            primary_tag_format="{name}@v{version}", releasable_name="core",
            releasable_config_dir=str(rel_dir),
        ))
        assert refs.aliases == ()
        assert refs.tags == ("core@v2.0.0",)

    def test_a_prerelease_version_matches_its_own_alias(self, tmp_path):
        rel_dir = tmp_path / ".rlsbl-monorepo" / "releasables" / "core"
        rel_dir.mkdir(parents=True)
        _record_alias(
            get_transition_record_path(str(tmp_path), releasable_dir=str(rel_dir)),
            "core@v1.0.0-rc.1", "old@v1.0.0-rc.1",
        )

        refs = BaseTarget().expected_refs("1.0.0-rc.1", ref_context(
            repo_root=str(tmp_path),
            primary_tag_format="{name}@v{version}", releasable_name="core",
            releasable_config_dir=str(rel_dir),
        ))
        assert set(refs.aliases) == {"core@v1.0.0-rc.1", "old@v1.0.0-rc.1"}

    def test_a_tag_under_no_known_scheme_carries_no_version(self, tmp_path):
        """A milestone-style tag is not silently attributed to a version."""
        rel_dir = tmp_path / ".rlsbl-monorepo" / "releasables" / "core"
        rel_dir.mkdir(parents=True)
        _record_alias(
            get_transition_record_path(str(tmp_path), releasable_dir=str(rel_dir)),
            "latest", "core@v1.0.0",
        )

        refs = BaseTarget().expected_refs("1.0.0", ref_context(
            repo_root=str(tmp_path),
            primary_tag_format="{name}@v{version}", releasable_name="core",
            releasable_config_dir=str(rel_dir),
        ))
        assert refs.aliases == ("core@v1.0.0",)

    def test_a_standalone_project_reads_its_own_record(self, tmp_path):
        (tmp_path / ".rlsbl").mkdir()
        _record_alias(get_transition_record_path(str(tmp_path)), "v1.0.0", "old@v1.0.0")

        refs = BaseTarget().expected_refs("1.0.0", ref_context(repo_root=str(tmp_path)))
        assert set(refs.aliases) == {"v1.0.0", "old@v1.0.0"}

    def test_a_malformed_record_is_a_hard_error(self, tmp_path):
        from rlsbl.transition_record import TransitionRecordError

        (tmp_path / ".rlsbl").mkdir()
        Path(get_transition_record_path(str(tmp_path))).write_text("{not json\n", encoding="utf-8")
        with pytest.raises(TransitionRecordError):
            BaseTarget().expected_refs("1.0.0", ref_context(repo_root=str(tmp_path)))


class TestRefContextConstruction:

    def test_a_releasable_reads_only_its_own_record(self, tmp_path):
        rel_dir = tmp_path / ".rlsbl-monorepo" / "releasables" / "core"
        context = ref_context(repo_root=str(tmp_path), releasable_config_dir=str(rel_dir))
        assert context.transition_record_paths == (
            os.path.join(str(rel_dir), "transitions.jsonl"),
        )

    def test_a_monorepo_package_reads_its_own_directory_record(self, tmp_path):
        context = ref_context(repo_root=str(tmp_path), project_path="packages/mylib")
        assert context.transition_record_paths == (
            os.path.join(str(tmp_path), "packages", "mylib", ".rlsbl", "transitions.jsonl"),
        )

    def test_both_alias_sources_come_from_the_same_fork(self, tmp_path):
        """The record and the archives are always read for the SAME project."""
        rel_dir = tmp_path / ".rlsbl-monorepo" / "releasables" / "core"
        releasable = ref_context(
            repo_root=str(tmp_path), releasable_config_dir=str(rel_dir),
        )
        assert releasable.releases_dirs == (
            os.path.join(str(rel_dir), "releases"),
        )

        package = ref_context(repo_root=str(tmp_path), project_path="packages/mylib")
        assert package.releases_dirs == (
            os.path.join(
                str(tmp_path), "packages", "mylib", ".rlsbl", "releases",
            ),
        )

        standalone = ref_context(repo_root=str(tmp_path))
        assert standalone.releases_dirs == (
            os.path.join(str(tmp_path), ".rlsbl", "releases"),
        )

    def test_member_paths_none_is_preserved_as_none(self, tmp_path):
        assert ref_context(repo_root=str(tmp_path)).member_package_paths is None
        assert ref_context(
            repo_root=str(tmp_path), member_package_paths=[],
        ).member_package_paths == ()


# ---------------------------------------------------------------------------
# What the release tags IS what expected_refs names
# ---------------------------------------------------------------------------


def _monorepo_with_go_member(root):
    """A git monorepo whose releasable has one publishing Go member."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")

    go_pkg = root / "packages" / "golib"
    go_pkg.mkdir(parents=True)
    (go_pkg / "go.mod").write_text("module github.com/test/golib\n\ngo 1.21\n")
    (go_pkg / ".rlsbl").mkdir()
    (go_pkg / ".rlsbl" / "config.json").write_text(json.dumps({
        "publish_mode": "ci", "targets": ["go"],
    }))
    (root / "README.md").write_text("mono\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


class TestTheReleaseTagsExactlyTheExpectedSet:
    """The set the tag step creates equals ``expected_refs``, on both fixtures.

    The step's own code takes ``expected.companions``/``expected.aliases``
    directly, so what these pin is that the fixture really produces the two
    interesting shapes -- a Go companion and a renamed-releasable alias -- and
    that the flat set a push would carry is exactly them.
    """

    def test_a_go_companion_fixture(self, tmp_path):
        from rlsbl.commands.release.execute import release_ref_context

        root = _monorepo_with_go_member(tmp_path / "mono")
        refs = TARGETS["go"].expected_refs("1.0.0", release_ref_context(
            monorepo_root=str(root), git_root=str(root),
            releasable_name="core", releasable_tag_format="{name}@v{version}",
            member_package_paths=["packages/golib"],
        ))
        assert refs.primary == "core@v1.0.0"
        assert refs.tags == ("core@v1.0.0", "packages/golib/v1.0.0")

    def test_a_renamed_releasable_alias_fixture(self, tmp_path):
        from rlsbl.commands.release.execute import release_ref_context

        root = _monorepo_with_go_member(tmp_path / "mono")
        rel_dir = root / ".rlsbl-monorepo" / "releasables" / "core"
        rel_dir.mkdir(parents=True)
        _git(root, "tag", "old@v1.0.0")
        commit = _git(root, "rev-list", "-n", "1", "old@v1.0.0")
        _record_alias(
            get_transition_record_path(str(root), releasable_dir=str(rel_dir)),
            "core@v1.0.0", "old@v1.0.0", commit=commit,
        )

        refs = TARGETS["go"].expected_refs("1.0.0", release_ref_context(
            monorepo_root=str(root), git_root=str(root),
            releasable_name="core", releasable_tag_format="{name}@v{version}",
            member_package_paths=["packages/golib"],
            releasable_config_dir=str(rel_dir),
        ))
        assert refs.primary == "core@v1.0.0"
        assert refs.tags == (
            "core@v1.0.0", "packages/golib/v1.0.0", "old@v1.0.0",
        )

    def test_the_go_path_scheme_suppresses_its_own_companion(self, tmp_path):
        from rlsbl.commands.release.execute import release_ref_context

        root = _monorepo_with_go_member(tmp_path / "mono")
        refs = TARGETS["go"].expected_refs("1.0.0", release_ref_context(
            monorepo_root=str(root), git_root=str(root),
            monorepo_name="golib", monorepo_project_path="packages/golib",
            member_package_paths=["packages/golib"],
        ))
        assert refs.primary == "packages/golib/v1.0.0"
        assert refs.companions == ()


class TestTheAxisInventoryClassifiesIt:

    def test_expected_refs_is_an_excluded_operation(self):
        from rlsbl.targets.introspect import (
            AXIS_NAMES,
            NON_AXIS_ATTRIBUTES,
            assert_axis_inventory_is_complete,
        )

        assert "expected_refs" in NON_AXIS_ATTRIBUTES
        assert NON_AXIS_ATTRIBUTES["expected_refs"].strip()
        assert "expected_refs" not in AXIS_NAMES
        assert_axis_inventory_is_complete()

    def test_no_target_overrides_it(self):
        """It composes axes; a target that overrode it would be a second answer."""
        overriders = [
            name for name, target in TARGETS.items()
            if type(target).expected_refs is not BaseTarget.expected_refs
        ]
        assert overriders == []
