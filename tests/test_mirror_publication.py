"""Publishing a released version onto a subtree mirror.

Every remote here is a local bare repository reached over ``file://`` -- the
mirror push path is exercised for real, and nothing leaves the machine.
"""

import json
import subprocess

import pytest

from rlsbl import mirror_publication as mp
from rlsbl.mirror_publication import MirrorPublicationError


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _bare(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare"], cwd=str(path), check=True)
    return str(path)


def _monorepo(root, subtree="packages/lib"):
    """A monorepo with one sub-project and two commits touching it."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.local")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")

    member = root / subtree
    member.mkdir(parents=True, exist_ok=True)
    (member / "package.json").write_text(
        json.dumps({"name": "lib", "version": "0.1.0"}) + "\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    first = _git(root, "rev-parse", "HEAD")

    (member / "index.js").write_text("module.exports = 1;\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second")
    second = _git(root, "rev-parse", "HEAD")
    return first, second


class _FakeGh:
    """Records gh argv; answers `release view` from a body registry."""

    def __init__(self, bodies=None):
        self.calls = []
        self.bodies = dict(bodies or {})

    def __call__(self, args, config=None):
        self.calls.append(list(args))
        if args[:2] == ["release", "view"]:
            tag = args[2]
            if tag not in self.bodies:
                raise RuntimeError(f"release not found: {tag}")
            return self.bodies[tag]
        if args[:2] == ["release", "create"]:
            tag = args[2]
            path = args[args.index("--notes-file") + 1]
            with open(path, encoding="utf-8") as f:
                self.bodies[tag] = f.read()
            return ""
        if args[:2] == ["release", "edit"]:
            tag = args[2]
            path = args[args.index("--notes-file") + 1]
            with open(path, encoding="utf-8") as f:
                self.bodies[tag] = f.read()
            return ""
        return ""


class TestSplitCorrespondence:
    def test_split_of_an_ancestor_is_an_ancestor_of_the_split_of_head(self, tmp_path):
        root = tmp_path / "mono"
        first, second = _monorepo(root)

        split_first = mp.split_commit_for(str(root), "packages/lib", first)
        split_second = mp.split_commit_for(str(root), "packages/lib", second)

        assert split_first != split_second
        # Both are real commits in the monorepo's object store, and the earlier
        # one is an ancestor of the later one -- so a tag materialized at the
        # earlier split names a commit the converged mirror already carries.
        assert _git(root, "cat-file", "-t", split_first) == "commit"
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", split_first, split_second],
            cwd=str(root), check=True,
        )

    def test_the_split_is_deterministic(self, tmp_path):
        root = tmp_path / "mono"
        _first, second = _monorepo(root)
        once = mp.split_commit_for(str(root), "packages/lib", second)
        twice = mp.split_commit_for(str(root), "packages/lib", second)
        assert once == twice

    def test_the_split_tree_is_the_members_tree(self, tmp_path):
        root = tmp_path / "mono"
        _first, second = _monorepo(root)
        split = mp.split_commit_for(str(root), "packages/lib", second)
        assert _git(root, "rev-parse", f"{split}^{{tree}}") == _git(
            root, "rev-parse", "HEAD:packages/lib",
        )

    def test_an_unknown_commit_is_a_hard_error(self, tmp_path):
        root = tmp_path / "mono"
        _monorepo(root)
        with pytest.raises(MirrorPublicationError, match="mirror's commit"):
            mp.split_commit_for(str(root), "packages/lib", "0" * 40)

    def test_split_map_asks_git_per_commit(self, tmp_path):
        root = tmp_path / "mono"
        first, second = _monorepo(root)
        mapping = mp.split_map_for(str(root), "packages/lib", [first, second, first])
        assert set(mapping) == {first, second}
        assert mapping[first] == mp.split_commit_for(str(root), "packages/lib", first)


class TestRefParsing:
    def test_peeled_tags_win(self):
        refs = mp.parse_ls_remote(
            "aaa\trefs/tags/v1.0.0\n"
            "bbb\trefs/tags/v1.0.0^{}\n"
            "ccc\trefs/heads/main\n"
        )
        assert refs["refs/tags/v1.0.0"] == "bbb"
        assert refs["refs/heads/main"] == "ccc"
        assert mp.remote_tag_commits(refs) == {"v1.0.0": "bbb"}


class TestEnsureTag:
    def test_pushes_a_missing_tag(self, tmp_path):
        root = tmp_path / "mono"
        _first, second = _monorepo(root)
        remote = _bare(tmp_path / "mirror.git")
        split = mp.split_commit_for(str(root), "packages/lib", second)

        assert mp.ensure_tag(remote, split, "v1.0.0", str(root)) == "pushed"
        tags = mp.remote_tag_commits(mp.remote_refs(remote, str(root)))
        assert tags["v1.0.0"] == split

    def test_an_already_correct_tag_is_left_alone(self, tmp_path):
        root = tmp_path / "mono"
        _first, second = _monorepo(root)
        remote = _bare(tmp_path / "mirror.git")
        split = mp.split_commit_for(str(root), "packages/lib", second)
        mp.ensure_tag(remote, split, "v1.0.0", str(root))
        assert mp.ensure_tag(remote, split, "v1.0.0", str(root)) == "present"

    def test_a_tag_at_another_commit_is_refused(self, tmp_path):
        root = tmp_path / "mono"
        first, second = _monorepo(root)
        remote = _bare(tmp_path / "mirror.git")
        old = mp.split_commit_for(str(root), "packages/lib", first)
        new = mp.split_commit_for(str(root), "packages/lib", second)
        mp.ensure_tag(remote, old, "v1.0.0", str(root))

        with pytest.raises(MirrorPublicationError) as exc:
            mp.ensure_tag(remote, new, "v1.0.0", str(root))
        assert "never moved" in str(exc.value)
        # ...and the tag really was not moved.
        tags = mp.remote_tag_commits(mp.remote_refs(remote, str(root)))
        assert tags["v1.0.0"] == old


class TestPublishVersion:
    def test_pushes_one_tag_and_one_release_and_no_branch(self, tmp_path):
        root = tmp_path / "mono"
        _first, second = _monorepo(root)
        remote = _bare(tmp_path / "mirror.git")
        gh = _FakeGh()

        split, tag_outcome, release_outcome = mp.publish_version(
            remote=remote, root=str(root), subtree_path="packages/lib",
            version="1.0.0", tag="v1.0.0", anchor_sha=second,
            notes="### Features\n- a thing", gh=gh, directory=str(tmp_path),
        )

        assert tag_outcome == "pushed"
        assert release_outcome == "created"
        refs = mp.remote_refs(remote, str(root))
        assert refs["refs/tags/v1.0.0"] == split
        # The BRANCH is the reconciler's, not the publication's.
        assert "refs/heads/main" not in refs

        creates = [c for c in gh.calls if c[:2] == ["release", "create"]]
        assert len(creates) == 1
        assert "--repo" in creates[0]

    def test_the_marker_names_the_mirror_commit_not_the_monorepo_anchor(
        self, tmp_path,
    ):
        root = tmp_path / "mono"
        _first, second = _monorepo(root)
        remote = _bare(tmp_path / "mirror.git")
        gh = _FakeGh()

        split, _t, _r = mp.publish_version(
            remote=remote, root=str(root), subtree_path="packages/lib",
            version="1.0.0", tag="v1.0.0", anchor_sha=second,
            notes="notes", gh=gh, directory=str(tmp_path),
        )
        body = gh.bodies["v1.0.0"]
        assert f"<!-- rlsbl-ci-sha: {split} -->" in body
        assert second not in body

    def test_rerunning_is_idempotent(self, tmp_path):
        root = tmp_path / "mono"
        _first, second = _monorepo(root)
        remote = _bare(tmp_path / "mirror.git")
        gh = _FakeGh()

        common = dict(
            remote=remote, root=str(root), subtree_path="packages/lib",
            version="1.0.0", tag="v1.0.0", anchor_sha=second, notes="notes",
            gh=gh, directory=str(tmp_path),
        )
        mp.publish_version(**common)
        _split, tag_outcome, release_outcome = mp.publish_version(**common)
        assert tag_outcome == "present"
        assert release_outcome == "already-correct"
        assert len([c for c in gh.calls if c[:2] == ["release", "create"]]) == 1

    def test_an_existing_release_without_a_marker_is_reconciled(self, tmp_path):
        root = tmp_path / "mono"
        _first, second = _monorepo(root)
        remote = _bare(tmp_path / "mirror.git")
        gh = _FakeGh(bodies={"v1.0.0": "hand-written notes\n"})

        split, _t, release_outcome = mp.publish_version(
            remote=remote, root=str(root), subtree_path="packages/lib",
            version="1.0.0", tag="v1.0.0", anchor_sha=second,
            notes="notes", gh=gh, directory=str(tmp_path),
        )
        assert release_outcome == "reconciled"
        assert "hand-written notes" in gh.bodies["v1.0.0"]
        assert f"<!-- rlsbl-ci-sha: {split} -->" in gh.bodies["v1.0.0"]


class TestMirrorTag:
    def test_uses_the_targets_standalone_form(self):
        from rlsbl.targets import TARGETS

        assert mp.mirror_tag("1.2.3", target=TARGETS["npm"]) == "v1.2.3"
        # A target with its own standalone spelling keeps it on the mirror.
        assert mp.mirror_tag("1.2.3", target=TARGETS["spec"]) == "spec-v1.2.3"


class TestVersionNotes:
    def test_reads_the_generated_per_version_markdown(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "1.0.0.md").write_text("### Features\n- a thing\n")
        assert mp.version_notes(str(changes), "1.0.0") == "### Features\n- a thing"

    def test_a_version_with_no_markdown_has_no_notes(self, tmp_path):
        assert mp.version_notes(str(tmp_path), "9.9.9") == ""
