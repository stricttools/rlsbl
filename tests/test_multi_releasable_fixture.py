"""Verification tests for the multi_releasable_monorepo fixture.

Ensures the fixture builds a valid monorepo with releasable structure
that load_releasables() and members_of() can consume correctly.

Also covers the releasable-state helpers (make_releasable_state,
make_releasable_monorepo), which build the real
``.rlsbl-monorepo/releasables/<name>/`` layout: version, changes/,
releases/ and config.json.
"""

import json
import os

import pytest

from conftest import (
    DEFAULT_RELEASE_FILE,
    make_releasable_monorepo,
    make_releasable_state,
)
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl
from rlsbl.workspace import (
    load_releasables,
    load_workspace,
    members_of,
    get_releasable_changes_dir,
    get_releasable_dir,
    read_releasable_version,
)


class TestMultiReleasableFixtureDefaults:
    """Tests using the default multi_releasable_monorepo fixture."""

    def test_load_releasables_returns_two(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        releasables = load_releasables(str(ns.root))
        assert len(releasables) == 2
        names = {r.name for r in releasables}
        assert names == {"alpha", "beta"}

    def test_alpha_has_two_members(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        projects = load_workspace(str(ns.root))
        members = members_of("alpha", projects)
        assert len(members) == 2
        member_names = {m.name for m in members}
        assert member_names == {"alpha-core", "alpha-web"}

    def test_beta_has_two_members(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        projects = load_workspace(str(ns.root))
        members = members_of("beta", projects)
        assert len(members) == 2
        member_names = {m.name for m in members}
        assert member_names == {"beta-api", "beta-cli"}

    def test_dev_only_project_not_in_any_releasable(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        projects = load_workspace(str(ns.root))
        devtools = [p for p in projects if p.name == "devtools"]
        assert len(devtools) == 1
        assert devtools[0].dev_only is True
        # Should not appear as a member of either releasable
        for rel_name in ("alpha", "beta"):
            members = members_of(rel_name, projects)
            assert all(m.name != "devtools" for m in members)

    def test_releasable_version_files_exist(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        for rel in ns.releasables:
            version = read_releasable_version(str(ns.root), rel.name)
            assert version == ns.initial_version

    def test_releasable_changes_dirs_exist(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        for rel in ns.releasables:
            changes_dir = get_releasable_changes_dir(str(ns.root), rel.name)
            assert os.path.isdir(changes_dir)
            unreleased = os.path.join(changes_dir, "unreleased.jsonl")
            assert os.path.isfile(unreleased)

    def test_releasable_config_json_exists(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        for rel in ns.releasables:
            rel_dir = get_releasable_dir(str(ns.root), rel.name)
            config_path = os.path.join(rel_dir, "config.json")
            assert os.path.isfile(config_path)
            with open(config_path) as f:
                config = json.load(f)
            # A real releasable config is never the empty object: publish_mode
            # is required, and "none" is the test-safe stance (no publishing to
            # any public registry).
            assert config == {"publish_mode": "none"}

    def test_project_directories_created(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        for proj_name, proj_dir in ns.project_dirs.items():
            assert proj_dir.is_dir(), f"{proj_name} dir missing"
            if proj_dir == ns.root:
                # The root member owns the repository root, which the factory
                # gives no manifest of its own.
                continue
            assert (proj_dir / "pyproject.toml").is_file()

    def test_git_tags_exist(self, multi_releasable_monorepo):
        """Each releasable should have a version tag."""
        import subprocess

        ns = multi_releasable_monorepo
        result = subprocess.run(
            ["git", "tag", "--list"],
            cwd=str(ns.root),
            capture_output=True,
            text=True,
            check=True,
        )
        tags = set(result.stdout.strip().split("\n"))
        for rel in ns.releasables:
            expected_tag = rel.effective_tag_format.format(
                name=rel.name, version=ns.initial_version
            )
            assert expected_tag in tags, f"tag {expected_tag} not found in {tags}"


class TestInitialReleaseState:
    """The version the factory TAGS is a released version, so it has state.

    Real rlsbl never leaves a tag standing over nothing: a released version
    always has its trio -- ``changes/<v>.jsonl``, ``changes/<v>.md`` and
    ``releases/v<v>.toml``. A fixture that tags without writing them models a
    repo the tools cannot produce.
    """

    def test_tagged_version_has_its_trio(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        for rel in ns.releasables:
            changes_dir = get_releasable_changes_dir(str(ns.root), rel.name)
            releases_dir = os.path.join(
                get_releasable_dir(str(ns.root), rel.name), "releases"
            )
            jsonl = os.path.join(changes_dir, f"{ns.initial_version}.jsonl")
            md = os.path.join(changes_dir, f"{ns.initial_version}.md")
            archive = os.path.join(releases_dir, f"v{ns.initial_version}.toml")
            assert os.path.isfile(jsonl), f"{rel.name}: {jsonl} missing"
            assert os.path.isfile(md), f"{rel.name}: {md} missing"
            assert os.path.isfile(archive), f"{rel.name}: {archive} missing"
            # Locked exactly as rlsbl locks released state.
            assert not os.access(jsonl, os.W_OK)
            assert not os.access(archive, os.W_OK)

    def test_initial_release_is_user_facing_and_resolvable(
        self, multi_releasable_monorepo,
    ):
        """The released entry is a real one: a user-facing entry over a commit
        that resolves in the repo, which is what a release requires."""
        import subprocess

        ns = multi_releasable_monorepo
        changes_dir = get_releasable_changes_dir(str(ns.root), "alpha")
        entries = parse_jsonl(
            os.path.join(changes_dir, f"{ns.initial_version}.jsonl")
        )
        assert entries
        assert all(e.user_facing for e in entries)
        for entry in entries:
            for sha in entry.commits:
                subprocess.run(
                    ["git", "cat-file", "-e", sha + "^{commit}"],
                    cwd=str(ns.root), check=True,
                    capture_output=True, text=True,
                )

    def test_caller_supplied_version_wins(self, multi_releasable_monorepo_factory):
        """An explicit entry list for the initial version replaces the default
        one -- the factory never fights the caller for that slot."""
        ns = multi_releasable_monorepo_factory(
            releasable_changes={
                "alpha": {
                    "versions": {
                        "0.1.0": [
                            ChangelogEntry(
                                commits=["cafebabe"], user_facing=True,
                                description="Caller's own entry", type="fix",
                            ),
                        ],
                    },
                },
            },
        )
        changes_dir = get_releasable_changes_dir(str(ns.root), "alpha")
        entries = parse_jsonl(os.path.join(changes_dir, "0.1.0.jsonl"))
        assert [e.description for e in entries] == ["Caller's own entry"]

    def test_damaged_state_is_opt_in(self, multi_releasable_monorepo_factory):
        """A test that WANTS the damaged shape (a tag with no state behind it)
        declares it, rather than getting it by default."""
        ns = multi_releasable_monorepo_factory(write_initial_release_state=False)
        for rel in ns.releasables:
            changes_dir = get_releasable_changes_dir(str(ns.root), rel.name)
            releases_dir = os.path.join(
                get_releasable_dir(str(ns.root), rel.name), "releases"
            )
            assert not os.path.exists(
                os.path.join(changes_dir, f"{ns.initial_version}.jsonl")
            )
            assert not os.path.exists(
                os.path.join(releases_dir, f"v{ns.initial_version}.toml")
            )

    def test_initial_release_state_is_committed(
        self, multi_releasable_monorepo,
    ):
        import subprocess

        ns = multi_releasable_monorepo
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ns.root), capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == ""


class TestMultiReleasableFactory:
    """Tests using the factory fixture for custom configurations."""

    def test_custom_releasable_config(self, multi_releasable_monorepo_factory):
        ns = multi_releasable_monorepo_factory(
            releasable_configs={
                "alpha": {"batch_limits": {"max_commits_per_entry": 3}},
            },
        )
        rel_dir = get_releasable_dir(str(ns.root), "alpha")
        config_path = os.path.join(rel_dir, "config.json")
        with open(config_path) as f:
            config = json.load(f)
        assert config["batch_limits"]["max_commits_per_entry"] == 3
        # A named releasable's config replaces the default outright.
        assert "publish_mode" not in config
        # An unnamed one keeps the default.
        beta_config = os.path.join(get_releasable_dir(str(ns.root), "beta"),
                                   "config.json")
        with open(beta_config) as f:
            assert json.load(f) == {"publish_mode": "none"}

    def test_make_releasable_state_default_config(self, tmp_path):
        """make_releasable_state's own default is the same minimal config, so
        the standalone helper and the factory agree."""
        rel_dir = make_releasable_state(tmp_path, "core")
        assert json.loads((rel_dir / "config.json").read_text()) == {
            "publish_mode": "none",
        }

    def test_explicit_empty_config_is_honored(self, tmp_path):
        """An explicitly empty config is still written empty -- the default
        applies to absence, never overriding what a caller stated."""
        rel_dir = make_releasable_state(tmp_path, "core", config={})
        assert json.loads((rel_dir / "config.json").read_text()) == {}

    def test_custom_hook_config(self, multi_releasable_monorepo_factory):
        ns = multi_releasable_monorepo_factory(
            hook_configs={
                "alpha": {
                    "pre-checks.sh": "#!/bin/bash\necho pre-checks\n",
                    "pre-release.sh": "#!/bin/bash\necho pre-release\n",
                },
            },
        )
        rel_dir = get_releasable_dir(str(ns.root), "alpha")
        hook_path = os.path.join(rel_dir, "hooks", "pre-checks.sh")
        assert os.path.isfile(hook_path)
        assert os.access(hook_path, os.X_OK)

    def test_custom_initial_version(self, multi_releasable_monorepo_factory):
        ns = multi_releasable_monorepo_factory(initial_version="1.0.0")
        for rel in ns.releasables:
            version = read_releasable_version(str(ns.root), rel.name)
            assert version == "1.0.0"

    def test_releasable_changes_content(self, multi_releasable_monorepo_factory):
        """releasable_changes fills the releasable's OWN changes dir --
        unreleased plus locked versioned files with their .md siblings."""
        ns = multi_releasable_monorepo_factory(
            releasable_changes={
                "alpha": {
                    "unreleased": [
                        ChangelogEntry(
                            commits=["deadbeef"],
                            user_facing=True,
                            description="Alpha feature",
                            type="feature",
                        ),
                    ],
                    "versions": {
                        "0.1.0": [
                            ChangelogEntry(
                                commits=["cafebabe"],
                                user_facing=True,
                                description="Alpha first release",
                                type="feature",
                            ),
                        ],
                    },
                },
            },
        )
        changes_dir = get_releasable_changes_dir(str(ns.root), "alpha")

        unreleased = parse_jsonl(os.path.join(changes_dir, "unreleased.jsonl"))
        assert [e.description for e in unreleased] == ["Alpha feature"]

        versioned_path = os.path.join(changes_dir, "0.1.0.jsonl")
        released = parse_jsonl(versioned_path)
        assert [e.description for e in released] == ["Alpha first release"]
        # Released changelog files are read-only, as rlsbl locks them.
        assert not os.access(versioned_path, os.W_OK)
        assert os.path.isfile(os.path.join(changes_dir, "0.1.0.md"))

        # beta was not named -- it still gets an empty unreleased.jsonl.
        beta_changes = get_releasable_changes_dir(str(ns.root), "beta")
        assert os.path.isfile(os.path.join(beta_changes, "unreleased.jsonl"))
        assert parse_jsonl(os.path.join(beta_changes, "unreleased.jsonl")) == []

    def test_released_version_gets_archive(self, multi_releasable_monorepo_factory):
        """A version with a changelog file also gets its release-file archive,
        which is where the description and context are read back from."""
        ns = multi_releasable_monorepo_factory(
            releasable_changes={"alpha": {"versions": {"0.1.0": []}}},
        )
        releases_dir = os.path.join(get_releasable_dir(str(ns.root), "alpha"), "releases")
        archive = os.path.join(releases_dir, "v0.1.0.toml")
        assert os.path.isfile(archive)
        assert not os.access(archive, os.W_OK)

    def test_release_file_written_on_request(self, multi_releasable_monorepo_factory):
        ns = multi_releasable_monorepo_factory(
            releasable_releases={"alpha": {"unreleased": DEFAULT_RELEASE_FILE}},
        )
        rel_dir = get_releasable_dir(str(ns.root), "alpha")
        unreleased_toml = os.path.join(rel_dir, "releases", "unreleased.toml")
        assert os.path.isfile(unreleased_toml)
        assert 'bump = "patch"' in open(unreleased_toml).read()
        # beta was not named: releases/ exists but holds no release file.
        beta_releases = os.path.join(get_releasable_dir(str(ns.root), "beta"), "releases")
        assert os.path.isdir(beta_releases)
        assert not os.path.exists(os.path.join(beta_releases, "unreleased.toml"))

    def test_releasable_state_is_committed(self, multi_releasable_monorepo_factory):
        """The state the fixture writes is committed, so operations that read
        committed state (clone, filter-repo) see it."""
        import subprocess

        ns = multi_releasable_monorepo_factory(
            releasable_changes={"alpha": {"versions": {"0.1.0": []}}},
        )
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ns.root), capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == ""


class TestMakeReleasableState:
    """make_releasable_state on its own, against a bare directory."""

    def test_writes_full_state_directory(self, tmp_path):
        rel_dir = make_releasable_state(
            tmp_path,
            "core",
            version="0.4.2",
            config={"publish_mode": "ci"},
            unreleased_entries=[
                ChangelogEntry(commits=["abc1234"], user_facing=False),
            ],
            versioned_entries={"0.4.1": []},
            release_file=DEFAULT_RELEASE_FILE,
            hooks={"pre-checks.sh": "#!/bin/bash\nexit 0\n"},
        )
        assert rel_dir == tmp_path / ".rlsbl-monorepo" / "releasables" / "core"
        assert (rel_dir / "version").read_text().strip() == "0.4.2"
        assert (rel_dir / "changes" / "unreleased.jsonl").is_file()
        assert (rel_dir / "changes" / "0.4.1.jsonl").is_file()
        assert (rel_dir / "releases" / "unreleased.toml").is_file()
        assert (rel_dir / "releases" / "v0.4.1.toml").is_file()
        assert json.loads((rel_dir / "config.json").read_text())["publish_mode"] == "ci"
        assert os.access(str(rel_dir / "hooks" / "pre-checks.sh"), os.X_OK)

    def test_version_md_is_the_generator_s_own_output(self, tmp_path):
        """The per-version .md is produced by the production generator over the
        just-written JSONL plus the archive -- not a fixed stub.

        A fixture that hand-rolls the .md drifts from what a real release
        writes the moment the generator's format changes, so the fixture calls
        the generator instead of imitating it.
        """
        from rlsbl.changelog.generate import generate_version_section

        entries = [
            ChangelogEntry(
                commits=["cafebabe"], user_facing=True,
                description="Shipped the thing", type="feature",
            ),
            ChangelogEntry(
                commits=["deadbeef"], user_facing=True,
                description="Stopped the crash", type="fix",
            ),
        ]
        rel_dir = make_releasable_state(
            tmp_path, "core", versioned_entries={"0.2.0": entries},
        )
        md = (rel_dir / "changes" / "0.2.0.md").read_text()

        # Byte-for-byte what the generator produces for these entries plus the
        # archive's description ("Test release", from DEFAULT_RELEASE_FILE).
        expected = generate_version_section(
            "0.2.0", parse_jsonl(str(rel_dir / "changes" / "0.2.0.jsonl")),
            description="Test release", context="", bump_type="patch",
        )
        assert md == expected
        assert "### Features" in md
        assert "- Shipped the thing" in md
        assert "### Fixes" in md
        assert "- Stopped the crash" in md
        assert "Test release" in md
        assert "No user-facing changes" not in md

    def test_version_md_reflects_a_custom_archive(self, tmp_path):
        """The archive's description and context reach the .md, because the
        generator reads them from the archive the fixture just wrote."""
        rel_dir = make_releasable_state(
            tmp_path,
            "core",
            versioned_entries={"0.3.0": []},
            archived_releases={
                "0.3.0": (
                    "format_version = 1\n"
                    'bump = "minor"\n'
                    'description = "The custom description"\n'
                    'context = "Why it happened"\n'
                ),
            },
        )
        md = (rel_dir / "changes" / "0.3.0.md").read_text()
        assert "The custom description" in md
        assert "<summary>Context</summary>" in md
        assert "Why it happened" in md

    def test_version_md_is_writable_like_production(self, tmp_path):
        """rlsbl locks the released .jsonl (0444) but leaves the .md writable.

        The .md is a regenerated derivative; the release regenerates it on every
        changelog pass, so a fixture that locks it models a state the tools
        never produce.
        """
        rel_dir = make_releasable_state(
            tmp_path, "core", versioned_entries={"0.1.0": []},
        )
        md_path = rel_dir / "changes" / "0.1.0.md"
        jsonl_path = rel_dir / "changes" / "0.1.0.jsonl"

        current_umask = os.umask(0)
        os.umask(current_umask)
        assert (md_path.stat().st_mode & 0o777) == (0o666 & ~current_umask)
        assert os.access(str(md_path), os.W_OK)
        # The JSONL beside it stays locked, as rlsbl locks it.
        assert not os.access(str(jsonl_path), os.W_OK)

    def test_explicit_archive_overrides_default(self, tmp_path):
        rel_dir = make_releasable_state(
            tmp_path,
            "core",
            versioned_entries={"0.1.0": []},
            archived_releases={"0.1.0": 'bump = "minor"\ndescription = "custom"\n'},
        )
        body = (rel_dir / "releases" / "v0.1.0.toml").read_text()
        assert 'description = "custom"' in body

    def test_rewriting_a_released_version_is_refused(self, tmp_path):
        """A second call naming an already-written released version is a hard
        error naming the file, never a silent unlock-and-rewrite.

        Released state is immutable: rlsbl locks a version's .jsonl at
        finalization. A fixture that quietly rewrote it would let a test
        corrupt the premise it is asserting against. Before the guard this
        died with a bare PermissionError from the 0444 open().
        """
        make_releasable_state(tmp_path, "core", versioned_entries={"0.1.0": []})

        with pytest.raises(ValueError) as exc_info:
            make_releasable_state(
                tmp_path, "core", versioned_entries={"0.1.0": []},
            )
        msg = str(exc_info.value)
        assert "0.1.0.jsonl" in msg
        assert "never overwrites released state" in msg

    def test_rewriting_a_released_archive_is_refused(self, tmp_path):
        """The archived release file is locked too, so re-archiving a version
        whose JSONL was left writable is refused just the same."""
        make_releasable_state(
            tmp_path, "core",
            versioned_entries={"0.1.0": []}, lock_versioned=False,
        )

        with pytest.raises(ValueError, match="v0.1.0.toml"):
            make_releasable_state(
                tmp_path, "core",
                versioned_entries={"0.1.0": []}, lock_versioned=False,
            )

    def test_a_second_call_adding_a_new_version_is_allowed(self, tmp_path):
        """The guard refuses only a REWRITE: adding a version alongside the
        released ones is how a fixture layers state in two passes."""
        make_releasable_state(tmp_path, "core", versioned_entries={"0.1.0": []})
        rel_dir = make_releasable_state(
            tmp_path, "core",
            versioned_entries={"0.2.0": []},
            unreleased_entries=[
                ChangelogEntry(commits=["abc1234"], user_facing=False),
            ],
        )
        assert (rel_dir / "changes" / "0.1.0.jsonl").is_file()
        assert (rel_dir / "changes" / "0.2.0.jsonl").is_file()
        assert len(parse_jsonl(str(rel_dir / "changes" / "unreleased.jsonl"))) == 1

    def test_refusal_happens_before_any_write(self, tmp_path):
        """The guard runs up front, so a refused call leaves the state dir
        exactly as it was -- no half-applied second pass."""
        make_releasable_state(tmp_path, "core", versioned_entries={"0.1.0": []})
        changes_dir = get_releasable_changes_dir(str(tmp_path), "core")
        before = os.listdir(changes_dir)

        with pytest.raises(ValueError):
            make_releasable_state(
                tmp_path, "core",
                versioned_entries={"0.1.0": [], "0.9.9": []},
                unreleased_entries=[
                    ChangelogEntry(commits=["abc1234"], user_facing=False),
                ],
            )

        assert sorted(os.listdir(changes_dir)) == sorted(before)
        assert parse_jsonl(os.path.join(changes_dir, "unreleased.jsonl")) == []

    def test_raw_line_entries_are_written_verbatim(self, tmp_path):
        """A raw string entry lands on disk untouched, so a fixture can plant a
        legacy line (no format_version) or a deliberately malformed one."""
        rel_dir = make_releasable_state(
            tmp_path,
            "core",
            unreleased_entries=['{"commits": ["abc1234"], "user_facing": false}'],
        )
        line = (rel_dir / "changes" / "unreleased.jsonl").read_text().strip()
        assert line == '{"commits": ["abc1234"], "user_facing": false}'


class TestMakeReleasableMonorepo:
    """make_releasable_monorepo builds the repo at an arbitrary root."""

    def test_builds_at_nested_root(self, tmp_path):
        ns = make_releasable_monorepo(tmp_path / "mono")
        assert ns.root == tmp_path / "mono"
        projects = load_workspace(str(ns.root))
        assert {r.name for r in load_releasables(str(ns.root), projects)} == {
            "alpha", "beta",
        }
        # Sibling paths outside the repo stay free for extract targets.
        assert not (tmp_path / "out").exists()

    def test_custom_releasables_and_projects(self, tmp_path):
        from rlsbl.workspace import Releasable

        ns = make_releasable_monorepo(
            tmp_path / "mono",
            releasables=[Releasable(name="core")],
            projects=[
                {"path": "pkgA", "name": "pkgA", "releasable": "core"},
                {"path": "pkgB", "name": "pkgB", "releasable": "core"},
            ],
        )
        projects = load_workspace(str(ns.root))
        assert {m.name for m in members_of("core", projects)} == {"pkgA", "pkgB"}
        assert read_releasable_version(str(ns.root), "core") == ns.initial_version
