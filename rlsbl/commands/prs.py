"""PRs command that lists open GitHub pull requests for the current repository to provide awareness of in-flight changes before releasing."""

import subprocess
import sys

from ..utils import check_gh_auth, check_gh_installed, run


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
        count_str = run("gh", ["pr", "list", "--state", "open", "--json", "number", "--jq", "length"])
        count = int(count_str)

        if count > 0:
            print(f"Open PRs: {count}")
            # Display the PR list directly to terminal (stdout not captured)
            subprocess.run(["gh", "pr", "list", "--state", "open"])

    except Exception as e:
        print(f"Warning: could not list PRs: {e}", file=sys.stderr)

    sys.exit(0)
