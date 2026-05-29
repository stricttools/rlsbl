"""Tests for monorepo impact analysis command."""

import json
import os
import subprocess

import pytest

from rlsbl.commands.monorepo.impact import (
    _cmd_impact,
    _compute_impact,
    _map_file_to_package,
)
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE
from rlsbl.workspace_graph import WorkspaceGraph


def _make_impact_workspace(root):
    """Create a workspace with a dependency chain for impact testing.

    Layout:
      models -> payments_contract -> flow_order -> app
      models -> marketplace_contract -> app

    Returns (str(root), projects_list).
    """
    projects = [
        {"path": "packages/models", "name": "models"},
        {"path": "packages/payments_contract", "name": "payments_contract",
         "depends_on": ["models"]},
        {"path": "packages/marketplace_contract", "name": "marketplace_contract",
         "depends_on": ["models"]},
        {"path": "packages/flow_order", "name": "flow_order",
         "depends_on": ["payments_contract"]},
        {"path": "packages/app", "name": "app",
         "depends_on": ["flow_order", "marketplace_contract"]},
    ]

    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)

    # Write workspace.toml with depends_on (conftest helper doesn't support it)
    lines = []
    for proj in projects:
        lines.append("[[projects]]")
        lines.append(f'path = "{proj["path"]}"')
        lines.append(f'name = "{proj["name"]}"')
        if "depends_on" in proj:
            deps_str = ", ".join(f'"{d}"' for d in proj["depends_on"])
            lines.append(f"depends_on = [{deps_str}]")
        lines.append("")
    (ws_dir / WORKSPACE_FILE).write_text("\n".join(lines))

    # Create package.json for each project so detect_targets works
    for proj in projects:
        proj_dir = root / proj["path"]
        proj_dir.mkdir(parents=True, exist_ok=True)
        pkg_json = proj_dir / "package.json"
        pkg_json.write_text(
            json.dumps({"name": proj["name"], "version": "1.0.0"})
        )

    return str(root), projects


class TestMapFileToPackage:
    def test_maps_file_to_correct_package(self, tmp_path):
        projects = [
            {"path": "packages/models", "name": "models"},
            {"path": "packages/app", "name": "app"},
        ]
        assert _map_file_to_package(
            "packages/models/src/money.dart", projects, str(tmp_path)
        ) == "models"

    def test_maps_file_at_project_root(self, tmp_path):
        projects = [{"path": "packages/models", "name": "models"}]
        # File that is exactly the project path (edge case)
        assert _map_file_to_package(
            "packages/models", projects, str(tmp_path)
        ) == "models"

    def test_returns_none_for_unmatched_file(self, tmp_path):
        projects = [{"path": "packages/models", "name": "models"}]
        assert _map_file_to_package(
            "other/dir/file.txt", projects, str(tmp_path)
        ) is None

    def test_most_specific_match_wins(self, tmp_path):
        projects = [
            {"path": "packages", "name": "outer"},
            {"path": "packages/models", "name": "models"},
        ]
        assert _map_file_to_package(
            "packages/models/src/file.dart", projects, str(tmp_path)
        ) == "models"


class TestComputeImpact:
    def _make_graph(self, tmp_path):
        root, projects = _make_impact_workspace(tmp_path)
        return WorkspaceGraph(root, projects), projects

    def test_direct_and_transitive_dependents(self, tmp_path):
        graph, _ = self._make_graph(tmp_path)
        result = _compute_impact({"models"}, graph, depth=None)
        assert set(result["direct_dependents"]) == {
            "marketplace_contract", "payments_contract"
        }
        assert set(result["transitive_dependents"]) == {
            "marketplace_contract", "payments_contract", "flow_order", "app"
        }

    def test_leaf_package_no_dependents(self, tmp_path):
        graph, _ = self._make_graph(tmp_path)
        result = _compute_impact({"app"}, graph, depth=None)
        assert result["direct_dependents"] == []
        assert result["transitive_dependents"] == []

    def test_depth_limiting(self, tmp_path):
        graph, _ = self._make_graph(tmp_path)
        result = _compute_impact({"models"}, graph, depth=1)
        # depth=1 means only direct dependents
        assert set(result["transitive_dependents"]) == {
            "marketplace_contract", "payments_contract"
        }

    def test_test_scope_equals_transitive(self, tmp_path):
        graph, _ = self._make_graph(tmp_path)
        result = _compute_impact({"models"}, graph, depth=None)
        assert result["test_scope"] == result["transitive_dependents"]
        assert result["release_candidates"] == result["transitive_dependents"]

    def test_multiple_packages_union(self, tmp_path):
        graph, _ = self._make_graph(tmp_path)
        # Impact of both models and flow_order
        result = _compute_impact({"models", "flow_order"}, graph, depth=None)
        # flow_order's direct dependent is app
        # models' direct dependents are marketplace_contract, payments_contract
        assert "app" in result["direct_dependents"]
        assert "marketplace_contract" in result["direct_dependents"]
        assert "payments_contract" in result["direct_dependents"]


class TestCmdImpactPackageMode:
    def test_package_input(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        _cmd_impact(["models"], {"format": "text"}, project_root=".")
        out = capsys.readouterr().out
        assert "Impact analysis for: models" in out
        assert "marketplace_contract" in out
        assert "payments_contract" in out
        assert "flow_order" in out
        assert "app" in out

    def test_unknown_package_errors(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        with pytest.raises(SystemExit):
            _cmd_impact(["nonexistent"], {"format": "text"}, project_root=".")

    def test_leaf_package_no_dependents(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        _cmd_impact(["app"], {"format": "text"}, project_root=".")
        out = capsys.readouterr().out
        assert "Direct dependents (0):" in out
        assert "(none)" in out

    def test_depth_flag(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        _cmd_impact(["models"], {"format": "text", "depth": 1}, project_root=".")
        out = capsys.readouterr().out
        # depth=1: only direct dependents in transitive list
        assert "Transitive dependents (2):" in out
        assert "flow_order" not in out
        assert "app" not in out


class TestCmdImpactFileMode:
    def test_file_input_maps_to_package(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        # Create the file so the path is plausible
        src_dir = tmp_path / "packages" / "models" / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "money.dart").write_text("// money model\n")

        _cmd_impact(["packages/models/src/money.dart"], {"format": "text"}, project_root=".")
        out = capsys.readouterr().out
        assert "Impact analysis for: models" in out
        assert "marketplace_contract" in out

    def test_file_not_in_any_package(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        _cmd_impact(["outside/dir/file.dart"], {"format": "text"}, project_root=".")
        err = capsys.readouterr().err
        assert "does not belong to any workspace package" in err

    def test_multiple_file_inputs(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        # Files from two different packages
        _cmd_impact(
            [
                "packages/models/src/file.dart",
                "packages/flow_order/lib/main.dart",
            ],
            {"format": "text"}, project_root=".")
        out = capsys.readouterr().out
        # Should union impacts of models and flow_order
        assert "models" in out
        assert "flow_order" in out


class TestCmdImpactGitMode:
    def test_since_flag(self, mock_git_repo, capsys):
        _make_impact_workspace(mock_git_repo)
        # Commit the workspace files
        subprocess.run(
            ["git", "add", "."],
            cwd=str(mock_git_repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "add workspace"],
            cwd=str(mock_git_repo),
            check=True,
            capture_output=True,
        )
        # Record the ref before our change
        base_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(mock_git_repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Make a change in the models package
        models_dir = mock_git_repo / "packages" / "models" / "src"
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / "change.txt").write_text("new content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=str(mock_git_repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "change models"],
            cwd=str(mock_git_repo),
            check=True,
            capture_output=True,
        )

        _cmd_impact([], {"format": "text", "since": base_ref}, project_root=".")
        out = capsys.readouterr().out
        assert "Impact analysis for: models" in out
        assert "marketplace_contract" in out


class TestCmdImpactJsonFormat:
    def test_json_output(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        _cmd_impact(["models"], {"format": "json"}, project_root=".")
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["input"] == "models"
        assert "direct_dependents" in data
        assert "transitive_dependents" in data
        assert "test_scope" in data
        assert "release_candidates" in data
        assert isinstance(data["direct_dependents"], list)
        assert set(data["direct_dependents"]) == {
            "marketplace_contract", "payments_contract"
        }

    def test_json_leaf_package(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        _cmd_impact(["app"], {"format": "json"}, project_root=".")
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["direct_dependents"] == []
        assert data["transitive_dependents"] == []


class TestCmdImpactErrors:
    def test_no_workspace(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            _cmd_impact(["models"], {"format": "text"}, project_root=".")

    def test_no_args_no_since(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        with pytest.raises(SystemExit):
            _cmd_impact([], {"format": "text"}, project_root=".")

    def test_unknown_format(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _make_impact_workspace(tmp_path)
        with pytest.raises(SystemExit):
            _cmd_impact(["models"], {"format": "yaml"}, project_root=".")
