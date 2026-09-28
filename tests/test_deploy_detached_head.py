"""`rlsbl deploy` in the scaffolded, Release-triggered deploy workflow.

The workflow checks out the Release's tag, so HEAD is detached and there is no
current branch to hold against a target's ``only_on``. The deploy used to
refuse every detached HEAD, so the scaffolded workflow could never succeed.
A detached HEAD now deploys when its commit is reachable from one of the
target's ``only_on`` branches as origin has them; no checked-out branch is
needed.

The runner's checkout is simulated with the git commands ``actions/checkout``
runs for a tag ref: with ``fetch-depth: 0`` it fetches every branch into
``refs/remotes/origin/`` and every tag, then checks out ``refs/tags/<tag>``;
with the default depth of 1 it fetches only the tag, shallow. Every refusal's
fix is applied and seen to clear it.
"""

import json
import os
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.deploy_cmd import run_cmd
from rlsbl.context import ProjectContext

TARGET = {"name": "web", "host": "example.invalid", "steps": ["echo deploy"],
          "only_on": ["main"]}


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _origin_with_tags(root):
    """A bare origin whose main carries v1.0.0, and a feature branch v0.9.0-rc."""
    origin = root / "origin.git"
    _git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    dev = root / "dev"
    _git(root, "init", "-q", "-b", "main", str(dev))
    _git(dev, "config", "user.email", "t@t.local")
    _git(dev, "config", "user.name", "T")
    (dev / ".rlsbl").mkdir()
    (dev / ".rlsbl" / "config.json").write_text(
        json.dumps({"deploy": [TARGET]}) + "\n"
    )
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", "config")
    (dev / "app.txt").write_text("1\n")
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", "app")
    _git(dev, "tag", "v1.0.0")
    (dev / "later.txt").write_text("later\n")
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", "later on main")
    _git(dev, "switch", "-q", "-c", "feature", "HEAD~1")
    (dev / "feature.txt").write_text("f\n")
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", "feature work")
    _git(dev, "tag", "v0.9.0-rc")
    _git(dev, "remote", "add", "origin", str(origin))
    _git(dev, "push", "-q", "origin", "main", "feature", "v1.0.0", "v0.9.0-rc")
    return origin


def _runner_checkout(root, origin, tag, *, fetch_depth):
    """The checkout actions/checkout performs for *tag* at *fetch_depth*."""
    work = root / f"runner-{tag}-{fetch_depth}"
    _git(root, "init", "-q", str(work))
    _git(work, "remote", "add", "origin", str(origin))
    if fetch_depth == 0:
        _git(work, "fetch", "-q", "--prune", "--no-recurse-submodules", "origin",
             "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*")
    else:
        _git(work, "fetch", "-q", "--no-tags", "--prune",
             "--no-recurse-submodules", f"--depth={fetch_depth}", "origin",
             f"+refs/tags/{tag}:refs/tags/{tag}")
    _git(work, "-c", "advice.detachedHead=false", "checkout", "-q", "--force",
         f"refs/tags/{tag}")
    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", "detached"
    return work


def _deploy(work, *, dry_run=True, target=None):
    config = json.loads((work / ".rlsbl" / "config.json").read_text())
    if target is not None:
        config["deploy"] = [target]
    ctx = ProjectContext(project_root=Path(work), workspace_root=None,
                         config=config)
    out, err = StringIO(), StringIO()
    code = 0
    cwd = os.getcwd()
    os.chdir(work)
    try:
        with patch("sys.stdout", out), patch("sys.stderr", err):
            try:
                run_cmd(None, [], {"dry-run": dry_run}, ctx=ctx)
            except SystemExit as exc:
                code = exc.code
    finally:
        os.chdir(cwd)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def origin(tmp_path):
    return _origin_with_tags(tmp_path)


class TestADetachedHeadAtAReleaseTag:

    def test_a_tag_on_main_deploys_from_a_full_checkout(self, tmp_path, origin):
        work = _runner_checkout(tmp_path, origin, "v1.0.0", fetch_depth=0)
        code, out, err = _deploy(work)
        assert code == 0, err
        assert "Deploy target: web" in out
        assert "main" in out

    def test_the_live_deploy_runs_for_the_reachable_branch(self, tmp_path, origin):
        from rlsbl.deploy import DeployResult

        work = _runner_checkout(tmp_path, origin, "v1.0.0", fetch_depth=0)
        with patch("rlsbl.commands.deploy_cmd.deploy_target",
                   return_value=DeployResult("web", True, "Deployed")) as dt:
            code, out, err = _deploy(work, dry_run=False)
        assert code == 0, err
        assert dt.call_args[0][1] == "main"

    def test_a_tag_on_no_allowed_branch_is_refused_until_its_branch_is_allowed(
        self, tmp_path, origin,
    ):
        work = _runner_checkout(tmp_path, origin, "v0.9.0-rc", fetch_depth=0)
        code, out, err = _deploy(work)
        assert code == 1
        assert "reachable from none of" in err
        assert "only_on" in err
        # The fix the refusal names: allow the branch that carries it.
        code, out, err = _deploy(work, target={**TARGET,
                                               "only_on": ["main", "feature"]})
        assert code == 0, err
        assert "feature" in out

    def test_a_shallow_tag_only_checkout_is_refused_until_fetch_depth_0(
        self, tmp_path, origin,
    ):
        work = _runner_checkout(tmp_path, origin, "v1.0.0", fetch_depth=1)
        code, out, err = _deploy(work)
        assert code == 1
        assert "fetch-depth: 0" in err
        # The fix the refusal names: the full-history checkout.
        work = _runner_checkout(tmp_path, origin, "v1.0.0", fetch_depth=0)
        code, out, err = _deploy(work)
        assert code == 0, err

    def test_a_shallow_checkout_clears_with_the_named_local_fetch(
        self, tmp_path, origin,
    ):
        work = _runner_checkout(tmp_path, origin, "v1.0.0", fetch_depth=1)
        code, out, err = _deploy(work)
        assert "git fetch --unshallow origin main" in err
        _git(work, "fetch", "-q", "--unshallow", "origin", "main")
        code, out, err = _deploy(work)
        assert code == 0, err

    def test_a_full_tag_only_checkout_clears_with_the_named_branch_fetch(
        self, tmp_path, origin,
    ):
        work = tmp_path / "tags-only"
        _git(tmp_path, "init", "-q", str(work))
        _git(work, "remote", "add", "origin", str(origin))
        _git(work, "fetch", "-q", "--no-tags", "origin",
             "+refs/tags/v1.0.0:refs/tags/v1.0.0")
        _git(work, "-c", "advice.detachedHead=false", "checkout", "-q",
             "refs/tags/v1.0.0")
        code, out, err = _deploy(work)
        assert code == 1
        assert "it has no origin/main" in err
        assert "`git fetch origin main` if it is not shallow" in err
        _git(work, "fetch", "-q", "origin", "main")
        code, out, err = _deploy(work)
        assert code == 0, err


class TestTheScaffoldedWorkflow:
    """The workflow `rlsbl scaffold` writes checks out what the deploy needs."""

    def test_its_checkout_carries_the_history_the_branch_check_reads(
        self, tmp_path, origin, mock_git_repo,
    ):
        from test_deploy_workflow_starts_from_releases import (
            _load_deploy,
            test_a_single_target_project,
        )

        test_a_single_target_project(mock_git_repo)
        checkout = _load_deploy()["jobs"]["deploy"]["steps"][0]
        assert checkout["uses"].startswith("actions/checkout@")
        # Simulate exactly what that step asks of actions/checkout.
        depth = int(checkout["with"].get("fetch-depth", 1))
        work = _runner_checkout(tmp_path, origin, "v1.0.0", fetch_depth=depth)
        code, out, err = _deploy(work)
        assert code == 0, err


def test_an_attached_branch_keeps_the_plain_branch_check(tmp_path, origin):
    work = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "-b", "feature", str(origin), str(work))
    code, out, err = _deploy(work)
    assert code == 1
    assert 'current branch "feature" is not in allowed branches' in err
