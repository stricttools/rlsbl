"""Real-git tests for `rlsbl release undo` (latest-release path).

These exercise the plan-driven undo against real git repositories with a bare
remote. Only two boundaries are mocked: ``run_gh`` (the GitHub CLI) and
``run_evidence_gate`` (the registry network probe). Everything else -- tag
deletion, multi-commit reverts, working-tree state -- runs against real git.

Regression coverage for the three production bugs the rework fixed:
  (a) full unwind of a 5-commit release restores the previous version;
  (b) `undo --dry-run` leaves tags/commits/tree byte-identical;
  (c) a published release is refused with yank/deprecate routing, and an
      inconclusive-evidence release is hard-blocked;
  (d) undoing when the latest tag is a published release refuses (the
      "second undo walks to the previous published release" scenario).
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from githarness import add_remote, git, init_repo, remote_ref, snapshot_remote_refs
from rlsbl.commands.undo import run_cmd
from rlsbl.context import ProjectContext
from rlsbl.evidence_gate import Evidence, EvidenceKind, GateResult, Verdict
from rlsbl.release_file import write_release_anchor

from conftest import archive_release, ledger_dir

_ENTRY = {
    "commits": [],
    "user_facing": True,
    "description": "**New feature.** A shiny new thing.",
    "type": "feature",
}


# --------------------------------------------------------------------------- #
# Evidence-gate stand-ins (the registry network boundary)
# --------------------------------------------------------------------------- #

def _cleared_gate(*_a, **_k):
    return GateResult(
        Verdict.CLEARED,
        [Evidence("registry_probe", "npm", EvidenceKind.UNPUBLISHED, "not on npm")],
        "at least one registry confirms unpublished, none confirms published",
    )


def _published_gate(*_a, **_k):
    return GateResult(
        Verdict.BLOCKED,
        [Evidence("registry_probe", "npm", EvidenceKind.PUBLISHED, "pkg@1.0.1 found on npm")],
        "published on: npm",
    )


def _inconclusive_gate(*_a, **_k):
    return GateResult(
        Verdict.BLOCKED,
        [Evidence("registry_probe", "npm", EvidenceKind.INCONCLUSIVE, "no authoritative probe")],
        "no authoritative evidence -- cannot determine publication status",
    )


class _FakeGh:
    """Stand-in for run_gh: records calls, controls whether a Release exists."""

    def __init__(self, release_exists=True):
        self.release_exists = release_exists
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:2] == ["release", "view"] and not self.release_exists:
            raise subprocess.CalledProcessError(1, "gh release view")
        return ""

    @property
    def deleted(self):
        return any(a[:2] == ["release", "delete"] for a in self.calls)


# --------------------------------------------------------------------------- #
# Fixture builder: a standalone repo in a completed-release state
# --------------------------------------------------------------------------- #

def _write(repo, rel, content):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _make_released_repo(repo, *, n_commits=5, with_remote=True):
    """Build ``repo`` in the state a completed 1.0.1 release leaves behind.

    The pre-release boundary is an ``initial`` commit at version 1.0.0 (tagged
    v1.0.0). On top of it sit the release commits, in history order:

      1. version-bump            "v1.0.1"                    (always)
      2. finalize-changelog      "chore: finalize changelog for 1.0.1"
      3. clean-stale-exclusions  "chore: clean 1 stale ..."  (n_commits >= 4)
      4. finalize-release-file   "chore: finalize release file for 1.0.1"
      5. regenerate-md           "chore: regenerate 1.0.1.md ..." (n_commits == 5)

    The tag v1.0.1 points at the last release commit. Returns a dict of shas.
    """
    init_repo(repo)

    _write(repo, "package.json", json.dumps({"name": "pkg", "version": "1.0.0"}, indent=2) + "\n")
    _write(repo, ".rlsbl/config.json", json.dumps({
        "publish_mode": "ci", "targets": ["npm"],
        "batch_limits": {"exclusions": ["stale"]},
    }, indent=2) + "\n")
    _write(repo, ".rlsbl/changes/unreleased.jsonl", json.dumps(_ENTRY) + "\n")
    _write(
        repo, ".rlsbl/releases/unreleased.toml",
        'format_version = 1\nbump = "patch"\ninclude = ["npm"]\nexclude = []\n'
        'description = "x"\n',
    )
    _write(repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n### Features\n\n- A shiny new thing.\n")
    _write(repo, ".gitignore", ".rlsbl/releases/in-progress.json\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "tag", "v1.0.0")
    shas = {"initial": git(repo, "rev-parse", "HEAD")}
    # 1.0.0's LEDGER entry -- what `undo` reads to find the release BEFORE the
    # one it is reverting.
    archive_release(
        ledger_dir(repo), "1.0.0", shas["initial"],
        tree=git(repo, "rev-parse", "HEAD^{tree}"),
    )

    # 1. version-bump
    pkg = json.loads((repo / "package.json").read_text())
    pkg["version"] = "1.0.1"
    (repo / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\n### Features\n\n- A shiny new thing.\n\n## 1.0.0\n\n- none\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v1.0.1")
    shas["version_bump"] = git(repo, "rev-parse", "HEAD")

    # 2. finalize-changelog (rename unreleased.jsonl -> 1.0.1.jsonl, create md)
    os.rename(repo / ".rlsbl/changes/unreleased.jsonl", repo / ".rlsbl/changes/1.0.1.jsonl")
    (repo / ".rlsbl/changes/unreleased.jsonl").write_text("")
    (repo / ".rlsbl/changes/1.0.1.md").write_text("### Features\n\n- A shiny new thing.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: finalize changelog for 1.0.1")
    shas["finalize_changelog"] = git(repo, "rev-parse", "HEAD")

    # 3. clean stale batch exclusions
    if n_commits >= 4:
        (repo / ".rlsbl/config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["npm"]}, indent=2) + "\n"
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "chore: clean 1 stale batch exclusion(s) from config.json")
        shas["clean_stale"] = git(repo, "rev-parse", "HEAD")

    # 4. finalize-release-file (rename unreleased.toml -> v1.0.1.toml, then
    #    write the anchor into it, exactly as the release flow does)
    os.rename(repo / ".rlsbl/releases/unreleased.toml", repo / ".rlsbl/releases/v1.0.1.toml")
    write_release_anchor(
        str(repo / ".rlsbl/releases/v1.0.1.toml"),
        candidate_sha=shas["version_bump"],
        tree_hashes={".": git(repo, "rev-parse", f'{shas["version_bump"]}^{{tree}}')},
    )
    os.chmod(repo / ".rlsbl/releases/v1.0.1.toml", 0o444)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: finalize release file for 1.0.1")
    shas["finalize_release_file"] = git(repo, "rev-parse", "HEAD")

    # 5. regenerate md
    if n_commits >= 5:
        (repo / ".rlsbl/changes/1.0.1.md").write_text("### Features\n\n- A shiny new thing (regenerated).\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "chore: regenerate 1.0.1.md from archived release metadata")
        shas["regenerate_md"] = git(repo, "rev-parse", "HEAD")

    git(repo, "tag", "v1.0.1")
    if with_remote:
        add_remote(repo, repo.parent / "remote.git")
    return shas


def _ctx(repo):
    return ProjectContext(
        project_root=Path(str(repo)), workspace_root=None,
        config={"publish_mode": "ci"},
    )


def _patches(gh, gate):
    return [
        patch("rlsbl.commands.undo.check_gh_installed", return_value=True),
        patch("rlsbl.commands.undo.check_gh_auth", return_value=True),
        patch("rlsbl.commands.undo.run_gh", side_effect=gh),
        patch("rlsbl.commands.undo.run_evidence_gate", side_effect=gate),
        patch("rlsbl.commands.undo.push_if_needed"),
    ]


def _run_undo(repo, flags, *, gh=None, gate=_cleared_gate):
    gh = gh or _FakeGh()
    ps = _patches(gh, gate)
    for p in ps:
        p.start()
    try:
        run_cmd("npm", [], flags, ctx=_ctx(repo))
    finally:
        for p in reversed(ps):
            p.stop()
    return gh


# --------------------------------------------------------------------------- #
# (a) Full unwind of a multi-commit release
# --------------------------------------------------------------------------- #

class TestFullUnwind:

    def test_five_commit_release_restores_previous_version(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=5)
        monkeypatch.chdir(repo)

        gh = _run_undo(repo, {})

        # Version file walked all the way back to the pre-release version.
        pkg = json.loads((repo / "package.json").read_text())
        assert pkg["version"] == "1.0.0", "all 5 release commits must be reverted"

        # Finalize renames fully undone by the reverts.
        assert not (repo / ".rlsbl/changes/1.0.1.jsonl").exists()
        assert (repo / ".rlsbl/changes/unreleased.jsonl").read_text().strip(), \
            "unreleased.jsonl entries restored"
        assert not (repo / ".rlsbl/releases/v1.0.1.toml").exists()
        assert (repo / ".rlsbl/releases/unreleased.toml").exists()

        # Tag deleted locally and on the remote; GitHub Release deleted.
        assert "v1.0.1" not in git(repo, "tag", "-l").split()
        assert remote_ref(repo, "refs/tags/v1.0.1") == ""
        assert gh.deleted

        # An audit record was written before the deletions.
        assert (repo / ".rlsbl" / "undo-audit.json").exists()

    def test_three_commit_release_unwinds(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=3)
        monkeypatch.chdir(repo)

        _run_undo(repo, {})

        pkg = json.loads((repo / "package.json").read_text())
        assert pkg["version"] == "1.0.0"
        assert not (repo / ".rlsbl/changes/1.0.1.jsonl").exists()
        assert not (repo / ".rlsbl/releases/v1.0.1.toml").exists()

    def test_reverts_survive_post_release_hook_commit_after_tag(self, tmp_path, monkeypatch):
        """A post-release hook commit sitting AFTER the tag must not stop the
        walk from targeting the release commits (which are below the tag)."""
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=5)
        # Post-release hook adds a commit on top of the tag.
        _write(repo, "DEPLOYED.txt", "deployed\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "chore: post-release deploy notes")
        monkeypatch.chdir(repo)

        _run_undo(repo, {})

        pkg = json.loads((repo / "package.json").read_text())
        assert pkg["version"] == "1.0.0", "release commits reverted despite hook commit after tag"
        # The unrelated hook commit's file is untouched.
        assert (repo / "DEPLOYED.txt").exists()


# --------------------------------------------------------------------------- #
# (b) Dry-run touches nothing
# --------------------------------------------------------------------------- #

class TestDryRun:

    def test_dry_run_yes_leaves_everything_byte_identical(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=5)
        monkeypatch.chdir(repo)

        head_before = git(repo, "rev-parse", "HEAD")
        tags_before = git(repo, "tag", "-l")
        remote_before = snapshot_remote_refs(repo)
        tree_before = git(repo, "rev-parse", "HEAD^{tree}")

        gh = _run_undo(repo, {"dry-run": True})

        assert git(repo, "rev-parse", "HEAD") == head_before, "dry-run created no commit"
        assert git(repo, "tag", "-l") == tags_before, "dry-run deleted no tag"
        assert snapshot_remote_refs(repo) == remote_before, "dry-run touched no remote ref"
        assert git(repo, "rev-parse", "HEAD^{tree}") == tree_before
        assert git(repo, "status", "--porcelain") == "", "dry-run left a clean tree"
        assert not gh.deleted, "dry-run did not delete the GitHub Release"
        assert not (repo / ".rlsbl" / "undo-audit.json").exists()


# --------------------------------------------------------------------------- #
# (b2) Non-interactive stdin at the two undo prompts
# --------------------------------------------------------------------------- #

class TestNoOwnPrompts:
    """`release undo` asks nothing of its own any more.

    It declares itself `consequential`, so strictcli confirms the whole
    operation once before dispatch (and refuses on a non-interactive stdin
    with the `--approve-consequential` remediation).  The command used to ask twice on top of that: once
    "This is destructive. Proceed?" before starting, and once "Push changes to
    remote?" halfway through the rollback -- the second of which could leave
    the remote holding the release that had just been undone locally.
    """

    def test_rollback_runs_without_prompting_and_pushes(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=3)
        monkeypatch.chdir(repo)

        gh = _FakeGh()
        with patch("rlsbl.commands.undo.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.undo.check_gh_auth", return_value=True), \
             patch("rlsbl.commands.undo.run_gh", side_effect=gh), \
             patch("rlsbl.commands.undo.run_evidence_gate", side_effect=_cleared_gate), \
             patch("rlsbl.commands.undo.push_if_needed") as mock_push, \
             patch("builtins.input", side_effect=AssertionError("must not prompt")):
            run_cmd("npm", [], {}, ctx=_ctx(repo))

        mock_push.assert_called_once()
        pkg = json.loads((repo / "package.json").read_text())
        assert pkg["version"] == "1.0.0"


# --------------------------------------------------------------------------- #
# (c) + (d) Evidence gate on the latest path
# --------------------------------------------------------------------------- #

class TestEvidenceGate:

    def test_published_release_refused_with_routing(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=3)
        monkeypatch.chdir(repo)

        head_before = git(repo, "rev-parse", "HEAD")
        with pytest.raises(SystemExit) as exc:
            _run_undo(repo, {}, gate=_published_gate)
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "yank" in err and "deprecate" in err, "must route to yank/deprecate"
        # Nothing was touched.
        assert git(repo, "rev-parse", "HEAD") == head_before
        assert "v1.0.1" in git(repo, "tag", "-l").split()

    def test_inconclusive_evidence_hard_blocks(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=3)
        monkeypatch.chdir(repo)

        head_before = git(repo, "rev-parse", "HEAD")
        with pytest.raises(SystemExit) as exc:
            _run_undo(repo, {}, gate=_inconclusive_gate)
        assert exc.value.code == 1
        assert git(repo, "rev-parse", "HEAD") == head_before
        assert "v1.0.1" in git(repo, "tag", "-l").split()

        err = capsys.readouterr().err
        assert "verify publication status manually" in err.lower()
        assert "release yank" in err
        assert "release deprecate" in err

    def test_second_undo_walks_to_published_release_is_refused(self, tmp_path, monkeypatch, capsys):
        """After a first undo removed the latest release, a second undo lands on
        the previous (published) release; the gate must protect it. Modeled here
        as: the latest tag points at a published release -> refuse."""
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=3)
        monkeypatch.chdir(repo)

        # git describe resolves the latest tag (v1.0.1); the gate says PUBLISHED.
        with pytest.raises(SystemExit) as exc:
            _run_undo(repo, {}, gate=_published_gate)
        assert exc.value.code == 1
        # The published release's tag survives untouched.
        assert "v1.0.1" in git(repo, "tag", "-l").split()
        assert remote_ref(repo, "refs/tags/v1.0.1") != ""
        assert "yank" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# (e) The audit record is a precondition, not a best-effort step
# --------------------------------------------------------------------------- #

class TestAuditFailureRefusesTheUndo:
    """An audit record that cannot be written stops the undo dead.

    The execution step promises the journal is written BEFORE any destructive
    action on every path. A blanket ``except`` around the write turned that
    promise into a printed traceback and a FAILED row while the GitHub Release,
    the tags and the release commits were destroyed anyway -- leaving no record
    of what was destroyed, in exactly the run where the record is needed.
    """

    def _corrupt_audit(self, repo):
        """A pre-existing audit file that cannot be parsed, committed clean."""
        audit = repo / ".rlsbl" / "undo-audit.json"
        audit.write_text("not json")
        git(repo, "add", ".rlsbl/undo-audit.json")
        git(repo, "commit", "-q", "-m", "chore: pre-existing audit record")
        return audit

    def test_unwritable_audit_refuses_before_any_destruction(
        self, tmp_path, monkeypatch, capsys,
    ):
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=5)
        monkeypatch.chdir(repo)
        audit = self._corrupt_audit(repo)

        head_before = git(repo, "rev-parse", "HEAD")
        tags_before = git(repo, "tag", "-l")
        remote_before = snapshot_remote_refs(repo)

        gh = _FakeGh()
        with pytest.raises(SystemExit) as exc:
            _run_undo(repo, {}, gh=gh)
        assert exc.value.code == 1

        # The GitHub CLI was never asked to delete anything.
        assert not gh.deleted, f"gh calls: {gh.calls}"
        # No tag deleted, locally or on the remote.
        assert git(repo, "tag", "-l") == tags_before
        assert snapshot_remote_refs(repo) == remote_before
        assert remote_ref(repo, "refs/tags/v1.0.1") != ""
        # No commit reverted: the released version still stands.
        assert git(repo, "rev-parse", "HEAD") == head_before
        assert json.loads((repo / "package.json").read_text())["version"] == "1.0.1"
        assert (repo / ".rlsbl/changes/1.0.1.jsonl").exists()

        # The unreadable file is left exactly as found, for the operator.
        assert audit.read_text() == "not json"

        err = capsys.readouterr().err
        assert "undo-audit.json" in err, "the refusal must name the audit file"
        assert "not readable JSON" in err, (
            "the hard error from the audit writer must be surfaced, not "
            "swallowed into a generic warning"
        )


# --------------------------------------------------------------------------- #
# GitHub Release absence and no-tags handling
# --------------------------------------------------------------------------- #

class TestEdgeCases:

    def test_missing_github_release_is_skipped_not_failed(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=3)
        monkeypatch.chdir(repo)

        gh = _FakeGh(release_exists=False)
        _run_undo(repo, {}, gh=gh)

        out = capsys.readouterr().out
        assert "FAILED" not in out
        assert not gh.deleted, "no delete attempted when the Release is absent"
        # Undo still unwound the release.
        assert json.loads((repo / "package.json").read_text())["version"] == "1.0.0"

    def test_interleaved_foreign_commit_refuses_partial_undo(self, tmp_path, monkeypatch, capsys):
        """A foreign commit interleaved in the release sequence stops the walk
        before reaching the version-bump commit. The undo must hard-error
        rather than silently performing a partial revert."""
        repo = tmp_path / "repo"
        init_repo(repo)

        _write(repo, "package.json", json.dumps({"name": "pkg", "version": "1.0.0"}, indent=2) + "\n")
        _write(repo, ".rlsbl/config.json", json.dumps({
            "publish_mode": "ci", "targets": ["npm"],
        }, indent=2) + "\n")
        _write(repo, ".rlsbl/changes/unreleased.jsonl", json.dumps(_ENTRY) + "\n")
        _write(
            repo, ".rlsbl/releases/unreleased.toml",
            'format_version = 1\nbump = "patch"\ninclude = ["npm"]\nexclude = []\n'
            'description = "x"\n',
        )
        _write(repo, "CHANGELOG.md", "# Changelog\n")
        _write(repo, ".gitignore", ".rlsbl/releases/in-progress.json\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "initial")
        git(repo, "tag", "v1.0.0")
        archive_release(
            ledger_dir(repo), "1.0.0", git(repo, "rev-parse", "HEAD"),
            tree=git(repo, "rev-parse", "HEAD^{tree}"),
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "chore: finalize release file for 1.0.0")

        # 1. version-bump
        pkg = json.loads((repo / "package.json").read_text())
        pkg["version"] = "1.0.1"
        (repo / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "v1.0.1")

        # 2. FOREIGN commit (interleaved -- not a release commit)
        _write(repo, "foreign.txt", "unrelated work\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "unrelated feature work")

        # 3. finalize-changelog (walk starts here, hits foreign before version_bump)
        os.rename(repo / ".rlsbl/changes/unreleased.jsonl", repo / ".rlsbl/changes/1.0.1.jsonl")
        (repo / ".rlsbl/changes/unreleased.jsonl").write_text("")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "chore: finalize changelog for 1.0.1")
        archive_release(
            ledger_dir(repo), "1.0.1", git(repo, "rev-parse", "HEAD"),
            tree=git(repo, "rev-parse", "HEAD^{tree}"),
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "chore: finalize release file for 1.0.1")
        git(repo, "tag", "v1.0.1")
        add_remote(repo, repo.parent / "remote.git")
        monkeypatch.chdir(repo)

        head_before = git(repo, "rev-parse", "HEAD")
        with pytest.raises(SystemExit) as exc:
            _run_undo(repo, {})
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "version bump" in err.lower() or "version_bump" in err.lower()
        # Nothing was reverted.
        assert git(repo, "rev-parse", "HEAD") == head_before

    def test_no_releases_errors(self, tmp_path, monkeypatch, capsys):
        """Nothing archived means nothing to undo.

        This used to say "no tags found": undo asked the tag namespace what
        the latest release was. It asks the release archives now, so a
        repository with a hand-made tag and no release still reports that
        there is nothing to undo.
        """
        repo = tmp_path / "repo"
        init_repo(repo)
        _write(repo, "package.json", json.dumps({"name": "pkg", "version": "1.0.0"}) + "\n")
        _write(repo, ".rlsbl/config.json", json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "initial")
        git(repo, "tag", "v9.9.9")  # a tag with no release behind it
        monkeypatch.chdir(repo)

        with pytest.raises(SystemExit) as exc:
            _run_undo(repo, {})
        assert exc.value.code == 1
        assert "no releases recorded" in capsys.readouterr().err.lower()
