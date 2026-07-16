"""Release checks (tag: release) verifying that local and remote git tags exist, the GitHub Release was created, and the branch is in sync.

Checks: local-tag, remote-tag, github-release, branch-sync.
"""

import subprocess

from ._common import _resolve_version_and_tag


def register_release_checks(app):
    """Register release-tag checks on *app*."""

    @app.warn_check("local-tag")
    def check_local_tag(ctx, reporter):
        """Git tag for the current version must exist locally."""
        from ..utils import tag_exists_locally

        _version, tag = _resolve_version_and_tag(ctx)
        if not tag:
            return reporter.skipped("no version detected")

        if tag_exists_locally(tag, cwd=str(ctx.project_root)):
            return reporter.passed(f"{tag} exists")
        reporter.warn(f"{tag} not found locally")
        return reporter.found(f"{tag} not found locally")

    @app.warn_check("remote-tag")
    def check_remote_tag(ctx, reporter):
        """Git tag for the current version must exist on origin."""
        from ..utils import tag_exists_locally, tag_exists_on_remote

        _version, tag = _resolve_version_and_tag(ctx)
        if not tag:
            return reporter.skipped("no version detected")

        if not tag_exists_locally(tag, cwd=str(ctx.project_root)):
            return reporter.skipped(f"{tag} not created yet")

        if tag_exists_on_remote(tag, cwd=str(ctx.project_root)):
            return reporter.passed(f"{tag} on origin")
        reporter.warn(f"{tag} not found on origin")
        return reporter.found(f"{tag} not found on origin")

    @app.warn_check("github-release")
    def check_github_release(ctx, reporter):
        """GitHub Release must exist for the current version tag."""
        from ..utils import check_gh_auth, check_gh_installed, run_gh, tag_exists_locally

        _version, tag = _resolve_version_and_tag(ctx)
        if not tag:
            return reporter.skipped("no version detected")

        if not tag_exists_locally(tag, cwd=str(ctx.project_root)):
            return reporter.skipped(f"{tag} not created yet")

        if not check_gh_installed():
            return reporter.skipped("gh CLI is not installed")
        if not check_gh_auth():
            return reporter.skipped("gh CLI is not authenticated")

        try:
            run_gh(["release", "view", tag], config=ctx.config, cwd=str(ctx.project_root))
            return reporter.passed(f"{tag} exists")
        except subprocess.CalledProcessError:
            reporter.warn(f"{tag} not found on GitHub")
            return reporter.found(f"{tag} not found on GitHub")

    @app.error_check("branch-sync")
    def check_branch_sync(ctx, reporter):
        """Local branch must be in sync with origin."""
        from ..utils import get_current_branch, run

        root_str = str(ctx.project_root)
        branch = get_current_branch()
        try:
            output = run("git", ["rev-list", "--left-right", "--count",
                                  f"origin/{branch}...HEAD"], cwd=root_str)
        except subprocess.CalledProcessError:
            return reporter.skipped(f"no remote tracking for {branch}")

        parts = output.split("\t")
        if len(parts) != 2:
            reporter.error(f"unexpected rev-list output: {output}")
            return reporter.found(f"unexpected rev-list output: {output}")

        behind, ahead = int(parts[0]), int(parts[1])
        if behind == 0 and ahead == 0:
            return reporter.passed(f"up to date with origin/{branch}")
        if behind == 0 and ahead > 0:
            reporter.warn(f"{ahead} commit(s) ahead of origin/{branch}")
            return reporter.found(f"{ahead} commit(s) ahead of origin/{branch}")
        if behind > 0 and ahead == 0:
            reporter.error(f"{behind} commit(s) behind origin/{branch}")
            return reporter.found(f"{behind} commit(s) behind origin/{branch}")
        reporter.error(f"{behind} behind, {ahead} ahead of origin/{branch}")
        return reporter.found(f"{behind} behind, {ahead} ahead of origin/{branch}")
