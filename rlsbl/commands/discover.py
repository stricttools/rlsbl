"""Discover command that lists projects in the rlsbl ecosystem by querying GitHub for repositories tagged with the rlsbl topic.

Every GitHub call here goes through ``gh api``, which resolves and applies
the credential inside its own process.  rlsbl deliberately never asks gh for
the raw token: a live credential on a captured stdout pipe is what the
observe standard forbids (see :mod:`rlsbl.observe_allowlist`).
"""

import shutil
import subprocess
import sys
from .. import effects


SEARCH_PATH = "search/repositories?q=topic:rlsbl&sort=updated&per_page=100"
MAX_RESULTS = 1000

#: Per-repo fields the listing renders, extracted by gh's own jq so the
#: paginated stream arrives as one TSV row per repository.
_REPO_JQ = (
    '.items[] | [.full_name, (.description // ""), .updated_at, .owner.login] '
    '| @tsv'
)


def _gh_api(args, *, timeout):
    """Read the GitHub API through ``gh api``; None when gh cannot answer.

    ``--method GET`` is mandatory rather than incidental: it is what makes the
    argv match the GET-pinned observe prefix, so these reads really execute
    under ``--dry-run`` instead of being recorded.
    """
    try:
        result = effects.gh(
            ["api", "--method", "GET", *args],
            capture_output=True, text=True, check=True, timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError):
        return None
    if effects.unsettled(result):
        return None
    return result.stdout


def _relative_time(iso_timestamp):
    """Convert an ISO 8601 timestamp to a relative time string like '2d ago'."""
    from datetime import datetime, timezone

    if not iso_timestamp:
        return ""

    # Parse ISO timestamp (GitHub uses Z suffix)
    ts = iso_timestamp.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    now = datetime.now(timezone.utc)
    delta = now - dt

    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def _get_authenticated_user():
    """The authenticated user's login name, or None when gh cannot say."""
    raw = _gh_api(["user", "--jq", ".login"], timeout=10)
    if raw is None:
        return None
    return raw.strip() or None


def _fetch_all_repos():
    """Every repo carrying the rlsbl topic, as ``(name, desc, updated, owner)``.

    ``gh api --paginate`` walks the Link header itself, and the ``--jq``
    projection turns each page into one TSV row per repository, so the
    concatenated pages parse without a per-page JSON document boundary.
    Returns None when gh could not answer at all.
    """
    raw = _gh_api(
        ["--paginate", SEARCH_PATH, "--jq", _REPO_JQ], timeout=120,
    )
    if raw is None:
        return None
    repos = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        repos.append(tuple(fields))
        if len(repos) >= MAX_RESULTS:
            break
    return repos


def run_cmd(registry, args, flags):
    """Discover command: list projects in the rlsbl ecosystem."""
    mine_only = flags.get("mine", False)

    repos = _fetch_all_repos()
    if repos is None:
        print(
            "Error: could not query the GitHub API through gh. Run "
            "'gh auth login' (or set GH_TOKEN) and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Filter to --mine if requested
    if mine_only:
        username = _get_authenticated_user()
        if not username:
            print("Error: could not determine authenticated user.", file=sys.stderr)
            sys.exit(1)
        repos = [r for r in repos if r[3] == username]

    if not repos:
        if mine_only:
            print("No rlsbl-tagged repositories found for your account.")
        else:
            print("No rlsbl-tagged repositories found.")
        return

    # Build table rows
    rows = [
        (full_name, description, _relative_time(updated_at))
        for full_name, description, updated_at, _owner in repos
    ]

    # Calculate column widths
    name_width = max(len(r[0]) for r in rows)
    desc_width = max(len(r[1]) for r in rows)
    time_width = max(len(r[2]) for r in rows)

    # Ensure minimum widths match headers
    name_width = max(name_width, len("owner/repo"))
    time_width = max(time_width, len("updated"))

    # Calculate available description width from terminal size.
    # Layout: "  {name}  {desc}  {time}" -- 6 chars of padding (2+2+2).
    term_width = shutil.get_terminal_size().columns
    available_desc = term_width - name_width - time_width - 6
    max_desc = max(available_desc, len("description"))
    if desc_width > max_desc:
        desc_width = max_desc

    # Print header
    print(f"\nrlsbl ecosystem ({len(repos)} projects)\n")
    header = f"  {'owner/repo':<{name_width}}  {'description':<{desc_width}}  {'updated':<{time_width}}"
    print(header)
    separator_len = name_width + desc_width + time_width + 6
    print(f"  {'─' * separator_len}")

    # Print rows
    for full_name, description, updated in rows:
        # Truncate long descriptions
        if len(description) > max_desc:
            description = description[:max_desc - 1] + "…"
        print(f"  {full_name:<{name_width}}  {description:<{desc_width}}  {updated}")
