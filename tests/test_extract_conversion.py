"""``rlsbl monorepo extract`` -- the releasable-level repository conversion.

The command observes the whole conversion, renders it as a plan under
``--dry-run``, and applies that plan item by item. These tests cover both
halves: what the plan says and refuses, and what an apply actually moves --
history, tree-object identity, the releasable's whole release state, the
anchors, the tags, the lineage record, and what the source loses.

The apply half needs git-filter-repo, which is not resolvable inside the
sandbox runner, so those classes carry the established skip marker and are
exercised by a bare scoped run.
"""

import json
import os
import pathlib
import shutil

import pytest

from conftest import make_releasable_monorepo, run_git, write_jsonl
from githarness import git as gitout
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl
from rlsbl.commands.monorepo.extract import ExtractError
from rlsbl.commands.monorepo.extract_cmd import (
    ITEM_DEPENDENCIES,
    ITEM_DESTINATION,
    ITEM_LINEAGE,
    ITEM_RELEASABLE,
    ITEM_SOURCE,
    ITEM_STATE,
    ITEM_TAGS,
    ITEM_TREES,
    cmd_extract,
)
from rlsbl.dep_floors import CONFIG_KEY
from rlsbl.lineage import (
    KIND_ANCHOR_REMAP,
    KIND_BOUNDARY_ALIAS,
    KIND_CONVERSION,
    KIND_DEPARTED_GLOBS,
    KIND_TAG_MAP,
    get_lineage_path,
    read_events,
)
from rlsbl.release_file import read_release_file, write_release_anchor
from rlsbl.workspace import (
    WORKSPACE_DIR,
    Releasable,
    get_releasable_dir,
    load_releasables,
    load_standalone_releasable,
    load_workspace,
)

HAS_FILTER_REPO = shutil.which("git-filter-repo") is not None
skip_no_filter_repo = pytest.mark.skipif(
    not HAS_FILTER_REPO,
    reason="git-filter-repo not installed",
)

# The WHOLE module, not just the apply half: the conversion refuses a missing
# git-filter-repo during observation, so even the plan and the refusals need it
# on PATH. The sandbox runner cannot resolve it, so this file is exercised by a
# bare scoped run.
pytestmark = skip_no_filter_repo


# ---------------------------------------------------------------------------
# Fixture: a releasable monorepo with real releasable state
# ---------------------------------------------------------------------------


def _commit(root, files, message):
    """Write files and commit them together; return the commit SHA."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        run_git(root, "add", rel)
    run_git(root, "commit", "-q", "-m", message)
    return gitout(root, "rev-parse", "HEAD")


def _tree(root, path):
    return gitout(root, "rev-parse", f"HEAD:{path}")


def _tags(repo):
    return gitout(repo, "tag", "-l").split()


def _anchor_archive(root, releasable, version, sha, tree_hashes):
    """Put a release anchor on an already-archived (locked) release file."""
    path = os.path.join(
        get_releasable_dir(str(root), releasable), "releases", f"v{version}.toml",
    )
    os.chmod(path, 0o644)
    write_release_anchor(path, candidate_sha=sha, tree_hashes=tree_hashes)
    os.chmod(path, 0o444)
    return path


def make_source(tmp_path, *, projects=None, releasables=None, name="mono"):
    """A monorepo with releasables ``core`` (pkgA, pkgB) and ``extras`` (pkgC).

    Both carry the real releasable layout -- version, changes/, releases/ with
    the archived v0.1.0 -- and are tagged at 0.1.0. One further commit touches
    both core members, is referenced by core's unreleased changelog entry, and
    is the commit core's release anchor names, so an extract has real hashes and
    real anchors to remap rather than an empty state directory.
    """
    root = tmp_path / name
    if releasables is None:
        releasables = [Releasable(name="core"), Releasable(name="extras")]
    if projects is None:
        projects = [
            {"path": "pkgA", "name": "pkgA", "releasable": "core"},
            {"path": "pkgB", "name": "pkgB", "releasable": "core"},
            {"path": "pkgC", "name": "pkgC", "releasable": "extras"},
        ]
    ns = make_releasable_monorepo(root, releasables=releasables, projects=projects)

    sha = _commit(
        root,
        {
            "pkgA/main.py": "print('A')\n",
            "pkgB/main.py": "print('B')\n",
        },
        "feat: core work",
    )
    changes = os.path.join(get_releasable_dir(str(root), "core"), "changes")
    write_jsonl(
        os.path.join(changes, "unreleased.jsonl"),
        [ChangelogEntry(
            commits=[sha], user_facing=True,
            description="Core work", type="feature",
        )],
    )
    _anchor_archive(
        root, "core", ns.initial_version, sha,
        {"pkgA": _tree(root, "pkgA"), "pkgB": _tree(root, "pkgB")},
    )
    run_git(root, "add", WORKSPACE_DIR)
    run_git(root, "commit", "-q", "-m", "core state")
    ns.core_sha = sha
    return ns


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


class TestPreview:
    def test_dry_run_renders_every_concern_and_writes_nothing(
        self, tmp_path, capsys,
    ):
        ns = make_source(tmp_path)
        head = gitout(ns.root, "rev-parse", "HEAD")
        target = tmp_path / "core_out"

        preview = cmd_extract(str(ns.root), "core", str(target), dry_run=True)

        assert list(preview.keys) == [
            ITEM_RELEASABLE, ITEM_DEPENDENCIES, ITEM_TREES, ITEM_STATE,
            ITEM_TAGS, ITEM_DESTINATION, ITEM_LINEAGE, ITEM_SOURCE,
            "next-steps",
        ]
        out = capsys.readouterr().out
        assert "extract-to-workspace" in out
        assert "pkgA" in out and "pkgB" in out

        assert not target.exists()
        assert gitout(ns.root, "rev-parse", "HEAD") == head
        assert [p.name for p in load_workspace(str(ns.root))].count("pkgA") == 1

    def test_single_member_plan_translates_tags_and_names_the_alias(
        self, tmp_path, capsys,
    ):
        ns = make_source(tmp_path)
        preview = cmd_extract(
            str(ns.root), "extras", str(tmp_path / "extras_out"), dry_run=True,
        )
        item = preview.by_key(ITEM_TAGS)
        assert item.state == "translate_tags"
        facts = "\n".join(item.facts)
        assert "extras@v0.1.0 -> v0.1.0" in facts
        assert "boundary alias" in facts
        assert preview.by_key(ITEM_RELEASABLE).state == "extract_to_standalone"

    def test_multi_member_keeps_its_tag_format(self, tmp_path):
        ns = make_source(tmp_path)
        preview = cmd_extract(
            str(ns.root), "core", str(tmp_path / "core_out"), dry_run=True,
        )
        assert preview.by_key(ITEM_TAGS).state == "tags_unchanged"

    def test_plan_names_the_state_the_transplant_carries(self, tmp_path):
        ns = make_source(tmp_path)
        preview = cmd_extract(
            str(ns.root), "core", str(tmp_path / "core_out"), dry_run=True,
        )
        facts = "\n".join(preview.by_key(ITEM_STATE).facts)
        assert "changes" in facts and "releases" in facts and "config.json" in facts
        assert f"v{ns.initial_version}.toml" in facts

    def test_standalone_plan_does_not_carry_the_version_file(self, tmp_path):
        ns = make_source(tmp_path)
        preview = cmd_extract(
            str(ns.root), "extras", str(tmp_path / "extras_out"), dry_run=True,
        )
        facts = "\n".join(preview.by_key(ITEM_STATE).facts)
        assert "not carried" in facts and "version" in facts

    def test_next_steps_name_the_trusted_publisher_path_for_pypi(self, tmp_path):
        ns = make_source(tmp_path)
        preview = cmd_extract(
            str(ns.root), "core", str(tmp_path / "core_out"), dry_run=True,
        )
        facts = "\n".join(preview.by_key("next-steps").facts)
        assert "pypi.org/manage/account/publishing/" in facts
        assert "rlsbl release retry" in facts
        assert "rlsbl scaffold" in facts


# ---------------------------------------------------------------------------
# Refusals -- every one of them fires during observation, before any write
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_unknown_releasable(self, tmp_path):
        ns = make_source(tmp_path)
        with pytest.raises(ExtractError, match="not found in this workspace"):
            cmd_extract(str(ns.root), "nope", str(tmp_path / "out"))

    def test_target_path_exists(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "out"
        target.mkdir()
        with pytest.raises(ExtractError, match="target path already exists"):
            cmd_extract(str(ns.root), "core", str(target))

    def test_dirty_source_tree(self, tmp_path):
        ns = make_source(tmp_path)
        (ns.root / "pkgA" / "scratch.py").write_text("x\n")
        with pytest.raises(ExtractError, match="uncommitted changes"):
            cmd_extract(str(ns.root), "core", str(tmp_path / "out"))

    def test_mirrored_releasable(self, tmp_path):
        ns = make_source(
            tmp_path,
            projects=[
                {"path": "pkgA", "name": "pkgA", "releasable": "core",
                 "subtree_remote": "git@github.com:o/pkga.git"},
                {"path": "pkgB", "name": "pkgB", "releasable": "core"},
                {"path": "pkgC", "name": "pkgC", "releasable": "extras"},
            ],
        )
        with pytest.raises(ExtractError) as exc:
            cmd_extract(str(ns.root), "core", str(tmp_path / "out"))
        assert "mirrored" in str(exc.value)
        assert "subtree_remote" in str(exc.value)

    def test_releasable_owning_the_root_member(self, tmp_path):
        ns = make_source(
            tmp_path,
            releasables=[
                Releasable(name="core", tag_format="v{version}"),
                Releasable(name="extras"),
            ],
            projects=[
                {"path": ".", "name": "root", "releasable": "core"},
                {"path": "pkgA", "name": "pkgA", "releasable": "core"},
                {"path": "pkgC", "name": "pkgC", "releasable": "extras"},
            ],
        )
        with pytest.raises(ExtractError) as exc:
            cmd_extract(str(ns.root), "core", str(tmp_path / "out"))
        assert "owns the repository root" in str(exc.value)

    def test_release_in_flight(self, tmp_path):
        ns = make_source(tmp_path)
        state = os.path.join(
            get_releasable_dir(str(ns.root), "core"), "releases",
            "in-progress.json",
        )
        with open(state, "w", encoding="utf-8") as f:
            f.write("{}\n")
        run_git(ns.root, "add", WORKSPACE_DIR)
        run_git(ns.root, "commit", "-q", "-m", "in flight")
        with pytest.raises(ExtractError, match="release in progress"):
            cmd_extract(str(ns.root), "core", str(tmp_path / "out"))

    def test_tag_collision(self, tmp_path):
        ns = make_source(tmp_path)
        # A standalone v0.1.0 already present collides with the translation of
        # extras@v0.1.0 into the standalone scheme.
        run_git(ns.root, "tag", "v0.1.0")
        with pytest.raises(ExtractError, match="collision"):
            cmd_extract(str(ns.root), "extras", str(tmp_path / "out"))

    def test_inbound_dependency_names_the_rewrite_command(self, tmp_path):
        ns = make_source(tmp_path)
        # pkgC stays behind and depends on pkgA, which would leave.
        (ns.root / "pkgC" / "pyproject.toml").write_text(
            '[project]\nname = "pkgC"\nversion = "0.1.0"\n'
            'dependencies = ["pkgA @ file:///' + str(ns.root / "pkgA")[1:] + '"]\n'
        )
        run_git(ns.root, "add", "pkgC")
        run_git(ns.root, "commit", "-q", "-m", "pkgC depends on pkgA")

        with pytest.raises(ExtractError) as exc:
            cmd_extract(str(ns.root), "core", str(tmp_path / "out"))
        message = str(exc.value)
        assert "'pkgC' depends on 'pkgA'" in message
        assert "rlsbl rewrite uv-path-sources" in message
        assert "extract never rewrites a manifest itself" in message

    def test_broken_target_declaration_on_a_remaining_member(self, tmp_path):
        """A member outside every releasable derives its glob from its targets.

        A releasable member's tag scheme comes from the releasable, so a broken
        config never reaches detection for one. The root member is the case that
        does: it stands outside every releasable, and a config file that exists
        while declaring no targets is a hard error by rlsbl's own rule. Refusing
        HERE means it fires before any history is rewritten, instead of pruning
        the wrong tags on a guessed scheme.
        """
        ns = make_source(tmp_path)
        root_config = ns.root / ".rlsbl" / "config.json"
        root_config.parent.mkdir(exist_ok=True)
        root_config.write_text(json.dumps({"publish_mode": "none"}) + "\n")
        run_git(ns.root, "add", ".rlsbl")
        run_git(ns.root, "commit", "-q", "-m", "root config without targets")

        with pytest.raises(ExtractError, match="broken target declaration"):
            cmd_extract(str(ns.root), "core", str(tmp_path / "out"))


class TestDeletionConsent:
    """saferm present / absent-with-flag / absent-without-flag.

    Absence is simulated with a PATH holding only the tools the conversion
    genuinely needs, so ``shutil.which("saferm")`` really answers None.
    """

    def _path_without_saferm(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin_no_saferm"
        bindir.mkdir()
        for tool in ("git", "git-filter-repo"):
            resolved = shutil.which(tool)
            if resolved:
                os.symlink(resolved, bindir / tool)
        monkeypatch.setenv("PATH", str(bindir))

    def test_absent_without_the_flag_refuses_naming_both_remedies(
        self, tmp_path, monkeypatch,
    ):
        ns = make_source(tmp_path)
        self._path_without_saferm(tmp_path, monkeypatch)
        with pytest.raises(ExtractError) as exc:
            cmd_extract(str(ns.root), "core", str(tmp_path / "out"))
        message = str(exc.value)
        assert "saferm is not installed" in message
        assert "Install saferm" in message
        assert "--delete-with-rm" in message

    def test_absent_with_the_flag_proceeds(self, tmp_path, monkeypatch):
        ns = make_source(tmp_path)
        self._path_without_saferm(tmp_path, monkeypatch)
        preview = cmd_extract(
            str(ns.root), "core", str(tmp_path / "out"),
            dry_run=True, delete_with_rm=True,
        )
        assert "rm -rf" in "\n".join(preview.by_key(ITEM_SOURCE).facts)

    def test_present_plans_a_saferm_deletion(self, tmp_path, monkeypatch):
        ns = make_source(tmp_path)
        bindir = tmp_path / "bin_with_saferm"
        bindir.mkdir()
        for tool in ("git", "git-filter-repo"):
            resolved = shutil.which(tool)
            if resolved:
                os.symlink(resolved, bindir / tool)
        (bindir / "saferm").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(bindir / "saferm", 0o755)
        monkeypatch.setenv("PATH", str(bindir))

        preview = cmd_extract(
            str(ns.root), "core", str(tmp_path / "out"), dry_run=True,
        )
        assert "saferm" in "\n".join(preview.by_key(ITEM_SOURCE).facts)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@skip_no_filter_repo
class TestApplyMultiMember:
    def test_destination_is_a_workspace_that_loads(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "core_out"

        cmd_extract(str(ns.root), "core", str(target))

        projects = load_workspace(str(target))
        assert {p.name for p in projects} == {"root", "pkgA", "pkgB"}
        releasables = load_releasables(str(target), projects)
        assert [r.name for r in releasables] == ["core"]
        # The tags travelled unchanged, so the format is stated rather than
        # left to the default.
        assert releasables[0].declares_tag_format
        assert releasables[0].tag_format == "{name}@v{version}"
        assert (target / "pkgA" / "main.py").is_file()
        assert (target / "pkgB" / "main.py").is_file()
        assert gitout(target, "status", "--porcelain") == ""

    def test_member_trees_survive_the_filter_unchanged(self, tmp_path):
        ns = make_source(tmp_path)
        expected = {name: _tree(ns.root, name) for name in ("pkgA", "pkgB")}
        target = tmp_path / "core_out"

        cmd_extract(str(ns.root), "core", str(target))

        for name, tree in expected.items():
            assert gitout(target, "rev-parse", f"HEAD:{name}") == tree

    def test_whole_state_directory_is_transplanted(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "core_out"

        cmd_extract(str(ns.root), "core", str(target))

        state = get_releasable_dir(str(target), "core")
        assert os.path.isfile(os.path.join(state, "version"))
        assert os.path.isfile(os.path.join(state, "config.json"))
        assert os.path.isfile(
            os.path.join(state, "changes", "unreleased.jsonl")
        )
        assert os.path.isfile(
            os.path.join(state, "changes", f"{ns.initial_version}.jsonl")
        )
        assert os.path.isfile(
            os.path.join(state, "releases", f"v{ns.initial_version}.toml")
        )

    def test_changelog_hashes_resolve_in_the_new_repository(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "core_out"

        cmd_extract(str(ns.root), "core", str(target))

        unreleased = os.path.join(
            get_releasable_dir(str(target), "core"), "changes", "unreleased.jsonl",
        )
        entries = parse_jsonl(unreleased)
        assert entries, "the core work entry should have survived"
        for entry in entries:
            for sha in entry.commits:
                gitout(target, "cat-file", "-e", sha + "^{commit}")

    def test_release_anchor_is_remapped_and_resolves(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "core_out"

        cmd_extract(str(ns.root), "core", str(target))

        archive = os.path.join(
            get_releasable_dir(str(target), "core"), "releases",
            f"v{ns.initial_version}.toml",
        )
        config = read_release_file(archive)
        assert config.candidate_sha != ns.core_sha  # the rewrite moved it
        gitout(target, "cat-file", "-e", config.candidate_sha + "^{commit}")
        # Cross-check: every recorded tree is the tree at that commit and path.
        assert set(config.tree_hashes) == {"pkgA", "pkgB"}
        for path, tree in config.tree_hashes.items():
            actual = gitout(
                target, "rev-parse", f"{config.candidate_sha}:{path}",
            )
            assert actual == tree
        assert not os.access(archive, os.W_OK), "the archive must stay locked"

    def test_lineage_record_explains_the_conversion(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "core_out"

        cmd_extract(str(ns.root), "core", str(target))

        path = get_lineage_path(
            str(target), releasable_dir=get_releasable_dir(str(target), "core"),
        )
        events = read_events(path)
        kinds = [e.KIND for e in events]
        assert kinds[0] == KIND_CONVERSION
        conversion = events[0]
        assert conversion.direction == "extract"
        assert conversion.destination.repo == "."
        assert conversion.destination.releasable == "core"
        assert conversion.commit
        assert KIND_ANCHOR_REMAP in kinds
        remap = next(e for e in events if e.KIND == KIND_ANCHOR_REMAP)
        assert remap.related_to == conversion.id
        assert remap.mappings[0].old_sha == ns.core_sha
        # A multi-member destination keeps its tag format, so nothing renamed.
        assert KIND_TAG_MAP not in kinds
        assert KIND_BOUNDARY_ALIAS not in kinds

    def test_source_loses_the_members_and_the_state(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "core_out"

        cmd_extract(str(ns.root), "core", str(target))

        projects = load_workspace(str(ns.root))
        assert {p.name for p in projects} == {"root", "pkgC"}
        assert [r.name for r in load_releasables(str(ns.root), projects)] == [
            "extras"
        ]
        assert not (ns.root / "pkgA").exists()
        assert not (ns.root / "pkgB").exists()
        assert not os.path.isdir(get_releasable_dir(str(ns.root), "core"))
        assert gitout(ns.root, "status", "--porcelain") == ""

    def test_source_records_the_departed_globs(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "core_out"

        cmd_extract(str(ns.root), "core", str(target))

        # The departure is a fact about the repository's tag namespace, so it
        # goes in the WORKSPACE-scoped record, not any releasable's.
        events = read_events(get_lineage_path(str(ns.root), workspace=True))
        departed = [e for e in events if e.KIND == KIND_DEPARTED_GLOBS]
        assert len(departed) == 1
        assert departed[0].globs == ["core@v*"]
        assert departed[0].destination.releasable == "core"
        # The tags themselves stay; the record is what explains them.
        assert "core@v0.1.0" in _tags(ns.root)

    def test_departing_names_join_the_remaining_releasables_floors(self, tmp_path):
        """The floors go where the dep-floors check reads them.

        Not the repository root's ``.rlsbl/config.json``: rlsbl's own
        ``root-rlsbl-conflict`` check refuses a root ``.rlsbl/`` next to
        ``.rlsbl-monorepo/``, so a workspace has no such file to write.
        """
        ns = make_source(tmp_path)
        cmd_extract(str(ns.root), "core", str(tmp_path / "core_out"))

        config = json.loads(
            (
                pathlib.Path(get_releasable_dir(str(ns.root), "extras"))
                / "config.json"
            ).read_text()
        )
        assert config[CONFIG_KEY] == ["pkgA", "pkgB"]
        assert not (ns.root / ".rlsbl").exists()

    def test_source_workspace_checks_gain_no_failure(self, tmp_path, monkeypatch):
        """The conversion introduces no new workspace-check failure.

        Compared against the same checks run BEFORE the extract rather than
        against green: the fixture's members carry no CI workflows, so
        ``workspace-ci-synced`` is red either way, and a test that demanded
        green would be asserting something about the fixture instead of about
        the conversion.
        """
        from rlsbl import app

        def failing():
            result = app.test(["check", "--tag", "workspace"])
            return {
                line.split()[1]
                for line in result.stdout.splitlines()
                if line.startswith("FAIL ")
            }

        ns = make_source(tmp_path)
        monkeypatch.chdir(ns.root)
        # Sync once first: the fixture has no CI router, so without this the
        # before-state skips the router checks the after-state runs, and the
        # comparison would be between two different question sets.
        app.test(["monorepo", "sync"])
        before = failing()

        cmd_extract(str(ns.root), "core", str(tmp_path / "core_out"))

        after = failing()
        assert after <= before, f"new failures: {sorted(after - before)}"

    def test_snapshot_is_regenerated(self, tmp_path):
        ns = make_source(tmp_path)
        cmd_extract(str(ns.root), "core", str(tmp_path / "core_out"))

        from rlsbl.snapshot import check_snapshot
        from rlsbl.workspace_graph import WorkspaceGraph

        projects = load_workspace(str(ns.root))
        assert check_snapshot(
            str(ns.root), projects, WorkspaceGraph(str(ns.root), projects),
        )


@skip_no_filter_repo
class TestApplySingleMember:
    def test_destination_is_a_flat_repository_that_identifies_itself(
        self, tmp_path,
    ):
        ns = make_source(tmp_path)
        target = tmp_path / "extras_out"

        cmd_extract(str(ns.root), "extras", str(target))

        assert (target / "pyproject.toml").is_file()
        assert not (target / "pkgC").exists()
        releasable = load_standalone_releasable(str(target))
        assert releasable is not None
        assert releasable.name == "extras"
        assert releasable.tag_format == "v{version}"
        assert gitout(target, "status", "--porcelain") == ""

    def test_state_lands_in_the_standalone_home_without_the_version_file(
        self, tmp_path,
    ):
        ns = make_source(tmp_path)
        target = tmp_path / "extras_out"

        cmd_extract(str(ns.root), "extras", str(target))

        assert (target / ".rlsbl" / "changes" / "unreleased.jsonl").is_file()
        assert (
            target / ".rlsbl" / "releases" / f"v{ns.initial_version}.toml"
        ).is_file()
        assert (target / ".rlsbl" / "config.json").is_file()
        # `.rlsbl/version` is the scaffolding version, not the project's, so
        # the releasable's version file is deliberately not carried over.
        version_file = target / ".rlsbl" / "version"
        assert not version_file.exists() or version_file.read_text().strip() != (
            ns.initial_version
        )

    def test_tags_translate_with_one_boundary_alias(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "extras_out"

        cmd_extract(str(ns.root), "extras", str(target))

        tags = _tags(target)
        assert "v0.1.0" in tags
        # The current version keeps its pre-conversion name beside the new one.
        assert "extras@v0.1.0" in tags
        assert gitout(target, "rev-list", "-n", "1", "v0.1.0") == (
            gitout(target, "rev-list", "-n", "1", "extras@v0.1.0")
        )
        # Another live releasable's tags are pruned.
        assert "core@v0.1.0" not in tags

    def test_lineage_records_the_rename_and_the_alias(self, tmp_path):
        ns = make_source(tmp_path)
        target = tmp_path / "extras_out"

        cmd_extract(str(ns.root), "extras", str(target))

        events = read_events(get_lineage_path(str(target)))
        kinds = [e.KIND for e in events]
        assert kinds[0] == KIND_CONVERSION
        assert KIND_TAG_MAP in kinds
        assert KIND_BOUNDARY_ALIAS in kinds
        tag_map = next(e for e in events if e.KIND == KIND_TAG_MAP)
        assert tag_map.related_to == events[0].id
        assert [(m.old_tag, m.new_tag) for m in tag_map.mappings] == [
            ("extras@v0.1.0", "v0.1.0")
        ]
        alias = next(e for e in events if e.KIND == KIND_BOUNDARY_ALIAS)
        assert alias.aliases[0].alias_tag == "v0.1.0"
        assert alias.aliases[0].aliased_tag == "extras@v0.1.0"

    def test_anchor_paths_are_rekeyed_to_the_repository_root(self, tmp_path):
        ns = make_source(tmp_path)
        # Anchored at a commit that touches pkgC, so it survives the filter.
        sha = _commit(ns.root, {"pkgC/main.py": "print('C')\n"}, "feat: extras")
        _anchor_archive(
            ns.root, "extras", ns.initial_version, sha,
            {"pkgC": _tree(ns.root, "pkgC")},
        )
        run_git(ns.root, "add", WORKSPACE_DIR)
        run_git(ns.root, "commit", "-q", "-m", "extras anchor")

        target = tmp_path / "extras_out"
        cmd_extract(str(ns.root), "extras", str(target))

        config = read_release_file(
            str(target / ".rlsbl" / "releases" / f"v{ns.initial_version}.toml")
        )
        assert set(config.tree_hashes) == {"."}
        assert gitout(
            target, "rev-parse", f"{config.candidate_sha}^{{tree}}",
        ) == config.tree_hashes["."]


@skip_no_filter_repo
class TestTreeVerification:
    def test_a_mismatch_names_both_hashes_and_stops(self, tmp_path, monkeypatch):
        ns = make_source(tmp_path)
        target = tmp_path / "core_out"

        import rlsbl.commands.monorepo.extract_cmd as mod

        real = mod._tree_hash

        def lying(repo, path, rev="HEAD"):
            if os.path.abspath(str(repo)) == str(target):
                return "0" * 40
            return real(repo, path, rev=rev)

        monkeypatch.setattr(mod, "_tree_hash", lying)

        with pytest.raises(ExtractError) as exc:
            cmd_extract(str(ns.root), "core", str(target))
        message = str(exc.value)
        assert "tree verification failed" in message
        # Both hashes are named: what the source has, and what arrived.
        assert _tree(ns.root, "pkgA") in message
        assert "0" * 40 in message

        # Nothing further was written: the source is intact.
        assert (ns.root / "pkgA").exists()
        assert "pkgA" in [p.name for p in load_workspace(str(ns.root))]


@skip_no_filter_repo
class TestConcurrency:
    def test_the_workspace_lock_is_taken_and_refuses_a_held_one(
        self, tmp_path, monkeypatch,
    ):
        """The conversion takes the lock a release takes, and refuses to queue."""
        ns = make_source(tmp_path)
        import fcntl

        lock_dir = ns.root / WORKSPACE_DIR
        lock_dir.mkdir(exist_ok=True)
        holder = open(lock_dir / "lock", "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            from rlsbl.lock import LockHeldError

            # A held lock is only reached in the apply half; the plan itself is
            # readable either way.
            with pytest.raises(LockHeldError):
                cmd_extract(str(ns.root), "core", str(tmp_path / "core_out"))
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
