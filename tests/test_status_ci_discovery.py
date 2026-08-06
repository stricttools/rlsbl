"""Tests for CI-workflow discovery in ``rlsbl status``.

``rlsbl status`` used to hardcode ``.github/workflows/ci.yml``, so every repo
scaffolded with per-target CI file names (``ci-go.yml``, ``ci-py.yml``, ...)
reported ``CI: missing`` even though its CI was fully wired. Status now routes
through ``ci_router.discover_project_ci_sources`` -- the same discovery that
feeds ``monorepo sync`` and the release CI gate -- and reports every source it
finds.
"""

import json
import os

from conftest import make_ctx

from rlsbl.commands.status import run_cmd


def _make_npm_project(base_path, name="test-pkg", version="1.0.0"):
    with open(os.path.join(str(base_path), "package.json"), "w") as f:
        json.dump({"name": name, "version": version}, f)


def _write_workflow(base_path, filename, body="name: ci\non: push\n"):
    wf_dir = os.path.join(str(base_path), ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    with open(os.path.join(wf_dir, filename), "w") as f:
        f.write(body)


def _status_out(capsys):
    capsys.readouterr()
    run_cmd("npm", [], {}, ctx=make_ctx("."))
    return capsys.readouterr().out


def _ci_line(out):
    for line in out.splitlines():
        if line.startswith("CI:"):
            return line
    raise AssertionError(f"no CI line in status output:\n{out}")


class TestStatusCiDiscovery:
    def test_target_named_ci_file_is_discovered(self, mock_git_repo, capsys):
        """A repo with only ``ci-go.yml`` must not report ``CI: missing``."""
        _make_npm_project(mock_git_repo)
        _write_workflow(mock_git_repo, "ci-go.yml")

        line = _ci_line(_status_out(capsys))
        assert "missing" not in line
        assert "ci-go.yml" in line

    def test_plain_ci_yml_still_discovered(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo)
        _write_workflow(mock_git_repo, "ci.yml")

        line = _ci_line(_status_out(capsys))
        assert "missing" not in line
        assert "ci.yml" in line

    def test_every_discovered_source_is_reported(self, mock_git_repo, capsys):
        """Multi-target repos list all their CI sources, not just the first."""
        _make_npm_project(mock_git_repo)
        _write_workflow(mock_git_repo, "ci.yml")
        _write_workflow(mock_git_repo, "ci-go.yml")
        _write_workflow(mock_git_repo, "ci-pypi.yml")

        line = _ci_line(_status_out(capsys))
        assert "ci.yml" in line
        assert "ci-go.yml" in line
        assert "ci-pypi.yml" in line

    def test_generated_router_is_not_a_source(self, mock_git_repo, capsys):
        """``ci-router.yml`` is generated output, never a project CI source."""
        _make_npm_project(mock_git_repo)
        _write_workflow(mock_git_repo, "ci-router.yml")

        line = _ci_line(_status_out(capsys))
        assert "missing" in line
        assert "ci-router.yml" not in line

    def test_no_workflows_reports_missing(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo)

        line = _ci_line(_status_out(capsys))
        assert "missing" in line

    def test_json_reports_discovered_sources(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo)
        _write_workflow(mock_git_repo, "ci-go.yml")
        _write_workflow(mock_git_repo, "ci-npm.yml")

        capsys.readouterr()
        run_cmd("npm", [], {"json": True}, ctx=make_ctx("."))
        data = json.loads(capsys.readouterr().out)
        assert data["ci"] == ["ci-go.yml", "ci-npm.yml"]

    def test_json_reports_empty_list_when_missing(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo)

        capsys.readouterr()
        run_cmd("npm", [], {"json": True}, ctx=make_ctx("."))
        data = json.loads(capsys.readouterr().out)
        assert data["ci"] == []
