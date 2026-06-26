"""Release checks (tag: release) verifying that local and remote git tags exist, the GitHub Release was created, and the branch is in sync.

Checks: local-tag, remote-tag, github-release, branch-sync.
"""

import subprocess

from strictcli import CheckResult

from ._common import _resolve_version_and_tag


def register_release_checks(app):
    """Register release-tag checks on *app*."""

    @app.check("local-tag")
    def check_local_tag(ctx):
        """Git tag for the current version must exist locally."""
        from ..utils import run

        _version, tag = _resolve_version_and_tag(ctx)
        if not tag:
            return CheckResult("skip", "no version detected")

        output = run("git", ["tag", "-l", tag], cwd=str(ctx.project_root))

        if output:
            return CheckResult("pass", f"{tag} exists")
        return CheckResult("warn", f"{tag} not found locally")

    @app.check("remote-tag")
    def check_remote_tag(ctx):
        """Git tag for the current version must exist on origin."""
        from ..utils import run

        _version, tag = _resolve_version_and_tag(ctx)
        if not tag:
            return CheckResult("skip", "no version detected")

        output = run("git", ["ls-remote", "--tags", "origin", tag], cwd=str(ctx.project_root))

        if output:
            return CheckResult("pass", f"{tag} on origin")
        return CheckResult("warn", f"{tag} not found on origin")

    @app.check("github-release")
    def check_github_release(ctx):
        """GitHub Release must exist for the current version tag."""
        from ..utils import check_gh_auth, check_gh_installed, gh_env, run

        _version, tag = _resolve_version_and_tag(ctx)
        if not tag:
            return CheckResult("skip", "no version detected")

        if not check_gh_installed():
            return CheckResult("fail", "gh CLI is not installed")
        if not check_gh_auth():
            return CheckResult("fail", "gh CLI is not authenticated")

        try:
            run("gh", ["release", "view", tag], env=gh_env(ctx.config), cwd=str(ctx.project_root))
            return CheckResult("pass", f"{tag} exists")
        except subprocess.CalledProcessError:
            return CheckResult("warn", f"{tag} not found on GitHub")

    @app.check("branch-sync")
    def check_branch_sync(ctx):
        """Local branch must be in sync with origin."""
        from ..utils import get_current_branch, run

        root_str = str(ctx.project_root)
        branch = get_current_branch()
        try:
            output = run("git", ["rev-list", "--left-right", "--count",
                                  f"origin/{branch}...HEAD"], cwd=root_str)
        except subprocess.CalledProcessError:
            return CheckResult("warn", f"no remote tracking for {branch}")

        parts = output.split("\t")
        if len(parts) != 2:
            return CheckResult("warn", f"unexpected rev-list output: {output}")

        behind, ahead = int(parts[0]), int(parts[1])
        if behind == 0 and ahead == 0:
            return CheckResult("pass", f"up to date with origin/{branch}")
        if behind == 0 and ahead > 0:
            return CheckResult("warn", f"{ahead} commit(s) ahead of origin/{branch}")
        if behind > 0 and ahead == 0:
            return CheckResult("fail", f"{behind} commit(s) behind origin/{branch}")
        return CheckResult("fail", f"{behind} behind, {ahead} ahead of origin/{branch}")
