"""E2E test: `rlsbl release resume` invoked at the WORKSPACE ROOT.

The CLI entry used to call _require_sub_project_root(), which sys.exit(1)s
at a workspace root that is not itself a member -- before
resolve_resume_source's workspace-root branch (find the single in-flight
releasable) could ever run. The entry must reach the resolver.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl import app
from rlsbl.commands.release.release_state import (
    get_state_path,
    save_release_state,
)
from rlsbl.workspace import (
    Releasable,
    get_releasable_changes_dir,
    get_releasable_dir,
    save_workspace,
    write_releasable_version,
)


def _git(repo, *args):
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_head(repo):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _setup_workspace_with_inflight_state(root):
    """Releasable workspace 'alpha' (member packages/core) with an
    in-flight release state file at the releasable location."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")

    core = root / "packages" / "core"
    core.mkdir(parents=True)
    (core / "package.json").write_text(
        json.dumps({"name": "core", "version": "1.0.1"}, indent=2) + "\n"
    )
    (core / ".rlsbl").mkdir()
    (core / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["npm"], "pipelines": {}}) + "\n"
    )
    save_workspace(
        str(root),
        [{"path": "packages/core", "name": "core", "releasable": "alpha"}],
        releasables=[Releasable(name="alpha")],
    )
    write_releasable_version(str(root), "alpha", "1.0.1")
    changes_dir = get_releasable_changes_dir(str(root), "alpha")
    os.makedirs(changes_dir, exist_ok=True)
    (Path(changes_dir) / "unreleased.jsonl").write_text("")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")

    rel_dir = get_releasable_dir(str(root), "alpha")
    state_path = get_state_path(str(core), releasable_dir=rel_dir)
    save_release_state(state_path, {
        "new_version": "1.0.1",
        "tag": "alpha@v1.0.1",
        "branch": "main",
        "pre_release_sha": _git_head(root),
        "bump_type": "patch",
        "registry": "npm",
        "completed_steps": ["VERSION_BUMPED", "COMMITTED"],
        "failed_steps": {},
        "companion_tags": [],
        "monorepo_name": "core",
        "releasable_name": "alpha",
        "commit_msg": "alpha: release v1.0.1",
        "description": "",
        "context": "",
        "include": ["npm"],
        "exclude": [],
        "preid": "",
        "blog": False,
    })
    return state_path


class TestResumeFromWorkspaceRootCli:

    def test_resume_at_workspace_root_reaches_resolver(self, tmp_project):
        """Invoking `rlsbl release resume` at the workspace root finds the
        single in-flight releasable and hands its state to resume_cmd."""
        _setup_workspace_with_inflight_state(tmp_project)

        with patch("rlsbl.commands.release.resume_cmd") as mock_resume:
            result = app.test(["release", "resume", "--no-watch", "--yes"])

        assert result.exit_code == 0, (
            f"resume from the workspace root must not exit: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert mock_resume.call_count == 1
        saved = mock_resume.call_args[0][0]
        assert saved["new_version"] == "1.0.1"
        assert saved["releasable_name"] == "alpha"

    def test_resume_at_workspace_root_no_state_errors_cleanly(self, tmp_project):
        """Without in-flight state, the resolver's error message is shown
        (not the 'not inside any registered project' sub-project error)."""
        _setup_workspace_with_inflight_state(tmp_project)
        # Remove the state file
        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        os.unlink(os.path.join(rel_dir, "releases", "in-progress.json"))

        result = app.test(["release", "resume", "--no-watch", "--yes"])
        assert result.exit_code == 1
        combined = (result.stdout or "") + (result.stderr or "")
        assert "cannot resume from monorepo root" in combined
        assert "not inside any registered project" not in combined
