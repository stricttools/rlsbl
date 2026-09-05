"""`rlsbl release reconcile` resolves the releasable it is standing in.

The command used to discard the check context and rebuild a plain
``ProjectContext``. A plain context is not a ``WorkspaceCheckContext``, so the
one resolution the ref checks work from
(``rlsbl.checks._common._resolve_release_identity``) never reached its
releasable branch: the release record it read was ``<member>/.rlsbl/releases``,
which in a workspace does not exist. Every releasable release was therefore
invisible to the reconcile -- it reported "Nothing to reconcile" while
``rlsbl check --name unpublished-refs``, run from the same directory, named
dozens of missing refs and pointed at this very command as the remedy -- and
the plan file it wrote created a stray ``.rlsbl/`` beside the member.

The tests drive the CLI rather than ``run_cmd``, because the defect is in how
the command builds its context, not in the engine underneath it.
"""

import json
import os
from unittest.mock import patch

import pytest

import rlsbl
from conftest import make_workspace
from githarness import add_remote, commit_file, git, init_repo

from rlsbl.release_file import write_archived_release_file

MOD = "rlsbl.commands.release_reconcile"

TAG = "core@v0.1.0"
VERSION = "0.1.0"


@pytest.fixture
def workspace(tmp_path):
    """A workspace whose releasable ``core`` shipped 0.1.0, with origin lacking the tag.

    The local tag exists, the archive records the commit it shipped from, and
    the bare remote holds the branch only. That is exactly the world
    ``unpublished-refs`` reports as "missing on origin" and names the reconcile
    for.
    """
    repo = tmp_path / "ws"
    repo.mkdir()
    init_repo(repo)

    core = repo / "packages" / "core"
    (core / ".rlsbl").mkdir(parents=True)
    (core / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n",
        encoding="utf-8",
    )
    (core / "pyproject.toml").write_text(
        '[project]\nname = "core"\nversion = "0.1.0"\n', encoding="utf-8",
    )

    rel_dir = repo / ".rlsbl-monorepo" / "releasables" / "core"
    (rel_dir / "changes").mkdir(parents=True)
    (rel_dir / "releases").mkdir(parents=True)
    (rel_dir / "version").write_text("0.1.0\n", encoding="utf-8")
    (rel_dir / "changes" / "unreleased.jsonl").write_text("", encoding="utf-8")
    jsonl = rel_dir / "changes" / f"{VERSION}.jsonl"
    jsonl.write_text(
        '{"format_version":1,"commits":["0000000"],"user_facing":false}\n',
        encoding="utf-8",
    )
    os.chmod(jsonl, 0o444)

    make_workspace(
        repo,
        [{"path": "packages/core", "name": "core", "releasable": "core"}],
        releasables=[{"name": "core", "tag_format": "{name}@v{version}"}],
    )
    commit_file(repo, "packages/core/thing.py", "x = 1\n", "core 0.1.0")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "workspace")
    git(repo, "tag", TAG)

    head = git(repo, "rev-parse", "HEAD")
    write_archived_release_file(
        str(rel_dir / "releases"), VERSION,
        bump="minor", include=["pypi"], description="The first release.",
        candidate_sha=head,
        tree_hashes={"packages/core": git(repo, "rev-parse", f"{head}:packages/core")},
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "archive")

    # The remote gets the branch and NOT the tag: the divergence under test.
    add_remote(repo, tmp_path / "remote", push=False)
    git(repo, "push", "-q", "--no-verify", "origin", "main")
    return repo, rel_dir


def _reconcile(argv):
    """Run the CLI with gh unavailable, so only the ref half is judged."""
    with (
        patch(f"{MOD}.check_gh_installed", return_value=False),
        patch(f"{MOD}.check_gh_auth", return_value=False),
    ):
        return rlsbl.app.test(argv)


class TestFromAMemberDirectory:
    def test_the_missing_releasable_tag_is_planned(self, workspace, monkeypatch):
        repo, rel_dir = workspace
        monkeypatch.chdir(repo / "packages" / "core")

        result = _reconcile(["release", "reconcile", "--plan",
                             "--approve-consequential"])

        assert result.exit_code == 0, result.stderr
        assert "Nothing to reconcile" not in result.stdout, (
            "the releasable's archives were not read at all; stdout was:\n"
            + result.stdout
        )
        assert f"refs/tags/{TAG}" in result.stdout, result.stdout
        assert "materialize" in result.stdout, result.stdout

    def test_the_plan_is_written_beside_the_releasable_records(
        self, workspace, monkeypatch,
    ):
        repo, rel_dir = workspace
        monkeypatch.chdir(repo / "packages" / "core")

        result = _reconcile(["release", "reconcile", "--plan",
                             "--approve-consequential"])

        assert result.exit_code == 0, result.stderr
        assert (rel_dir / "releases" / "reconcile-plan.toml").is_file(), (
            "the plan belongs beside the release records it reconciles"
        )

    def test_no_stray_rlsbl_directory_is_created(self, workspace, monkeypatch):
        repo, _rel_dir = workspace
        monkeypatch.chdir(repo / "packages" / "core")

        _reconcile(["release", "reconcile", "--plan", "--approve-consequential"])

        assert not (repo / "packages" / "core" / ".rlsbl" / "releases").exists(), (
            "a workspace member has no .rlsbl/releases; writing one there "
            "invents a release record that nothing else reads"
        )
        assert not (repo / ".rlsbl").exists(), (
            "the workspace root has no .rlsbl either"
        )

    def test_the_releasable_selector_is_refused_here(self, workspace, monkeypatch):
        repo, _rel_dir = workspace
        monkeypatch.chdir(repo / "packages" / "core")

        result = _reconcile(["release", "reconcile", "--plan",
                             "--approve-consequential", "--releasable", "core"])

        assert result.exit_code == 1
        assert "--releasable is only accepted at the workspace root" in result.stderr


class TestFromTheWorkspaceRoot:
    def test_it_refuses_without_the_selector_and_lists_the_releasables(
        self, workspace, monkeypatch,
    ):
        repo, _rel_dir = workspace
        monkeypatch.chdir(repo)

        result = _reconcile(["release", "reconcile", "--plan",
                             "--approve-consequential"])

        assert result.exit_code == 1
        assert "--releasable" in result.stderr
        assert "core" in result.stderr

    def test_the_selector_scopes_it_to_that_releasable(self, workspace, monkeypatch):
        repo, rel_dir = workspace
        monkeypatch.chdir(repo)

        result = _reconcile(["release", "reconcile", "--plan",
                             "--approve-consequential", "--releasable", "core"])

        assert result.exit_code == 0, result.stderr
        assert f"refs/tags/{TAG}" in result.stdout, result.stdout
        assert (rel_dir / "releases" / "reconcile-plan.toml").is_file()
        assert not (repo / ".rlsbl").exists()

    def test_an_unknown_releasable_is_named(self, workspace, monkeypatch):
        repo, _rel_dir = workspace
        monkeypatch.chdir(repo)

        result = _reconcile(["release", "reconcile", "--plan",
                             "--approve-consequential", "--releasable", "nope"])

        assert result.exit_code == 1
        assert "nope" in result.stderr


class TestStandaloneIsUnchanged:
    def test_the_selector_is_refused_in_a_standalone_repository(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "solo"
        repo.mkdir()
        init_repo(repo)
        (repo / ".rlsbl" / "releases").mkdir(parents=True)
        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n",
            encoding="utf-8",
        )
        commit_file(repo, "README.md", "hi\n", "initial")
        monkeypatch.chdir(repo)

        result = _reconcile(["release", "reconcile", "--plan",
                             "--approve-consequential", "--releasable", "core"])

        assert result.exit_code == 1
        assert "standalone" in result.stderr
