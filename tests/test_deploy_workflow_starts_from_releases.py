"""The scaffolded deploy workflow starts from a published GitHub Release.

It used to start on a pushed tag matching ``v*``, which broke silently in four
ways the owner-run probe measured: ``v*`` never matches ``name@v1.2.3`` or
``path/v1.2.3`` tags, a push carrying more than three tags fires nothing, a tag
created by a workflow's ``GITHUB_TOKEN`` fires nothing, and it had no publish
gate. It now starts the way every rlsbl publish workflow does: on
``release: published`` plus manual dispatch at a tag, behind the publish gate,
one run per tag.
"""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ruamel.yaml import YAML

from rlsbl.commands.init_cmd import run_cmd, run_cmd_multi
from rlsbl.context import ProjectContext

CONFIG = {
    "publish_mode": "ci",
    "deploy": [{
        "name": "web", "host": "example.invalid", "steps": ["echo deploy"],
        "only_on": ["main"],
    }],
}


def _write_config(root, targets):
    (root / ".rlsbl").mkdir(exist_ok=True)
    (root / ".rlsbl" / "config.json").write_text(
        json.dumps({**CONFIG, "targets": targets}) + "\n"
    )
    return {**CONFIG, "targets": targets}


def _load_deploy():
    path = os.path.join(".github", "workflows", "deploy.yml")
    with open(path, encoding="utf-8") as f:
        return YAML(typ="safe").load(f)


def _assert_release_triggered(workflow):
    triggers = workflow.get("on") or workflow.get(True)
    assert "push" not in triggers, "a tag push never starts the deploy"
    assert triggers["release"] == {"types": ["published"]}
    assert "workflow_dispatch" in triggers
    jobs = workflow["jobs"]
    assert "gate" in jobs, "the deploy waits for CI like every publish"
    assert jobs["deploy"]["needs"] == "gate"
    checkout = jobs["deploy"]["steps"][0]
    assert checkout["with"]["ref"] == (
        "${{ inputs.tag || github.event.release.tag_name }}"
    )
    assert workflow["concurrency"] == {
        "group": "deploy-${{ inputs.tag || github.ref_name }}",
        "cancel-in-progress": False,
    }


def test_a_single_target_project(mock_git_repo):
    (mock_git_repo / "pyproject.toml").write_text(
        '[project]\nname = "deploy-test"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
    )
    config = _write_config(mock_git_repo, ["pypi"])
    ctx = ProjectContext(project_root=Path("."), workspace_root=None,
                         config=config)
    with patch("sys.stdout", new_callable=StringIO):
        run_cmd("pypi", [], {}, ctx=ctx)
    _assert_release_triggered(_load_deploy())


def test_a_multi_target_project(mock_git_repo):
    (mock_git_repo / "pyproject.toml").write_text(
        '[project]\nname = "deploy-test"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
    )
    (mock_git_repo / "go.mod").write_text(
        "module github.com/test/deploy-test\n\ngo 1.23\n"
    )
    config = _write_config(mock_git_repo, ["pypi", "go"])
    ctx = ProjectContext(project_root=Path("."), workspace_root=None,
                         config=config)
    with patch("sys.stdout", new_callable=StringIO):
        run_cmd_multi(["pypi", "go"], [], {}, ctx=ctx)
    _assert_release_triggered(_load_deploy())
