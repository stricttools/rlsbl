"""A fork's upstream: the declaration, ``rlsbl upstream adopt-tags``, and
changelog coverage that leaves upstream's history out.

Every test builds three local repositories: an upstream with its own tags, a
bare ``origin`` standing in for the fork's hosting, and the fork's work tree.
The declared upstream URL (``https://github.com/up/proj``) is redirected to
the local upstream with git's ``url.<base>.insteadOf``, so the code under test
runs the real ``git ls-remote`` / ``git fetch`` against the URL it derives.
"""

import json
import subprocess

import pytest

import rlsbl
from githarness import commit_file, git, harness_env, init_repo

from rlsbl.changelog.schema import ChangelogEntry
from rlsbl.changelog.validate import check_coverage, check_in_range
from rlsbl.errors import ReleaseRecordError
from rlsbl.release_record import unreleased_range
from rlsbl.upstream import (
    Upstream,
    UpstreamError,
    history_exclusions,
    load,
    missing_history_message,
)

UPSTREAM_URL = "https://github.com/up/proj"
DECL = Upstream(host="github.com", owner="up", repo="proj", branch="main")
KEPT = "refs/tags-of/github.com/up/proj/"
BRANCH_REF = "refs/upstream/github.com/up/proj/main"
DECLARATION = (
    'format_version = 1\nhost = "github.com"\nowner = "up"\n'
    'repo = "proj"\nbranch = "main"\n'
)


def _declare(fork):
    d = fork / ".strictmetadata" / "upstream"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.toml").write_text('owner = "strictspec"\n')
    (d / "upstream.toml").write_text(DECLARATION)


def _refs(repo, prefix):
    out = git(repo, "for-each-ref", "--format=%(objectname) %(refname)", prefix)
    return dict(reversed(line.split(" ", 1)) for line in out.splitlines())


def _shell(cmd, cwd):
    """Run a printed fix command as written (it quotes a refspec glob)."""
    return subprocess.run(
        cmd, shell=True, cwd=str(cwd), env=harness_env(),
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def fork(tmp_path, monkeypatch):
    """An upstream with three tags (one annotated), a fork carrying them plus
    one tag of its own and one own commit, and a bare origin holding all of
    it. Returns the fork's work tree; cwd is the fork."""
    upstream = tmp_path / "upstream"
    init_repo(upstream)
    commit_file(upstream, "a.txt", "a\n", "upstream: a")
    git(upstream, "tag", "v0.1.0")
    commit_file(upstream, "b.txt", "b\n", "upstream: b")
    git(upstream, "tag", "-a", "v0.2.0", "-m", "upstream release 0.2.0")
    commit_file(upstream, "c.txt", "c\n", "upstream: c")
    git(upstream, "tag", "v0.3.0")

    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(upstream), str(origin))

    work = tmp_path / "fork"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "config", "user.email", "test@test.local")
    git(work, "config", "user.name", "Test")
    git(work, "config", f"url.file://{upstream}.insteadOf", UPSTREAM_URL)
    commit_file(work, "ours.txt", "ours\n", "fork: our own change")
    git(work, "tag", "nightly")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/main",
        "refs/tags/nightly")
    _declare(work)
    monkeypatch.chdir(work)
    return work


def _origin(fork):
    return fork.parent / "origin.git"


def _adopt(*argv):
    return rlsbl.app.test(["upstream", "adopt-tags", *argv])


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


class TestDeclaration:
    def test_a_repository_without_the_file_is_not_a_fork(self, tmp_path):
        init_repo(tmp_path)
        assert load(tmp_path) is None

    def test_the_declaration_names_the_derived_refs(self, fork):
        up = load(fork)
        assert up == DECL
        assert up.url == UPSTREAM_URL
        assert up.kept_ref("v0.1.0") == KEPT + "v0.1.0"
        assert up.branch_ref == BRANCH_REF

    def test_an_invalid_declaration_is_refused_naming_its_diagnostics(self, fork):
        path = fork / ".strictmetadata" / "upstream" / "upstream.toml"
        path.write_text('format_version = 1\nhost = "github.com"\n')
        with pytest.raises(UpstreamError) as exc:
            load(fork)
        assert "STRICTSPEC_" in str(exc.value)
        assert "owner" in str(exc.value)
        # The shape the error prints, filled in, is accepted.
        path.write_text(DECLARATION)
        assert load(fork) == DECL


# ---------------------------------------------------------------------------
# rlsbl upstream adopt-tags
# ---------------------------------------------------------------------------


class TestAdoptTags:
    def test_the_command_is_mutating_and_consequential(self):
        """Only a person may decide that tags this repository carries are
        upstream's releases rather than its own: the command deletes them
        from refs/tags here and on origin."""
        command = rlsbl.app._groups["upstream"].commands["adopt-tags"]
        assert command.effect == "mutating"
        assert command.consequential is True

    def test_dry_run_prints_the_plan_and_writes_nothing(self, fork):
        before_local = _refs(fork, "refs/")
        before_origin = _refs(_origin(fork), "refs/")
        result = rlsbl.app.test(["--dry-run", "upstream", "adopt-tags"])
        assert result.exit_code == 0, result.stderr
        assert "v0.1.0: inherited at" in result.stdout
        assert f"write {KEPT}v0.1.0" in result.stdout
        assert "1 tag(s) upstream does not have stay in refs/tags: nightly" in result.stdout
        assert "Dry run: 3 inherited tag(s) would move" in result.stdout
        assert _refs(fork, "refs/") == before_local
        assert _refs(_origin(fork), "refs/") == before_origin

    def test_inherited_tags_move_keeping_their_objects(self, fork):
        before = _refs(fork, "refs/tags/")
        result = _adopt("--approve-consequential")
        assert result.exit_code == 0, result.stderr

        local_tags = _refs(fork, "refs/tags/")
        assert set(local_tags) == {"refs/tags/nightly"}
        kept = _refs(fork, KEPT)
        for tag in ("v0.1.0", "v0.2.0", "v0.3.0"):
            # The object is kept -- for the annotated v0.2.0 that is the tag
            # object, not the commit it points at.
            assert kept[KEPT + tag] == before[f"refs/tags/{tag}"]
        assert git(fork, "cat-file", "-t", kept[KEPT + "v0.2.0"]) == "tag"

        origin = _origin(fork)
        assert set(_refs(origin, "refs/tags/")) == {"refs/tags/nightly"}
        assert _refs(origin, KEPT) == kept

    def test_a_second_run_has_nothing_to_do(self, fork):
        assert _adopt("--approve-consequential").exit_code == 0
        local, origin = _refs(fork, "refs/"), _refs(_origin(fork), "refs/")
        result = _adopt("--approve-consequential")
        assert result.exit_code == 0, result.stderr
        assert "v0.1.0: adopted: already in" in result.stdout
        assert "Nothing to do" in result.stdout
        assert _refs(fork, "refs/") == local
        assert _refs(_origin(fork), "refs/") == origin

    def test_a_re_run_finishes_an_interrupted_one(self, fork):
        """A crash after the kept refs were written but before the push."""
        for tag in ("v0.1.0", "v0.2.0", "v0.3.0"):
            oid = git(fork, "rev-parse", f"refs/tags/{tag}")
            git(fork, "update-ref", KEPT + tag, oid)
        result = _adopt("--approve-consequential")
        assert result.exit_code == 0, result.stderr
        assert set(_refs(fork, "refs/tags/")) == {"refs/tags/nightly"}
        assert set(_refs(_origin(fork), KEPT)) == {
            KEPT + t for t in ("v0.1.0", "v0.2.0", "v0.3.0")
        }

    def test_tags_fetched_back_from_upstream_are_moved_out_again(self, fork):
        assert _adopt("--approve-consequential").exit_code == 0
        git(fork, "fetch", "-q", "--tags", UPSTREAM_URL)
        assert "refs/tags/v0.1.0" in _refs(fork, "refs/tags/")
        result = _adopt("--approve-consequential")
        assert result.exit_code == 0, result.stderr
        assert "3 tag(s) deleted from refs/tags here" in result.stdout
        assert set(_refs(fork, "refs/tags/")) == {"refs/tags/nightly"}

    def test_a_same_name_tag_at_another_object_is_refused_named(self, fork):
        git(fork, "tag", "-f", "v0.2.0", "HEAD")
        local, origin = _refs(fork, "refs/"), _refs(_origin(fork), "refs/")
        result = _adopt("--approve-consequential")
        assert result.exit_code == 1
        assert "v0.2.0: refs/tags/v0.2.0 here is at" in result.stderr
        assert "upstream's is at" in result.stderr
        # Nothing moved -- not even the tags that are inherited.
        assert _refs(fork, "refs/") == local
        assert _refs(_origin(fork), "refs/") == origin

    def test_a_same_name_tag_on_origin_at_another_object_is_refused(self, fork):
        own = git(fork, "rev-parse", "HEAD")
        git(fork, "push", "-q", "-f", "origin", f"{own}:refs/tags/v0.1.0")
        result = _adopt("--approve-consequential")
        assert result.exit_code == 1
        assert "v0.1.0: refs/tags/v0.1.0 on origin is at" in result.stderr
        assert "refs/tags/v0.1.0" in _refs(fork, "refs/tags/")

    def test_no_declaration_is_refused_and_writing_one_clears_it(self, fork):
        (fork / ".strictmetadata" / "upstream" / "upstream.toml").unlink()
        result = _adopt("--approve-consequential")
        assert result.exit_code == 1
        assert "declares no upstream" in result.stderr
        # The fix the error names: write the declaration it shows.
        _declare(fork)
        assert _adopt("--approve-consequential").exit_code == 0

    def test_no_origin_is_refused_and_adding_it_clears_it(self, fork):
        url = git(fork, "remote", "get-url", "origin")
        git(fork, "remote", "remove", "origin")
        result = _adopt("--approve-consequential")
        assert result.exit_code == 1
        assert "has no `origin` remote" in result.stderr
        assert "git remote add origin <url of this fork>" in result.stderr
        git(fork, "remote", "add", "origin", url)
        assert _adopt("--approve-consequential").exit_code == 0

    def test_a_tag_only_on_origin_whose_object_is_missing_names_its_fetch(
        self, fork, tmp_path, monkeypatch,
    ):
        # A clone that fetched no tags and holds only the fork's main: the
        # inherited tags exist on origin, their objects here only partly.
        sparse = tmp_path / "sparse"
        # A clone would copy the annotated tag's object along (include-tag);
        # a --no-tags fetch of main alone does not.
        init_repo(sparse)
        git(sparse, "remote", "add", "origin", str(_origin(fork)))
        git(sparse, "fetch", "-q", "--no-tags", "origin",
            "+refs/heads/main:refs/remotes/origin/main")
        git(sparse, "config", f"url.file://{tmp_path / 'upstream'}.insteadOf",
            UPSTREAM_URL)
        _declare(sparse)
        monkeypatch.chdir(sparse)
        result = _adopt("--approve-consequential")
        assert result.exit_code == 1, result.stdout
        assert "git fetch origin tag v0.2.0" in result.stderr
        _shell("git fetch origin tag v0.2.0", sparse)
        result = _adopt("--approve-consequential")
        assert result.exit_code == 0, result.stderr
        assert set(_refs(_origin(fork), "refs/tags/")) == {
            "refs/tags/nightly",
        }


# ---------------------------------------------------------------------------
# Changelog coverage in a fork
# ---------------------------------------------------------------------------


def _releases_dir(fork):
    return str(fork / ".rlsbl" / "releases")


def _fetch_upstream_branch(fork):
    for cmd in missing_history_message(DECL).splitlines()[-2:]:
        _shell(cmd.strip(), fork)


def _own_commits(fork):
    """Every commit that is the fork's own: the one the fixture made, plus
    the declaration commit."""
    commit_file(fork, ".strictmetadata/upstream/upstream.toml", DECLARATION,
                "declare the upstream")
    return git(fork, "log", "--format=%H", "-2").splitlines()


class TestCoverageInAFork:
    def test_upstream_history_needs_no_entries(self, fork):
        assert _adopt("--approve-consequential").exit_code == 0
        _fetch_upstream_branch(fork)
        own = _own_commits(fork)
        entries = [ChangelogEntry(commits=own, user_facing=False)]

        passed, details = check_coverage(entries, _releases_dir(fork))
        assert passed, details

    def test_without_a_declaration_upstream_history_is_uncovered(self, fork):
        assert _adopt("--approve-consequential").exit_code == 0
        own = _own_commits(fork)
        (fork / ".strictmetadata" / "upstream" / "upstream.toml").unlink()
        entries = [ChangelogEntry(commits=own, user_facing=False)]
        passed, details = check_coverage(entries, _releases_dir(fork))
        assert not passed
        assert len([d for d in details if "not covered" in d]) == 3

    def test_a_commit_reachable_only_from_a_kept_tag_is_left_out(self, fork):
        assert _adopt("--approve-consequential").exit_code == 0
        _fetch_upstream_branch(fork)
        own = _own_commits(fork)
        # Upstream's branch moves back, as if rewritten: its tip no longer
        # reaches c.txt's commit, which only the kept v0.3.0 still does.
        git(fork, "update-ref", BRANCH_REF, f"{BRANCH_REF}~1")
        entries = [ChangelogEntry(commits=own, user_facing=False)]
        passed, details = check_coverage(entries, _releases_dir(fork))
        assert passed, details

    def test_an_entry_for_an_upstream_commit_is_out_of_range(self, fork):
        assert _adopt("--approve-consequential").exit_code == 0
        _fetch_upstream_branch(fork)
        upstream_commit = git(fork, "rev-parse", BRANCH_REF)
        entries = [ChangelogEntry(commits=[upstream_commit], user_facing=False)]
        passed, details = check_in_range(entries, _releases_dir(fork))
        assert not passed
        assert any("not in unreleased range" in d for d in details)

    def test_missing_upstream_refs_are_refused_with_the_fetch_that_restores_them(
        self, fork,
    ):
        assert _adopt("--approve-consequential").exit_code == 0
        own = _own_commits(fork)
        entries = [ChangelogEntry(commits=own, user_facing=False)]
        # A fresh clone: neither the upstream branch ref nor the kept tags.
        for ref in _refs(fork, KEPT):
            git(fork, "update-ref", "-d", ref)
        with pytest.raises(UpstreamError) as exc:
            check_coverage(entries, _releases_dir(fork))
        message = str(exc.value)
        assert BRANCH_REF in message
        fetches = [line.strip() for line in message.splitlines()[-2:]]
        assert fetches == [
            f"git fetch --no-tags {UPSTREAM_URL} +refs/heads/main:{BRANCH_REF}",
            f"git fetch --no-tags origin '{KEPT}*:{KEPT}*'",
        ]
        # The fix: run the printed commands as written.
        for cmd in fetches:
            _shell(cmd, fork)
        passed, details = check_coverage(entries, _releases_dir(fork))
        assert passed, details
        assert set(_refs(fork, KEPT)) == {
            KEPT + t for t in ("v0.1.0", "v0.2.0", "v0.3.0")
        }
        # The fetches brought no inherited tag back into refs/tags.
        assert set(_refs(fork, "refs/tags/")) == {"refs/tags/nightly"}

    def test_exclusions_are_empty_outside_a_fork(self, tmp_path):
        init_repo(tmp_path)
        commit_file(tmp_path, "a.txt", "a\n", "a")
        assert history_exclusions(tmp_path) == []

    def test_the_check_command_reports_the_refusal(self, fork):
        assert _adopt("--approve-consequential").exit_code == 0
        (fork / ".rlsbl" / "changes").mkdir(parents=True)
        (fork / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "none"}))
        (fork / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        result = rlsbl.app.test(["check", "--name", "changelog-coverage"])
        assert result.exit_code != 0
        assert "git fetch --no-tags" in result.stdout + result.stderr


class TestPushedCommitsInAFork:
    def test_upstream_commits_in_a_push_need_no_entries(self, fork):
        from rlsbl.prepush_utils import _get_pushed_commits

        assert _adopt("--approve-consequential").exit_code == 0
        _fetch_upstream_branch(fork)
        own = _own_commits(fork)
        # A new branch pushed to a remote that holds none of this history:
        # --remotes excludes nothing, so only the upstream exclusion keeps
        # upstream's commits out of the pushed set.
        git(_origin(fork), "update-ref", "-d", "refs/heads/main")
        git(fork, "fetch", "-q", "--prune", "origin")
        pushed = _get_pushed_commits([(git(fork, "rev-parse", "HEAD"), "0" * 40)])
        assert pushed == set(own)


class TestEmptyReleaseRecordInAFork:
    def test_the_refusal_names_adopt_tags_and_running_it_clears_it(self, fork):
        with pytest.raises(ReleaseRecordError) as exc:
            unreleased_range(_releases_dir(fork))
        message = str(exc.value)
        assert "fork of https://github.com/up/proj" in message
        assert "rlsbl upstream adopt-tags --dry-run" in message
        assert "rlsbl upstream adopt-tags --approve-consequential" in message

        # The fix, as printed: preview, then apply.
        dry = rlsbl.app.test(["--dry-run", "upstream", "adopt-tags"])
        assert dry.exit_code == 0, dry.stderr
        assert _adopt("--approve-consequential").exit_code == 0
        # No version tag is left, so the range is the whole (own) history.
        assert unreleased_range(_releases_dir(fork)) == "HEAD"


class TestReadCommandsInAFork:
    def _project(self, fork):
        (fork / ".rlsbl" / "changes").mkdir(parents=True)
        (fork / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "none"}))
        (fork / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")

    def test_unreleased_lists_only_the_forks_own_commits(self, fork):
        assert _adopt("--approve-consequential").exit_code == 0
        _fetch_upstream_branch(fork)
        _own_commits(fork)
        self._project(fork)
        result = rlsbl.app.test(["unreleased"])
        assert result.exit_code == 0, result.stderr
        assert "fork: our own change" in result.stdout
        assert "declare the upstream" in result.stdout
        assert "upstream: a" not in result.stdout
        assert "upstream: c" not in result.stdout
