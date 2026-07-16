"""Tests for the --registry flag on rlsbl status.

When --registry is passed, the status command queries the package registry
for the latest published version and displays drift (AHEAD, BEHIND, SAME,
UNPUBLISHED, ERROR, PRIVATE).
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from conftest import make_ctx


def _make_npm_project(base_path, name="test-pkg", version="0.1.0"):
    """Create a minimal npm project (package.json)."""
    proj_dir = str(base_path)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": name, "version": version}, f)


def _commit_file(repo, name, content="x\n", message="change"):
    """Create/modify a file and commit it."""
    fp = os.path.join(str(repo), name)
    with open(fp, "w") as f:
        f.write(content)
    subprocess.run(["git", "add", name], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(repo), check=True)


class TestStatusRegistryAhead:
    """Registry found, local version ahead of registry."""

    def test_ahead_text_output(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="1.2.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        with patch("rlsbl.registry.query_registry_version") as mock_query:
            mock_query.return_value = {"status": "found", "version": "1.1.0"}
            capsys.readouterr()
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {"registry": True}, ctx=make_ctx(".", config={"publish_mode": "ci"}))
            out = capsys.readouterr().out

        assert "Registry:" in out
        assert "1.1.0" in out
        assert "AHEAD" in out
        assert "npm" in out

    def test_ahead_json_output(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="1.2.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        with patch("rlsbl.registry.query_registry_version") as mock_query:
            mock_query.return_value = {"status": "found", "version": "1.1.0"}
            capsys.readouterr()
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {"registry": True, "json": True}, ctx=make_ctx(".", config={"publish_mode": "ci"}))
            out = capsys.readouterr().out

        data = json.loads(out)
        assert data["registry_version"] == "1.1.0"
        assert data["drift"] == "AHEAD"


class TestStatusRegistrySame:
    """Registry found, local version matches registry."""

    def test_same_text_output(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="2.0.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        with patch("rlsbl.registry.query_registry_version") as mock_query:
            mock_query.return_value = {"status": "found", "version": "2.0.0"}
            capsys.readouterr()
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {"registry": True}, ctx=make_ctx(".", config={"publish_mode": "ci"}))
            out = capsys.readouterr().out

        assert "Registry:" in out
        assert "2.0.0" in out
        assert "SAME" in out


class TestStatusRegistryUnpublished:
    """Registry not found (package not published)."""

    def test_unpublished_text_output(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        with patch("rlsbl.registry.query_registry_version") as mock_query:
            mock_query.return_value = {"status": "not_found"}
            capsys.readouterr()
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {"registry": True}, ctx=make_ctx(".", config={"publish_mode": "ci"}))
            out = capsys.readouterr().out

        assert "Registry:" in out
        assert "not found on npm" in out

    def test_unpublished_json_output(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        with patch("rlsbl.registry.query_registry_version") as mock_query:
            mock_query.return_value = {"status": "not_found"}
            capsys.readouterr()
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {"registry": True, "json": True}, ctx=make_ctx(".", config={"publish_mode": "ci"}))
            out = capsys.readouterr().out

        data = json.loads(out)
        assert data["registry_version"] is None
        assert data["drift"] == "UNPUBLISHED"


class TestStatusRegistryPrivate:
    """Private project skips registry query."""

    def test_private_text_output(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        ctx = make_ctx(".", config={"publish_mode": "none"})
        with patch("rlsbl.registry.query_registry_version") as mock_query:
            capsys.readouterr()
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {"registry": True}, ctx=ctx)
            out = capsys.readouterr().out
            # Registry query should NOT be called for private projects
            mock_query.assert_not_called()

        assert "Registry:" in out
        assert "skipped, private project" in out

    def test_private_json_output(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        ctx = make_ctx(".", config={"publish_mode": "none"})
        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {"registry": True, "json": True}, ctx=ctx)
        out = capsys.readouterr().out

        data = json.loads(out)
        assert data["registry_version"] is None
        assert data["drift"] == "PRIVATE"


class TestStatusRegistryError:
    """Registry query fails with an error."""

    def test_error_text_output(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        with patch("rlsbl.registry.query_registry_version") as mock_query:
            mock_query.return_value = {"status": "error", "message": "HTTP 500"}
            capsys.readouterr()
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {"registry": True}, ctx=make_ctx(".", config={"publish_mode": "ci"}))
            out = capsys.readouterr().out

        assert "Registry:" in out
        assert "query failed" in out

    def test_error_json_output(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        with patch("rlsbl.registry.query_registry_version") as mock_query:
            mock_query.return_value = {"status": "error", "message": "HTTP 500"}
            capsys.readouterr()
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {"registry": True, "json": True}, ctx=make_ctx(".", config={"publish_mode": "ci"}))
            out = capsys.readouterr().out

        data = json.loads(out)
        assert data["registry_version"] is None
        assert data["drift"] == "ERROR"


class TestStatusRegistryFlagNotSet:
    """When --registry flag is not set, no registry query is made."""

    def test_no_registry_query_without_flag(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        with patch("rlsbl.registry.query_registry_version") as mock_query:
            capsys.readouterr()
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {}, ctx=make_ctx(".", config={"publish_mode": "ci"}))
            out = capsys.readouterr().out
            mock_query.assert_not_called()

        assert "Registry:" not in out

    def test_no_registry_fields_in_json_without_flag(self, mock_git_repo, capsys):
        _make_npm_project(mock_git_repo, name="my-pkg", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {"json": True}, ctx=make_ctx(".", config={"publish_mode": "ci"}))
        out = capsys.readouterr().out

        data = json.loads(out)
        assert data["registry_version"] is None
        assert data["drift"] is None
