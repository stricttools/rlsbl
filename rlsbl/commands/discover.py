"""Discover command that lists projects in the rlsbl ecosystem by querying GitHub for repositories tagged with the rlsbl topic."""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


SEARCH_URL = "https://api.github.com/search/repositories?q=topic:rlsbl&sort=updated&per_page=100"
MAX_RESULTS = 1000
MAX_PAGES = 20


def _get_github_token():
    """Get a GitHub token from GITHUB_TOKEN env or `gh auth token`."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _make_request(url, token):
    """Make a GET request to the GitHub API, return parsed JSON and response headers.

    On a 403 with a Retry-After header, waits (capped at 10s) and retries once.
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "rlsbl-cli")
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            headers = dict(resp.headers)
            return data, headers
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise
        retry_after = e.headers.get("Retry-After")
        if retry_after is None:
            raise
        # Sleep for the indicated duration, capped at 10s, then retry once
        wait = min(int(retry_after), 10)
        time.sleep(wait)
        # Build a fresh request for the retry
        retry_req = urllib.request.Request(url, method="GET")
        retry_req.add_header("Accept", "application/vnd.github+json")
        retry_req.add_header("User-Agent", "rlsbl-cli")
        if token:
            retry_req.add_header("Authorization", f"token {token}")
        with urllib.request.urlopen(retry_req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            headers = dict(resp.headers)
            return data, headers


def _parse_next_link(headers):
    """Extract the 'next' URL from the Link header, or None."""
    link = headers.get("Link") or headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        if 'rel="next"' in part:
            start = part.index("<") + 1
            end = part.index(">")
            url = part[start:end]
            if not url.startswith("https://api.github.com/"):
                return None
            return url
    return None


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


def _get_authenticated_user(token):
    """Get the authenticated user's login name."""
    if not token:
        return None
    try:
        req = urllib.request.Request("https://api.github.com/user", method="GET")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "rlsbl-cli")
        req.add_header("Authorization", f"token {token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("login")
    except Exception:
        return None


def _fetch_all_repos(token):
    """Fetch all repos with the rlsbl topic, handling pagination."""
    repos = []
    url = SEARCH_URL
    pages_fetched = 0

    while url and len(repos) < MAX_RESULTS and pages_fetched < MAX_PAGES:
        data, headers = _make_request(url, token)
        items = data.get("items", [])
        repos.extend(items)
        pages_fetched += 1
        url = _parse_next_link(headers)

    return repos


def run_cmd(registry, args, flags):
    """Discover command: list projects in the rlsbl ecosystem."""
    token = _get_github_token()
    mine_only = flags.get("mine", False)

    if mine_only and not token:
        print("Error: --mine requires authentication (set GITHUB_TOKEN or install gh CLI).", file=sys.stderr)
        sys.exit(1)

    # Fetch repos
    try:
        repos = _fetch_all_repos(token)
    except urllib.error.HTTPError as e:
        print(f"Error: GitHub API returned {e.code}: {e.reason}", file=sys.stderr)
        if e.code == 403:
            print("Hint: run 'gh auth login' to increase API rate limits (60/hr unauthenticated → 5000/hr).", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: could not reach GitHub API: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Filter to --mine if requested
    if mine_only:
        username = _get_authenticated_user(token)
        if not username:
            print("Error: could not determine authenticated user.", file=sys.stderr)
            sys.exit(1)
        repos = [r for r in repos if r.get("owner", {}).get("login") == username]

    if not repos:
        if mine_only:
            print("No rlsbl-tagged repositories found for your account.")
        else:
            print("No rlsbl-tagged repositories found.")
        return

    # Build table rows
    rows = []
    for repo in repos:
        full_name = repo.get("full_name", "")
        description = repo.get("description") or ""
        updated = _relative_time(repo.get("updated_at", ""))
        rows.append((full_name, description, updated))

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
