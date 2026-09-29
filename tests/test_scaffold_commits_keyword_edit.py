"""Scaffold commits the "rlsbl" keyword it adds to a manifest.

``rlsbl scaffold`` adds "rlsbl" to package.json (or pyproject.toml) keywords,
and its auto-commit named every file it wrote except that manifest, so the
edit stayed uncommitted -- after ``rlsbl monorepo add`` too, which scaffolds
the member it adds. A manifest with no uncommitted changes of its own is now
committed with the scaffold; one the operator is editing is left to them.
"""

import json
import subprocess
from io import StringIO
from unittest.mock import patch

from conftest import cli_ctx


def _git(*args):
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, timeout=30,
    ).stdout


def _npm_project():
    with open("package.json", "w") as f:
        f.write(json.dumps({
            "name": "demo-pkg", "version": "0.1.0", "description": "x",
            "license": "MIT", "engines": {"node": ">=22"},
        }, indent=2) + "\n")
    _git("add", "package.json")
    _git("commit", "-q", "-m", "package")


def _scaffold():
    import rlsbl

    with patch("rlsbl.tagging.ensure_github_topic"), \
            patch("sys.stdout", new_callable=StringIO):
        rlsbl.cmd_scaffold(
            cli_ctx(), target="npm", publish_mode="ci",
            auto_commit=True, skip_shared=False, auto_tag=True,
        )


def test_a_clean_manifest_is_committed_with_its_keyword(mock_git_repo):
    _npm_project()
    _scaffold()

    assert _git("status", "--porcelain", "--", "package.json") == ""
    committed = json.loads(_git("show", "HEAD:package.json"))
    assert "rlsbl" in committed["keywords"]


def test_a_manifest_with_edits_of_its_own_is_left_uncommitted(mock_git_repo, capsys):
    _npm_project()
    data = json.loads(open("package.json").read())
    data["description"] = "work in progress"
    with open("package.json", "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")

    _scaffold()

    assert _git("status", "--porcelain", "--", "package.json").strip()
    assert "work in progress" not in _git("show", "HEAD:package.json")
    assert "uncommitted changes of its own" in capsys.readouterr().err
