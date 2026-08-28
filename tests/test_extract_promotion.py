"""Promotion: extracting a MIRRORED releasable adopts the mirror's history.

A mirror already holds the subtree's standalone history -- every commit that
touched the member has a synthetic counterpart there, produced by the
deterministic subtree split. Filtering the monorepo again would build a second
standalone history of the same code with commit ids no consumer resolves, so
``rlsbl monorepo extract`` switches engines when a mirror binding exists: the
destination starts from the mirror, the deletion is justified by tree-hash
equality, and the monorepo-to-mirror correspondence is persisted into the
extracted repository's own lineage record.

Nothing here needs git-filter-repo (a promotion filters nothing), and the
mirror remote is always a local bare repository.
"""

import json
import os
import subprocess

import pytest

from conftest import (
    DEFAULT_ROOT_MEMBER,
    make_releasable_monorepo,
    run_git,
)
from githarness import git as gitout

from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl
from rlsbl.commands.monorepo.extract import ExtractError
from rlsbl.commands.monorepo.extract_cmd import cmd_extract
from rlsbl.commands.monorepo.mirror_cmd import converge_branch
from rlsbl.lineage import (
    KIND_PROMOTION_SPLIT_MAP,
    get_lineage_path,
    read_events,
)
from rlsbl.mirror_publication import split_commit_for
from rlsbl.release_file import (
    list_archived_versions,
    read_release_file,
    write_release_anchor,
)
from rlsbl.workspace import (
    Releasable,
    get_releasable_dir,
    load_releasables,
    load_standalone_releasable,
    load_workspace,
)


def _bare(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare"], cwd=str(path), check=True)
    return str(path)


def _resolves(repo, rev):
    return subprocess.run(
        ["git", "rev-parse", "--verify", f"{rev}^{{commit}}"],
        cwd=str(repo), capture_output=True, text=True,
    ).returncode == 0


def make_promotable(tmp_path, *, converge=True):
    """A monorepo with one mirrored releasable, and its mirror converged.

    Returns ``(namespace, mirror_remote)``. The releasable ``pkg`` owns the one
    member ``pkg/``; ``other`` stays behind so the source keeps a releasable
    after the promotion.
    """
    mirror = _bare(tmp_path / "mirror.git")
    root = tmp_path / "mono"
    ns = make_releasable_monorepo(
        root,
        releasables=[
            Releasable(name="pkg", subtree_remote=mirror),
            Releasable(name="other"),
        ],
        projects=[
            dict(DEFAULT_ROOT_MEMBER),
            {"path": "pkg", "name": "pkg", "releasable": "pkg"},
            {"path": "other", "name": "other", "releasable": "other"},
        ],
    )

    # A second released-state commit inside the member, referenced by the
    # unreleased changelog and anchored by the archive, so the promotion has
    # real hashes to translate rather than an empty state directory.
    (root / "pkg" / "feature.py").write_text("value = 1\n")
    run_git(root, "add", "pkg/feature.py")
    run_git(root, "commit", "-q", "-m", "feat: pkg work")
    sha = gitout(root, "rev-parse", "HEAD")

    changes = os.path.join(get_releasable_dir(str(root), "pkg"), "changes")
    from conftest import write_jsonl

    write_jsonl(
        os.path.join(changes, "unreleased.jsonl"),
        [ChangelogEntry(
            commits=[sha], user_facing=True,
            description="Pkg work", type="feature",
        )],
    )
    # The released version's entry names a commit that touched the MEMBER --
    # which is what a real releasable's changelog carries, since `changelog
    # add` refuses a commit outside the member's territory.
    released = os.path.join(changes, "0.1.0.jsonl")
    os.chmod(released, 0o644)
    write_jsonl(
        released,
        [ChangelogEntry(
            commits=[sha], user_facing=True,
            description="Initial pkg release", type="feature",
        )],
    )
    os.chmod(released, 0o444)
    # Anchor the archived 0.1.0 at that commit, with the member's tree.
    archive = os.path.join(
        get_releasable_dir(str(root), "pkg"), "releases", "v0.1.0.toml",
    )
    os.chmod(archive, 0o644)
    write_release_anchor(
        archive,
        candidate_sha=sha,
        tree_hashes={"pkg": gitout(root, "rev-parse", "HEAD:pkg")},
    )
    os.chmod(archive, 0o444)
    run_git(root, "add", ".rlsbl-monorepo")
    run_git(root, "commit", "-q", "-m", "chore: pkg release state")

    if converge:
        converge_branch(
            mirror, str(root), "pkg",
            os.path.join(str(root), "pkg", ".rlsbl", "config.json"),
        )
    return ns, mirror


class TestPreview:
    def test_the_plan_says_the_mirror_is_promoted(self, tmp_path, capsys):
        ns, mirror = make_promotable(tmp_path, converge=False)
        preview = cmd_extract(
            str(ns.root), "pkg", str(tmp_path / "out"), dry_run=True,
        )
        assert preview.by_key("releasable").state == "promote_mirror"
        assert preview.by_key("trees").state == "verify_mirror_tree"
        out = capsys.readouterr().out
        assert mirror in out
        assert "promotion-split-map" in out
        # A promotion inherits the mirror as its origin: it must not be told
        # to create one.
        assert "create the remote repository" not in out

    def test_the_dry_run_writes_nothing(self, tmp_path):
        ns, _mirror = make_promotable(tmp_path, converge=False)
        target = tmp_path / "out"
        cmd_extract(str(ns.root), "pkg", str(target), dry_run=True)
        assert not target.exists()
        assert (ns.root / "pkg").is_dir()


class TestApply:
    def test_the_promoted_repo_is_the_mirrors_history(self, tmp_path):
        ns, mirror = make_promotable(tmp_path)
        target = tmp_path / "out"
        mirror_tip = subprocess.run(
            ["git", "ls-remote", mirror, "refs/heads/main"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]

        cmd_extract(str(ns.root), "pkg", str(target), delete_with_rm=True)

        # The destination's history IS the mirror's: its origin is the mirror,
        # and the mirror's tip is an ancestor of (or equal to) its HEAD.
        assert gitout(target, "remote", "get-url", "origin") == mirror
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", mirror_tip, "HEAD"],
            cwd=str(target), check=True,
        )

    def test_tree_equality_is_the_deletion_proof(self, tmp_path, capsys):
        ns, _mirror = make_promotable(tmp_path)
        target = tmp_path / "out"
        expected = gitout(ns.root, "rev-parse", "HEAD:pkg")

        cmd_extract(str(ns.root), "pkg", str(target), delete_with_rm=True)

        out = capsys.readouterr().out
        assert f"tree {expected[:12]} verified" in out

    def test_a_stale_mirror_stops_the_promotion(self, tmp_path):
        ns, _mirror = make_promotable(tmp_path)
        # Advance the member AFTER converging: the mirror now holds an older
        # tree than the one the source would lose.
        (ns.root / "pkg" / "later.py").write_text("later = 1\n")
        run_git(ns.root, "add", "pkg/later.py")
        run_git(ns.root, "commit", "-q", "-m", "feat: later")

        with pytest.raises(ExtractError) as exc:
            cmd_extract(
                str(ns.root), "pkg", str(tmp_path / "out"), delete_with_rm=True,
            )
        message = str(exc.value)
        assert "does not carry this releasable's current subtree" in message
        assert "rlsbl monorepo mirror pkg" in message
        # Nothing was taken out of the source.
        assert (ns.root / "pkg").is_dir()

    def test_the_split_map_is_persisted_into_the_lineage_record(self, tmp_path):
        ns, mirror = make_promotable(tmp_path)
        target = tmp_path / "out"
        cmd_extract(str(ns.root), "pkg", str(target), delete_with_rm=True)

        events = read_events(
            get_lineage_path(str(target)), kinds=[KIND_PROMOTION_SPLIT_MAP],
        )
        assert len(events) == 1
        event = events[0]
        assert event.subtree_path == "pkg"
        assert event.mirror_remote == mirror
        assert event.promoted_version == "0.1.0"
        assert event.mappings
        # Every recorded mapping really is the split of its source commit, and
        # the split really is a commit the promoted repository carries.
        for mapping in event.mappings:
            assert _resolves(target, mapping.split_sha)

    def test_every_changelog_hash_resolves_in_the_promoted_repo(self, tmp_path):
        ns, _mirror = make_promotable(tmp_path)
        target = tmp_path / "out"
        cmd_extract(str(ns.root), "pkg", str(target), delete_with_rm=True)

        changes = target / ".rlsbl" / "changes"
        seen = 0
        for name in sorted(os.listdir(changes)):
            if not name.endswith(".jsonl"):
                continue
            for entry in parse_jsonl(str(changes / name)):
                for sha in entry.commits:
                    seen += 1
                    assert _resolves(target, sha), (
                        f"{name}: {sha} does not resolve in the promoted repo"
                    )
        assert seen, "the fixture must carry changelog hashes to translate"

    def test_every_release_anchor_resolves_in_the_promoted_repo(self, tmp_path):
        ns, _mirror = make_promotable(tmp_path)
        target = tmp_path / "out"
        cmd_extract(str(ns.root), "pkg", str(target), delete_with_rm=True)

        releases = str(target / ".rlsbl" / "releases")
        versions = list_archived_versions(releases)
        assert versions, "the fixture must carry an archived release"
        for version in versions:
            archive = read_release_file(os.path.join(releases, f"v{version}.toml"))
            assert archive.candidate_sha
            assert _resolves(target, archive.candidate_sha), (
                f"v{version}: anchor {archive.candidate_sha} does not resolve"
            )

    def test_the_anchor_is_the_split_of_the_monorepo_anchor(self, tmp_path):
        ns, _mirror = make_promotable(tmp_path)
        source_anchor = read_release_file(os.path.join(
            get_releasable_dir(str(ns.root), "pkg"), "releases", "v0.1.0.toml",
        )).candidate_sha
        expected = split_commit_for(str(ns.root), "pkg", source_anchor)

        target = tmp_path / "out"
        cmd_extract(str(ns.root), "pkg", str(target), delete_with_rm=True)

        archive = read_release_file(
            str(target / ".rlsbl" / "releases" / "v0.1.0.toml")
        )
        assert archive.candidate_sha == expected

    def test_the_released_tag_stands_at_that_commit(self, tmp_path):
        ns, _mirror = make_promotable(tmp_path)
        source_anchor = read_release_file(os.path.join(
            get_releasable_dir(str(ns.root), "pkg"), "releases", "v0.1.0.toml",
        )).candidate_sha
        expected = split_commit_for(str(ns.root), "pkg", source_anchor)

        target = tmp_path / "out"
        cmd_extract(str(ns.root), "pkg", str(target), delete_with_rm=True)

        assert "v0.1.0" in gitout(target, "tag", "-l").split()
        assert gitout(target, "rev-list", "-n", "1", "v0.1.0") == expected

    def test_the_destination_reads_back_as_the_same_releasable(self, tmp_path):
        ns, _mirror = make_promotable(tmp_path)
        target = tmp_path / "out"
        cmd_extract(str(ns.root), "pkg", str(target), delete_with_rm=True)

        releasable = load_standalone_releasable(str(target))
        assert releasable is not None
        assert releasable.name == "pkg"

    def test_the_promoted_repo_keeps_the_members_own_config(self, tmp_path):
        """The mirror's config is the scaffold's DERIVED copy (publish_mode
        forced to "none"); the promoted repository carries the authored one."""
        ns, _mirror = make_promotable(tmp_path)
        target = tmp_path / "out"
        cmd_extract(str(ns.root), "pkg", str(target), delete_with_rm=True)

        config = json.loads((target / ".rlsbl" / "config.json").read_text())
        assert config["publish_mode"] == "ci"

    def test_the_source_loses_the_member_and_the_releasable(self, tmp_path):
        ns, _mirror = make_promotable(tmp_path)
        cmd_extract(
            str(ns.root), "pkg", str(tmp_path / "out"), delete_with_rm=True,
        )

        assert not (ns.root / "pkg").exists()
        names = {p.name for p in load_workspace(str(ns.root))}
        assert "pkg" not in names
        assert {r.name for r in load_releasables(str(ns.root))} == {"other"}
        # ...and with the releasable goes its mirror binding.
        text = (ns.root / ".rlsbl-monorepo" / "workspace.toml").read_text()
        assert "subtree_remote" not in text
