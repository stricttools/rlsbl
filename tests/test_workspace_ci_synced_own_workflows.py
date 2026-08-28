"""``workspace-ci-synced`` and members that ship no CI workflow of their own.

``rlsbl monorepo sync`` mints router jobs from a member's OWN CI workflow files
(``<member>/.github/workflows/ci*.yml``). A member that has none contributes
nothing to the router -- sync says so with a warning and moves on -- so the
check has nothing to demand for it either.

The member this bites is the mandated root member: its ``.github/workflows/``
directory is where the GENERATED routers sit, not where its own CI workflows
sit, and a workspace whose root member carries no CI file of its own used to be
reported as unsynced on every run.
"""

from pathlib import Path

import pytest

from conftest import run_git

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.router_filters import ROUTER_HEADER
from rlsbl.workspace import WorkspaceProject


def _init_repo(repo):
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-q", "-m", "initial")


def _make_ws_ctx(repo, projects):
    return WorkspaceCheckContext(
        project_root=Path(str(repo)),
        workspace_root=Path(str(repo)),
        config={},
        projects=projects,
        graph=None,
        releasables=[],
    )


def _write_router(repo, job_keys, *, generated=True):
    """A ci-router.yml at the workspace root carrying *job_keys*."""
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    lines = []
    if generated:
        lines.append(ROUTER_HEADER)
    lines += ["name: CI Router", "on: push", "jobs:", "  detect:", "    runs-on: ubuntu-latest"]
    for key in job_keys:
        lines.append(f"  {key}:")
        lines.append("    runs-on: ubuntu-latest")
    (wf / "ci-router.yml").write_text("\n".join(lines) + "\n")


def _write_member_ci(repo, path, basename="ci.yml"):
    """One of a member's own CI workflow sources."""
    wf = repo / path / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / basename).write_text(
        "name: CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
    )


def _run(ctx):
    return app._check_defs["workspace-ci-synced"].impl(ctx)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    monkeypatch.chdir(r)
    _init_repo(r)
    return r


class TestMemberWithoutOwnWorkflows:
    """The migrated model: a root member whose directory holds the routers."""

    @staticmethod
    def _migrated(repo):
        """A workspace whose root member ships no CI workflow of its own."""
        # The root member carries a manifest (so it detects a CI-capable
        # target) but no CI workflow file -- the shape every migrated
        # workspace has.
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "root"\nversion = "0.1.0"\n'
        )
        _write_member_ci(repo, "alpha")
        _write_router(repo, ["alpha-ci-build"])
        return [
            WorkspaceProject({"name": "root", "path": "."}),
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
        ]

    def test_the_check_passes(self, repo):
        result = _run(_make_ws_ctx(repo, self._migrated(repo)))
        assert result.status == "pass", f"{result.status}: {result.message}"

    def test_a_note_names_the_member(self, repo):
        result = _run(_make_ws_ctx(repo, self._migrated(repo)))
        assert any("root" in note for note in result.notes), result.notes

    def test_the_message_says_it_was_skipped(self, repo):
        result = _run(_make_ws_ctx(repo, self._migrated(repo)))
        assert "no CI workflow of its own" in result.message
        assert "root" in result.message

    def test_the_generated_router_is_not_the_root_members_own_workflow(self, repo):
        """A generated router in the root member's workflows directory is a
        generated artifact, never the member's own CI source -- recognized by
        the same header ``monorepo sync`` writes and reads."""
        projects = self._migrated(repo)
        # A second generated router file, named so the router's own filename
        # exclusion cannot be what saves the check.
        (repo / ".github" / "workflows" / "ci-router-old.yml").write_text(
            f"{ROUTER_HEADER}\nname: CI Router\non: push\njobs:\n"
            f"  detect:\n    runs-on: ubuntu-latest\n"
        )
        result = _run(_make_ws_ctx(repo, projects))
        assert result.status == "pass", f"{result.status}: {result.message}"

    def test_discovery_drops_the_generated_router(self, repo):
        from rlsbl.ci_router import discover_project_ci_sources

        self._migrated(repo)
        (repo / ".github" / "workflows" / "ci-router-old.yml").write_text(
            f"{ROUTER_HEADER}\njobs: {{}}\n"
        )
        assert discover_project_ci_sources(str(repo)) == []


class TestMemberWithOwnWorkflows:
    """The check's real job survives: a workflow absent from the router errors."""

    def test_a_member_missing_from_the_router_still_fails(self, repo):
        _write_member_ci(repo, "beta")
        _write_router(repo, ["alpha-ci-build"])
        projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
            WorkspaceProject({"name": "beta", "path": "beta"}),
        ]
        _write_member_ci(repo, "alpha")
        result = _run(_make_ws_ctx(repo, projects))
        assert result.status == "fail"
        assert "beta" in result.message

    def test_a_root_member_with_a_hand_authored_workflow_still_fails(self, repo):
        """The root member's OWN ci-*.yml (no generated-router header) is a
        real workflow: sync inlines it, so the router must carry its jobs."""
        wf = repo / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "ci-root.yml").write_text(
            "name: Root CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        )
        _write_member_ci(repo, "alpha")
        _write_router(repo, ["alpha-ci-build"])
        projects = [
            WorkspaceProject({"name": "root", "path": "."}),
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
        ]
        result = _run(_make_ws_ctx(repo, projects))
        assert result.status == "fail", f"{result.status}: {result.message}"
        assert "root" in result.message
        assert "root-ci-root" in result.message
