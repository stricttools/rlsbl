"""PRs command that lists open GitHub pull requests for the current repository to provide awareness of in-flight changes before releasing."""

import os
import subprocess
import sys

from ..utils import check_gh_auth, check_gh_installed, get_github_repo, run_gh


def run_cmd(registry, args, flags):
    """List open pull requests for the current repository.

    Designed to be safe for hooks -- always exits 0, never crashes the session.
    """
    try:
        if not check_gh_installed():
            print("prs: gh CLI not found, skipping.", file=sys.stderr)
            sys.exit(0)

        if not check_gh_auth():
            print("prs: gh not authenticated, skipping.", file=sys.stderr)
            sys.exit(0)

        # Get the count of open PRs (captured via run utility)
        count_str = run_gh(["pr", "list", "--state", "open", "--json", "number", "--jq", "length"])
        count = int(count_str)

        if count > 0:
            print(f"Open PRs: {count}")
            # Display the PR list directly to terminal (stdout not captured).
            # Cannot use run_gh here because run() hardcodes capture_output=True
            # and we need interactive terminal output.
            repo = get_github_repo()
            env = {**os.environ, "GH_REPO": repo} if repo else None
            subprocess.run(["gh", "pr", "list", "--state", "open"], env=env)

    except Exception as e:
        print(f"Warning: could not list PRs: {e}", file=sys.stderr)

    sys.exit(0)
