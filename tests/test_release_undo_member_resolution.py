"""`rlsbl release undo` resolves the member it is standing in, by membership.

``undo`` reverts ONE project's latest release, and which project that is used to
be answered by walking up from the cwd to the nearest ``.rlsbl/`` or
``.rlsbl-monorepo/`` directory. Inside a workspace that walk is wrong for every
member whose per-package ``.rlsbl/`` was cleaned up (the ordinary state after
``rlsbl monorepo cleanup``): it leaves the member entirely, stops at the
workspace root, and the command then answers for the ROOT member -- a dev node
with no release records at all -- instead of the releasable the operator is
standing in.

The test drives the CLI rather than ``run_cmd``, because the defect is in how
the command resolves its project, not in the engine underneath it.
"""

import json
import os
from unittest.mock import patch

import pytest

import rlsbl
from conftest import make_workspace
from githarness import add_remote, commit_file, git, init_repo

from rlsbl.evidence_gate import Evidence, EvidenceKind, GateResult, Verdict
from rlsbl.release_file import write_archived_release_file

TAG = "core@v0.1.0"
VERSION = "0.1.0"


@pytest.fixture
def workspace(tmp_path):
    """A workspace whose releasable ``core`` shipped 0.1.0 from a marker-less member."""
    repo = tmp_path / "ws"
    repo.mkdir()
    init_repo(repo)

    core = repo / "packages" / "core"
    core.mkdir(parents=True)
    (core / "pyproject.toml").write_text(
        '[project]\nname = "core"\nversion = "0.1.0"\n', encoding="utf-8",
    )

    rel_dir = repo / ".rlsbl-monorepo" / "releasables" / "core"
    (rel_dir / "changes").mkdir(parents=True)
    (rel_dir / "releases").mkdir(parents=True)
    (rel_dir / "version").write_text("0.1.0\n", encoding="utf-8")
    (rel_dir / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n",
        encoding="utf-8",
    )
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
    git(repo, "commit", "-q", "-m", f"core: release v{VERSION}")
    git(repo, "tag", TAG)

    head = git(repo, "rev-parse", "HEAD")
    write_archived_release_file(
        str(rel_dir / "releases"), VERSION,
        bump="minor", include=["pypi"], description="The first release.",
        candidate_sha=head,
        tree_hashes={
            "packages/core": git(repo, "rev-parse", f"{head}:packages/core"),
        },
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "archive")

    add_remote(repo, tmp_path / "remote", push=False)
    git(repo, "push", "-q", "--no-verify", "origin", "main")
    return repo, rel_dir


def _cleared_gate(*_a, **_k):
    return GateResult(
        Verdict.CLEARED,
        [Evidence("registry_probe", "pypi", EvidenceKind.UNPUBLISHED,
                  "not on pypi")],
        "unpublished",
    )


def _undo(argv):
    """Run the CLI with gh present and the registry probe answered locally."""
    with (
        patch("rlsbl.commands.undo.check_gh_installed", return_value=True),
        patch("rlsbl.commands.undo.check_gh_auth", return_value=True),
        patch("rlsbl.commands.undo.run_evidence_gate", side_effect=_cleared_gate),
    ):
        return rlsbl.app.test(argv)


class TestFromAMarkerlessMemberDirectory:
    def test_it_plans_the_members_own_release(self, workspace, monkeypatch):
        repo, _rel_dir = workspace
        monkeypatch.chdir(repo / "packages" / "core")

        result = _undo([
            "release", "undo", "--dry-run", "--approve-consequential",
        ])

        assert result.exit_code == 0, result.stderr
        assert TAG in result.stdout, (
            "the member resolved to the workspace root instead of its own "
            "releasable; output was:\n" + result.stdout + result.stderr
        )
