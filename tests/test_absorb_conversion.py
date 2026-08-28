"""``rlsbl monorepo absorb`` -- the inbound repository conversion.

The command observes the whole conversion, renders it as a plan under
``--dry-run``, and applies that plan item by item. These tests cover both
halves: what the plan says and refuses, and what an apply actually moves --
history, tags, the release state with its anchors, the workspace entry, the
lineage record -- plus the property the rebuild exists for: a crashed run can
be re-run to completion without duplicating anything.

The whole module needs git-filter-repo, which the conversion refuses without
even during observation, and which is not resolvable inside the sandbox runner;
it is exercised by a bare scoped run. The validation-level refusals that do not
need it are in ``tests/test_releasable_extract.py``.
"""

import json
import os
import shutil

import pytest

from conftest import make_releasable_monorepo, run_git, write_jsonl
from githarness import git as gitout
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl
from rlsbl.commands.monorepo.absorb_cmd import (
    ABSORB_TRAILER,
    ITEM_HISTORY,
    ITEM_RELEASABLE,
    ITEM_SOURCE,
    ITEM_STATE,
    ITEM_TAGS,
    ITEM_WORKSPACE,
    AbsorbError,
    cmd_absorb,
)
from rlsbl.lineage import (
    KIND_ANCHOR_REMAP,
    KIND_BOUNDARY_ALIAS,
    KIND_CONVERSION,
    KIND_TAG_MAP,
    get_lineage_path,
    read_events,
)
from rlsbl.release_file import (
    read_release_file,
    write_archived_release_file,
    write_release_anchor,
)
from rlsbl.workspace import (
    Releasable,
    get_releasable_dir,
    load_releasables,
    load_workspace,
)

HAS_FILTER_REPO = shutil.which("git-filter-repo") is not None
skip_no_filter_repo = pytest.mark.skipif(
    not HAS_FILTER_REPO,
    reason="git-filter-repo not installed",
)
pytestmark = skip_no_filter_repo


# ---------------------------------------------------------------------------
# Fixtures: a released standalone source, and a releasable workspace
# ---------------------------------------------------------------------------


def _commit(root, files, message):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        run_git(root, "add", rel)
    run_git(root, "commit", "-q", "-m", message)
    return gitout(root, "rev-parse", "HEAD")


def _tags(repo):
    return gitout(repo, "tag", "-l").split()


def make_source(tmp_path, *, name="widget", version="0.1.0", targets=("npm",)):
    """A released standalone repository, ready to be absorbed.

    It carries what a real one carries: a manifest at the released version, a
    ``.rlsbl/config.json`` naming its targets, a locked released changelog with
    its generated markdown, an ANCHORED release archive for that version, one
    unreleased entry over a later commit, and the ``v<version>`` tag.
    """
    repo = tmp_path / f"{name}_src"
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")

    manifest = (
        {"package.json": json.dumps({"name": name, "version": version}) + "\n"}
        if "npm" in targets else
        {"pyproject.toml": f'[project]\nname = "{name}"\nversion = "{version}"\n'}
    )
    released = _commit(
        repo,
        {
            **manifest,
            "src/index.js": "export const v = 1;\n",
            ".rlsbl/config.json": json.dumps(
                {"publish_mode": "ci", "targets": list(targets)}
            ) + "\n",
        },
        "feat: the first release",
    )
    changes = repo / ".rlsbl" / "changes"
    write_jsonl(
        str(changes / f"{version}.jsonl"),
        [ChangelogEntry(
            commits=[released], user_facing=True,
            description="The first release", type="feature",
        )],
        lock=True,
    )
    (changes / f"{version}.md").write_text(
        f"## {version}\n\n### Features\n- The first release\n"
    )
    write_archived_release_file(
        str(repo / ".rlsbl" / "releases"), version,
        bump="minor", include=[], description="the first release",
        candidate_sha=released,
        tree_hashes={".": gitout(repo, "rev-parse", f"{released}^{{tree}}")},
    )
    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "chore: release state")
    run_git(repo, "tag", f"v{version}")

    wip = _commit(repo, {"src/wip.js": "export const w = 2;\n"}, "feat: wip")
    write_jsonl(
        str(changes / "unreleased.jsonl"),
        [ChangelogEntry(
            commits=[wip], user_facing=True,
            description="Work in progress", type="feature",
        )],
    )
    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "changelog: wip")
    return repo


def make_destination(tmp_path, name="mono"):
    """A releasable workspace with one member of its own to stay behind."""
    root = tmp_path / name
    return make_releasable_monorepo(
        root,
        releasables=[Releasable(name="core")],
        projects=[{"path": "pkgA", "name": "pkgA", "releasable": "core"}],
    )


def absorb(ns, source, dest="packages/widget", **kwargs):
    return cmd_absorb(str(ns.root), str(source), dest, **kwargs)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


class TestPreview:
    def test_dry_run_renders_every_concern_and_writes_nothing(
        self, tmp_path, capsys,
    ):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        head = gitout(ns.root, "rev-parse", "HEAD")

        preview = absorb(ns, source, dry_run=True)

        assert list(preview.keys) == [
            ITEM_SOURCE, ITEM_RELEASABLE, ITEM_HISTORY, ITEM_TAGS, ITEM_STATE,
            ITEM_WORKSPACE, "lineage", "next-steps",
        ]
        out = capsys.readouterr().out
        assert "rewrite-history" in out
        assert "create-releasable" in out
        # Nothing moved: no commit, no member, no directory, no tag.
        assert gitout(ns.root, "rev-parse", "HEAD") == head
        assert "widget" not in [p.name for p in load_workspace(str(ns.root))]
        assert not (ns.root / "packages").exists()
        assert _tags(ns.root) == ["core@v0.1.0"]

    def test_the_plan_states_the_created_releasables_tag_format(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        preview = absorb(ns, source, dry_run=True)

        item = preview.by_key(ITEM_RELEASABLE)
        assert item.state == "create_releasable"
        assert "{name}@v{version}" in "\n".join(item.facts)
        assert "written explicitly" in "\n".join(item.facts)

    def test_a_go_source_derives_the_path_scheme(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path, targets=("go",))
        (source / "go.mod").write_text("module example.com/widget\n\ngo 1.22\n")
        run_git(source, "add", "go.mod")
        run_git(source, "commit", "-q", "-m", "chore: go.mod")

        preview = absorb(ns, source, dry_run=True)

        facts = "\n".join(preview.by_key(ITEM_RELEASABLE).facts)
        assert "packages/widget/v{version}" in facts

    def test_joining_an_existing_releasable_keeps_its_format(self, tmp_path):
        ns = make_destination(tmp_path)
        # A version core has not released, so the join is not a collision.
        source = make_source(tmp_path, version="0.4.0")

        preview = absorb(ns, source, releasable_name="core", dry_run=True)

        item = preview.by_key(ITEM_RELEASABLE)
        assert item.state == "join_releasable"
        assert "the releasable's own" in "\n".join(item.facts)

    def test_the_plan_names_the_tags_and_the_boundary_alias(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        preview = absorb(ns, source, dry_run=True)

        facts = "\n".join(preview.by_key(ITEM_TAGS).facts)
        assert "v0.1.0 -> widget@v0.1.0" in facts
        assert "boundary alias: v0.1.0 is created beside widget@v0.1.0" in facts

    def test_the_plan_names_the_state_the_migration_carries(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        facts = "\n".join(absorb(ns, source, dry_run=True).by_key(ITEM_STATE).facts)
        assert "unreleased entries: 1" in facts
        assert "released changelogs: 0.1.0" in facts
        assert "release archives: v0.1.0.toml" in facts

    def test_next_steps_name_the_trusted_publisher_path_for_pypi(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path, targets=("pypi",))

        facts = "\n".join(
            absorb(ns, source, dry_run=True).by_key("next-steps").facts
        )
        assert "PyPI" in facts
        assert "pypi.org/manage/account/publishing" in facts


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_a_dirty_source(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        (source / "dirty.txt").write_text("x\n")

        with pytest.raises(AbsorbError, match="uncommitted changes"):
            absorb(ns, source, dry_run=True)

    def test_a_dirty_workspace(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        (ns.root / "dirty.txt").write_text("x\n")

        with pytest.raises(AbsorbError, match="workspace has uncommitted"):
            absorb(ns, source, dry_run=True)

    def test_a_destination_path_that_exists_on_disk(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        # A directory no member declares -- the root member owns it -- so the
        # refusal is about the DIRECTORY, not about a workspace entry.
        _commit(ns.root, {"vendor/keep.txt": "keep\n"}, "chore: vendor")

        with pytest.raises(AbsorbError, match="already exists on disk"):
            absorb(ns, source, dest="vendor", dry_run=True)

    def test_a_member_name_already_taken(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        with pytest.raises(AbsorbError, match="already exists in workspace"):
            absorb(ns, source, dest="packages/other", name="pkgA", dry_run=True)

    def test_the_repository_root_as_a_destination(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        with pytest.raises(AbsorbError, match="repository root"):
            absorb(ns, source, dest=".", dry_run=True)

    def test_an_unknown_releasable(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        with pytest.raises(AbsorbError, match="not defined in \\[\\[releasables\\]\\]"):
            absorb(ns, source, releasable_name="nope", dry_run=True)

    def test_a_releasable_name_that_already_exists(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path, name="core")

        with pytest.raises(AbsorbError, match="already exists in this"):
            absorb(ns, source, dest="packages/core", dry_run=True)

    def test_tag_format_together_with_an_existing_releasable(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        with pytest.raises(AbsorbError, match="applies only to the releasable"):
            absorb(
                ns, source, releasable_name="core",
                tag_format="{name}@v{version}", dry_run=True,
            )

    def test_a_tag_name_collision(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        run_git(ns.root, "tag", "widget@v0.1.0")

        with pytest.raises(AbsorbError, match="tag collision"):
            absorb(ns, source, dry_run=True)

    def test_a_boundary_alias_collision(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        run_git(ns.root, "tag", "v0.1.0")

        with pytest.raises(AbsorbError, match="boundary alias collision"):
            absorb(ns, source, dry_run=True)

    def test_a_version_the_releasable_has_already_released(self, tmp_path):
        """The refusal reads the LEDGER, not the tag names.

        ``core`` has released 0.1.0 -- it has the locked changelog and the
        archived release file to prove it -- and the source carries 0.1.0 too.
        Joining would give one version two releases.
        """
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        with pytest.raises(AbsorbError) as exc:
            absorb(ns, source, releasable_name="core", dry_run=True)
        assert "version collision" in str(exc.value)
        assert "already released 0.1.0" in str(exc.value)

    def test_a_broken_source_target_declaration(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        (source / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )
        run_git(source, "add", ".rlsbl")
        run_git(source, "commit", "-q", "-m", "chore: break the declaration")

        with pytest.raises(AbsorbError, match="broken target declaration"):
            absorb(ns, source, dry_run=True)

    def test_mixed_tag_schemes_name_the_tag_format_flag(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path, targets=("npm", "go"))
        (source / "go.mod").write_text("module example.com/widget\n\ngo 1.22\n")
        run_git(source, "add", "go.mod")
        run_git(source, "commit", "-q", "-m", "chore: go.mod")

        with pytest.raises(AbsorbError) as exc:
            absorb(ns, source, dry_run=True)
        message = str(exc.value)
        assert "incompatible monorepo tag schemes" in message
        assert "--tag-format" in message

    def test_tag_format_resolves_the_mixed_scheme_refusal(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path, targets=("npm", "go"))
        (source / "go.mod").write_text("module example.com/widget\n\ngo 1.22\n")
        run_git(source, "add", "go.mod")
        run_git(source, "commit", "-q", "-m", "chore: go.mod")

        preview = absorb(
            ns, source, tag_format="packages/widget/v{version}", dry_run=True,
        )
        facts = "\n".join(preview.by_key(ITEM_RELEASABLE).facts)
        assert "packages/widget/v{version}" in facts

    def test_a_source_with_no_readable_version(self, tmp_path):
        ns = make_destination(tmp_path)
        repo = tmp_path / "bare_src"
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")
        _commit(repo, {"package.json": json.dumps({"name": "bare"}) + "\n"}, "init")

        with pytest.raises(AbsorbError, match="cannot determine the version"):
            absorb(ns, repo, dest="packages/bare", dry_run=True)

    def test_a_missing_saferm_without_the_flag(self, tmp_path, monkeypatch):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        real_which = shutil.which
        monkeypatch.setattr(
            shutil, "which",
            lambda n, *a, **k: None if n == "saferm" else real_which(n, *a, **k),
        )

        with pytest.raises(AbsorbError, match="saferm is not installed"):
            absorb(ns, source, dry_run=True)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class TestApply:
    def test_the_history_arrives_under_the_destination_prefix(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        assert (ns.root / "packages" / "widget" / "src" / "index.js").is_file()
        log = gitout(ns.root, "log", "--oneline", "--", "packages/widget")
        assert "the first release" in log
        assert "wip" in log

    def test_the_merge_carries_the_absorb_trailers(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        body = gitout(
            ns.root, "log", "--format=%B", "-n", "20",
        )
        assert f"{ABSORB_TRAILER}: widget packages/widget" in body
        assert "Autogenerated: true" in body

    def test_tags_are_created_at_the_rewritten_commits(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        tags = _tags(ns.root)
        assert "widget@v0.1.0" in tags
        assert "v0.1.0" in tags  # the boundary alias
        assert gitout(ns.root, "rev-list", "-n", "1", "widget@v0.1.0") == (
            gitout(ns.root, "rev-list", "-n", "1", "v0.1.0")
        )
        # The tagged commit really is in this repository's history now.
        gitout(ns.root, "cat-file", "-e", "widget@v0.1.0^{commit}")

    def test_the_destinations_own_tag_survives_untouched(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        before = gitout(ns.root, "rev-list", "-n", "1", "core@v0.1.0")

        absorb(ns, source)

        assert "core@v0.1.0" in _tags(ns.root)
        assert gitout(ns.root, "rev-list", "-n", "1", "core@v0.1.0") == before

    def test_the_release_state_moves_into_the_releasable(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        state = get_releasable_dir(str(ns.root), "widget")
        assert os.path.isfile(os.path.join(state, "version"))
        assert open(os.path.join(state, "version")).read().strip() == "0.1.0"
        assert os.path.isfile(os.path.join(state, "config.json"))
        changes = os.path.join(state, "changes")
        assert os.path.isfile(os.path.join(changes, "unreleased.jsonl"))
        assert os.path.isfile(os.path.join(changes, "0.1.0.jsonl"))
        assert os.path.isfile(os.path.join(changes, "0.1.0.md"))
        assert os.path.isfile(os.path.join(state, "releases", "v0.1.0.toml"))
        entries = parse_jsonl(os.path.join(changes, "unreleased.jsonl"))
        assert [e.description for e in entries] == ["Work in progress"]

    def test_a_migrated_released_changelog_is_locked(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        state = get_releasable_dir(str(ns.root), "widget")
        released = os.path.join(state, "changes", "0.1.0.jsonl")
        archive = os.path.join(state, "releases", "v0.1.0.toml")
        assert oct(os.stat(released).st_mode & 0o777) == "0o444"
        assert oct(os.stat(archive).st_mode & 0o777) == "0o444"
        # The generated markdown is a derivative, never locked.
        md = os.path.join(state, "changes", "0.1.0.md")
        assert os.stat(md).st_mode & 0o200

    def test_the_per_package_residue_is_removed(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        member = ns.root / "packages" / "widget" / ".rlsbl"
        assert not (member / "changes").exists()
        assert not (member / "releases").exists()

    def test_changelog_hashes_resolve_in_the_workspace(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        changes = os.path.join(
            get_releasable_dir(str(ns.root), "widget"), "changes",
        )
        hashes = []
        for name in sorted(os.listdir(changes)):
            if name.endswith(".jsonl"):
                for entry in parse_jsonl(os.path.join(changes, name)):
                    hashes.extend(entry.commits)
        assert hashes
        for h in hashes:
            gitout(ns.root, "cat-file", "-e", h + "^{commit}")

    def test_the_release_anchor_is_remapped_and_resolves(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        before = read_release_file(
            str(source / ".rlsbl" / "releases" / "v0.1.0.toml")
        )

        absorb(ns, source)

        archive = os.path.join(
            get_releasable_dir(str(ns.root), "widget"), "releases", "v0.1.0.toml",
        )
        after = read_release_file(archive)
        assert after.candidate_sha != before.candidate_sha
        gitout(ns.root, "cat-file", "-e", after.candidate_sha + "^{commit}")
        # The anchor is rekeyed to the member path, and the tree it records is
        # the one the rewritten commit really has there.
        assert list(after.tree_hashes) == ["packages/widget"]
        assert after.tree_hashes["packages/widget"] == gitout(
            ns.root, "rev-parse", f"{after.candidate_sha}:packages/widget",
        )
        # Content-identical to what the source recorded: the rewrite moved the
        # path, it did not change the content.
        assert after.tree_hashes["packages/widget"] == before.tree_hashes["."]

    def test_the_workspace_declares_the_member_and_its_releasable(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source, registry_name="widget-npm")

        projects = load_workspace(str(ns.root))
        member = next(p for p in projects if p.name == "widget")
        assert member.path == "packages/widget"
        assert member.releasable == "widget"
        assert member.registry_name == "widget-npm"
        releasables = {r.name: r for r in load_releasables(str(ns.root), projects)}
        assert set(releasables) == {"core", "widget"}
        # The created releasable's tag format is WRITTEN, never inherited.
        assert releasables["widget"].declares_tag_format
        assert releasables["widget"].tag_format == "{name}@v{version}"

    def test_the_workspace_is_committed_and_clean(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        assert gitout(ns.root, "--no-optional-locks", "status", "--porcelain") == ""

    def test_the_snapshot_is_regenerated(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        snapshot = json.loads(
            (ns.root / ".rlsbl-monorepo" / "snapshot.json").read_text()
        )
        assert "widget" in json.dumps(snapshot)

    def test_the_source_repository_is_untouched(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        head = gitout(source, "rev-parse", "HEAD")
        tags = _tags(source)

        absorb(ns, source)

        assert gitout(source, "rev-parse", "HEAD") == head
        assert _tags(source) == tags
        assert (source / ".rlsbl" / "changes" / "unreleased.jsonl").is_file()

    def test_the_working_clone_is_removed(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        assert not (ns.root / ".git" / "rlsbl" / "absorb-widget").exists()

    def test_lineage_explains_the_conversion(self, tmp_path):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)

        absorb(ns, source)

        path = get_lineage_path(
            str(ns.root),
            releasable_dir=get_releasable_dir(str(ns.root), "widget"),
        )
        events = read_events(path)
        kinds = [e.KIND for e in events]
        assert kinds[0] == KIND_CONVERSION
        assert set(kinds) == {
            KIND_CONVERSION, KIND_TAG_MAP, KIND_ANCHOR_REMAP, KIND_BOUNDARY_ALIAS,
        }
        conversion = events[0]
        assert conversion.direction == "absorb"
        assert conversion.destination.path == "packages/widget"
        assert conversion.destination.releasable == "widget"
        assert conversion.destination.tag_format == "{name}@v{version}"
        for event in events[1:]:
            assert event.related_to == conversion.id
        tag_map = next(e for e in events if e.KIND == KIND_TAG_MAP)
        assert [(m.old_tag, m.new_tag) for m in tag_map.mappings] == [
            ("v0.1.0", "widget@v0.1.0"),
        ]
        alias = next(e for e in events if e.KIND == KIND_BOUNDARY_ALIAS)
        # Keyed by FORMAT, the way an extract's record is: alias_tag is the
        # post-conversion spelling, aliased_tag the pre-conversion one.
        assert alias.aliases[0].alias_tag == "widget@v0.1.0"
        assert alias.aliases[0].aliased_tag == "v0.1.0"
        assert alias.aliases[0].commit == gitout(
            ns.root, "rev-list", "-n", "1", "v0.1.0",
        )

    def test_joining_an_existing_releasable_appends_to_its_changelog(
        self, tmp_path,
    ):
        ns = make_destination(tmp_path)
        # A source at a version core does not carry, so there is no collision.
        source = make_source(tmp_path, version="0.4.0")
        before = parse_jsonl(
            os.path.join(
                get_releasable_dir(str(ns.root), "core"), "changes",
                "unreleased.jsonl",
            )
        )

        absorb(ns, source, releasable_name="core")

        changes = os.path.join(
            get_releasable_dir(str(ns.root), "core"), "changes",
        )
        entries = parse_jsonl(os.path.join(changes, "unreleased.jsonl"))
        assert len(entries) == len(before) + 1
        assert "Work in progress" in [e.description for e in entries]
        # The existing releasable's version is its own business.
        version = open(
            os.path.join(get_releasable_dir(str(ns.root), "core"), "version")
        ).read().strip()
        assert version == ns.initial_version
        assert "core@v0.4.0" in _tags(ns.root)

    def test_a_sibling_registry_dependency_is_left_byte_identical(self, tmp_path):
        """Absorb never edits another member's manifest.

        The member that stays behind depends on the absorbed package THROUGH
        THE REGISTRY, which is a perfectly good dependency and none of this
        conversion's business: a repository that starts publishing a package
        does not make its consumers' declarations wrong.
        """
        ns = make_destination(tmp_path)
        manifest = ns.root / "pkgA" / "pyproject.toml"
        manifest.write_text(
            '[project]\nname = "pkgA"\nversion = "0.1.0"\n'
            'dependencies = ["widget>=0.1.0"]\n'
        )
        run_git(ns.root, "add", "pkgA/pyproject.toml")
        run_git(ns.root, "commit", "-q", "-m", "feat: depend on widget")
        before = manifest.read_bytes()
        source = make_source(tmp_path)

        absorb(ns, source)

        assert manifest.read_bytes() == before


# ---------------------------------------------------------------------------
# Idempotent re-run healing
# ---------------------------------------------------------------------------


class _KillSwitch(RuntimeError):
    """Stops an absorb at a chosen step, the way a crash would."""


def _kill_after(monkeypatch, item_key):
    """Make the apply pipeline die right after the step keyed ``item_key``."""
    from rlsbl.commands.monorepo import absorb_cmd

    real = absorb_cmd._APPLY_STEPS[item_key]

    def die(arr, item, run):
        real(arr, item, run)
        raise _KillSwitch(item_key)

    monkeypatch.setitem(absorb_cmd._APPLY_STEPS, item_key, die)


def _commit_absorb_leftovers(root, message):
    """Commit whatever a killed run left behind, as an operator would.

    A crash leaves the working tree as it was; the re-run refuses a dirty
    workspace (it merges), so the leftovers are committed first. That is the
    real recovery, not a test shortcut.
    """
    if gitout(root, "--no-optional-locks", "status", "--porcelain"):
        run_git(root, "add", "-A")
        run_git(root, "commit", "-q", "-m", message)


class TestHealing:
    """A run killed at any step is completed by re-running it."""

    def _rerun_and_verify(self, ns, source):
        absorb(ns, source)

        # One merge, one member, one set of tags, one copy of the entry.
        merges = gitout(
            ns.root, "log", "-F",
            f"--grep={ABSORB_TRAILER}: widget packages/widget", "--format=%H",
        ).split()
        assert len(merges) == 1, merges
        projects = load_workspace(str(ns.root))
        assert [p.name for p in projects].count("widget") == 1
        tags = _tags(ns.root)
        assert tags.count("widget@v0.1.0") == 1
        entries = parse_jsonl(os.path.join(
            get_releasable_dir(str(ns.root), "widget"), "changes",
            "unreleased.jsonl",
        ))
        assert [e.description for e in entries] == ["Work in progress"]
        assert gitout(ns.root, "--no-optional-locks", "status", "--porcelain") == ""

    def test_killed_after_the_merge(self, tmp_path, monkeypatch):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        _kill_after(monkeypatch, ITEM_HISTORY)
        with pytest.raises(_KillSwitch):
            absorb(ns, source)
        monkeypatch.undo()
        _commit_absorb_leftovers(ns.root, "wip: interrupted absorb")

        self._rerun_and_verify(ns, source)

    def test_killed_after_the_tags(self, tmp_path, monkeypatch):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        _kill_after(monkeypatch, ITEM_TAGS)
        with pytest.raises(_KillSwitch):
            absorb(ns, source)
        monkeypatch.undo()
        assert "widget@v0.1.0" in _tags(ns.root)
        _commit_absorb_leftovers(ns.root, "wip: interrupted absorb")

        self._rerun_and_verify(ns, source)

    def test_killed_after_the_workspace_entry(self, tmp_path, monkeypatch):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        _kill_after(monkeypatch, ITEM_WORKSPACE)
        with pytest.raises(_KillSwitch):
            absorb(ns, source)
        monkeypatch.undo()
        assert "widget" in [p.name for p in load_workspace(str(ns.root))]
        _commit_absorb_leftovers(ns.root, "wip: interrupted absorb")

        self._rerun_and_verify(ns, source)

    def test_the_plan_says_the_history_is_already_merged(
        self, tmp_path, monkeypatch,
    ):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        _kill_after(monkeypatch, ITEM_HISTORY)
        with pytest.raises(_KillSwitch):
            absorb(ns, source)
        monkeypatch.undo()
        _commit_absorb_leftovers(ns.root, "wip: interrupted absorb")

        preview = absorb(ns, source, dry_run=True)
        assert preview.by_key(ITEM_HISTORY).state == "history_already_merged"

    def test_a_different_source_at_the_same_path_is_refused(
        self, tmp_path, monkeypatch,
    ):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        _kill_after(monkeypatch, ITEM_HISTORY)
        with pytest.raises(_KillSwitch):
            absorb(ns, source)
        monkeypatch.undo()
        _commit_absorb_leftovers(ns.root, "wip: interrupted absorb")

        # A genuinely different repository: same destination, other content,
        # therefore another root commit.
        other = make_source(tmp_path / "second", name="gadget")
        with pytest.raises(AbsorbError, match="DIFFERENT repository"):
            absorb(ns, other, name="widget", dry_run=True)


# ---------------------------------------------------------------------------
# Anchor verification
# ---------------------------------------------------------------------------


class TestAnchorVerification:
    def test_a_recorded_tree_the_rewrite_disagrees_with_is_a_hard_error(
        self, tmp_path,
    ):
        ns = make_destination(tmp_path)
        source = make_source(tmp_path)
        archive = source / ".rlsbl" / "releases" / "v0.1.0.toml"
        config = read_release_file(str(archive))
        os.chmod(str(archive), 0o644)
        write_release_anchor(
            str(archive),
            candidate_sha=config.candidate_sha,
            tree_hashes={".": "0" * 40},
        )
        os.chmod(str(archive), 0o444)
        run_git(source, "add", ".rlsbl")
        run_git(source, "commit", "-q", "-m", "chore: a lying anchor")

        with pytest.raises(AbsorbError) as exc:
            absorb(ns, source)
        message = str(exc.value)
        assert "does not survive the rewrite" in message
        assert "content-addressed" in message
