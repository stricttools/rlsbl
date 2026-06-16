"""Shared utilities: subprocess runner, git helpers, version bumping, changelog extraction, commit tooling, and GitHub API queries."""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

from .errors import ConfigError, VersionError


def run(cmd, args=None, timeout=120, env=None, cwd=None):
    """Run a command with args, return trimmed stdout. Raise on failure."""
    full_cmd = [cmd] + (args or [])
    result = subprocess.run(full_cmd, capture_output=True, text=True, check=True, timeout=timeout, env=env, cwd=cwd)
    return result.stdout.strip()


def warn_exception(context: str, exc: Exception) -> None:
    """Print a warning with optional traceback for non-fatal errors."""
    import traceback
    print(f"Warning: {context}: {exc}", file=sys.stderr)
    traceback.print_exc()


def require_tool(name, purpose=None, fatal=True):
    """Check that a CLI tool is available on PATH.

    Args:
        name: command name (e.g., "uv", "npm", "go").
        purpose: optional human-readable reason ("for editable install"),
            included in the error message when fatal.
        fatal: if True, raise FileNotFoundError on missing tool; if False,
            return None silently.

    Returns the resolved path to the tool, or None if missing and not fatal.
    """
    path = shutil.which(name)
    if path is None:
        msg = f"Required tool not found on PATH: {name}"
        if purpose:
            msg += f" (needed {purpose})"
        if fatal:
            raise FileNotFoundError(msg)
        return None
    return path



def find_project_root(start=None):
    """Walk up from start (default: cwd) to find .rlsbl/ or .rlsbl-monorepo/.

    Returns the directory path containing the marker, or None if not found.
    Prefers the nearest ancestor with either marker.
    """
    current = os.path.abspath(start or ".")
    while True:
        if os.path.isdir(os.path.join(current, ".rlsbl")):
            return current
        if os.path.isdir(os.path.join(current, ".rlsbl-monorepo")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def is_clean_tree():
    """Returns True if the git working tree is clean (no uncommitted changes)."""
    status = run("git", ["status", "--porcelain"])
    return len(status) == 0


def get_current_branch():
    """Returns the current git branch name."""
    return run("git", ["rev-parse", "--abbrev-ref", "HEAD"])


def get_push_timeout(config):
    """Return the push timeout in seconds.

    Precedence: RLSBL_PUSH_TIMEOUT env var > config dict push_timeout
    > default 120 (the documented contract).

    ``config`` is the project config dict (already loaded).
    """
    raw = os.environ.get("RLSBL_PUSH_TIMEOUT")
    if raw is not None:
        try:
            val = int(raw)
            if val <= 0:
                raise ValueError
            return val
        except ValueError:
            raise ConfigError(
                f'Invalid RLSBL_PUSH_TIMEOUT="{raw}". Must be a positive integer.'
            )

    config_val = config.get("push_timeout")
    if config_val is not None:
        if not isinstance(config_val, int) or config_val <= 0:
            raise ConfigError(
                f'Invalid push_timeout in .rlsbl/config.json: {config_val!r}. '
                f'Must be a positive integer.'
            )
        return config_val

    return 120


def get_hook_timeout():
    """Return the hook timeout in seconds, from RLSBL_HOOK_TIMEOUT or default None.

    If not set, returns None (no timeout — hooks run to completion).
    If set, parses as a positive integer.
    On invalid value, prints a warning and returns None.
    """
    raw = os.environ.get("RLSBL_HOOK_TIMEOUT")
    if raw is None:
        return None
    try:
        val = int(raw)
        if val <= 0:
            raise ValueError
        return val
    except ValueError:
        print(f'Warning: invalid RLSBL_HOOK_TIMEOUT="{raw}", ignoring (no timeout)', file=sys.stderr)
        return None


def remote_branch_exists(branch):
    """Check whether origin/{branch} exists as a valid ref."""
    try:
        run("git", ["rev-parse", "--verify", f"origin/{branch}"])
        return True
    except subprocess.CalledProcessError:
        return False


def push_if_needed(branch, env=None, *, config):
    """Push the branch to origin if local is ahead of remote.

    Args:
        branch: branch name to push.
        env: optional environment dict passed to the push subprocess (e.g. to
            set ``RLSBL_RELEASE_PUSH=1`` so the pre-push hook recognises the
            push as release-authorized). Defaults to None (inherit current env).
        config: project config dict forwarded to get_push_timeout.
    """
    timeout = get_push_timeout(config)
    local = run("git", ["rev-parse", branch])
    if not remote_branch_exists(branch):
        run("git", ["push", "-u", "origin", branch], timeout=timeout, env=env)
        return
    remote = run("git", ["rev-parse", f"origin/{branch}"])
    if local != remote:
        run("git", ["push", "origin", branch], timeout=timeout, env=env)


def extract_changelog_entry_from_text(content, version):
    """Extract a changelog entry for a specific version from a markdown string.

    Looks for a heading like '## 1.2.3' and captures everything
    until the next heading or EOF.
    """
    escaped_version = re.escape(version)
    header_pattern = re.compile(r"^## " + escaped_version + r"\s*$", re.MULTILINE)
    match = header_pattern.search(content)
    if not match:
        return None

    # Start after the matched header line
    start_idx = match.end()
    # Find the next "## " heading or use end of string
    next_heading_idx = content.find("\n## ", start_idx)
    end_idx = len(content) if next_heading_idx == -1 else next_heading_idx
    entry = content[start_idx:end_idx].strip()
    return entry or None


def extract_changelog_entry(changelog_path, version):
    """Extract a changelog entry for a specific version.

    Looks for a heading like '## 1.2.3' and captures everything
    until the next heading or EOF.
    """
    with open(changelog_path, "r", encoding="utf-8") as f:
        content = f.read()
    return extract_changelog_entry_from_text(content, version)


def check_gh_installed():
    """Check that the gh CLI is installed."""
    try:
        run("gh", ["--version"])
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def check_gh_auth():
    """Check that the gh CLI is authenticated."""
    try:
        run("gh", ["auth", "status"])
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


_safegit_warning_shown = False


def find_commit_tool():
    """Detect safegit or fall back to git for committing.

    Returns "safegit" if available on PATH, otherwise "git".
    Prints a one-time warning to stderr when falling back to git.
    """
    global _safegit_warning_shown
    if require_tool("safegit", fatal=False):
        return "safegit"
    if not _safegit_warning_shown:
        print(
            "note: safegit not found, using git (concurrent commits may conflict)",
            file=sys.stderr,
        )
        _safegit_warning_shown = True
    return "git"


def has_staged_or_modified(paths: list[str], cwd: str | None = None) -> bool:
    """Check if any of the given paths have staged or unstaged changes.

    When cwd is set, git commands run from that directory and os.path.exists
    checks are resolved relative to it. Paths must be relative to cwd (or
    absolute).
    """
    for p in paths:
        abs_p = os.path.join(cwd, p) if cwd and not os.path.isabs(p) else p
        diff = run("git", ["diff", "--name-only", "--", p], cwd=cwd) if os.path.exists(abs_p) else ""
        status = run("git", ["status", "--porcelain", "--", p], cwd=cwd)
        if diff or status:
            return True
    return False


def commit_files(
    message: str,
    files: list[str],
    allow_failure: bool = False,
    autogenerated: bool = True,
    cwd: str | None = None,
) -> bool:
    """Commit specific files using safegit (preferred) or git.

    When autogenerated is True, passes ``--trailer "Autogenerated: true"`` to
    the commit command (supported by git 2.32+ and safegit 0.10.0+).

    When cwd is set, the commit tool runs from that directory. File paths
    must be relative to cwd (or absolute). This is needed in monorepo mode
    where paths are relative to the repo root but the process CWD is a
    sub-project directory.

    Returns True on success. When allow_failure is True, catches errors and
    returns False with a warning to stderr. When False, exceptions propagate.
    """
    try:
        trailer_args = ["--trailer", "Autogenerated: true"] if autogenerated else []
        tool = find_commit_tool()
        if tool == "safegit":
            run(tool, ["commit", "--yes", *trailer_args, "-m", message, "--", *files], cwd=cwd)
        else:
            run("git", ["add", *files], cwd=cwd)
            run("git", ["commit", *trailer_args, "-m", message], cwd=cwd)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if allow_failure:
            print(f"Warning: commit failed: {e}", file=sys.stderr)
            return False
        raise


def commit_files_if_changed(
    message: str,
    files: list[str],
    skip_message: str = "No changes to commit.",
    autogenerated: bool = True,
    cwd: str | None = None,
) -> bool:
    """Commit files only if they have actual changes, otherwise print skip_message.

    Returns True if a commit was made, False if nothing changed.
    Raises on commit failure (never uses allow_failure=True).
    """
    status = run("git", ["status", "--porcelain", "--", *files], cwd=cwd)
    if not status:
        print(skip_message)
        return False
    commit_files(message, files, allow_failure=False, autogenerated=autogenerated, cwd=cwd)
    return True


def bump_version(version, bump_type):
    """Bump a semver version string by the given type (patch, minor, major).

    Handles pre-release suffixes (e.g. "1.0.0-beta.1"): the suffix is stripped
    and the bump is applied to the base version.

    Returns the new version string (always clean, no pre-release suffix).
    """
    # Strip pre-release suffix (everything after the first hyphen)
    base_version = version.split("-", 1)[0]

    parts = base_version.split(".")
    if len(parts) != 3:
        raise VersionError(f'Invalid semver version: "{version}"')
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        raise VersionError(f'Invalid semver version: "{version}"')

    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        raise VersionError(f'Invalid bump type: "{bump_type}". Use patch, minor, or major.')


def is_private_repo():
    """Detect if the current repo is private via GitHub API.

    Returns True if private, False if public, None if detection fails.
    """
    try:
        remote = run("git", ["remote", "get-url", "origin"])
        # Extract owner/repo from git@github.com:owner/repo or https://github.com/owner/repo
        match = re.search(r"github\.com[/:]([^/]+/[^/.]+)", remote)
        if not match:
            return None
        repo_name = match.group(1).removesuffix(".git")
        owner, repo = repo_name.split("/", 1)

        token = run("gh", ["auth", "token"])
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}"
        )
        req.add_header("Authorization", f"Bearer {token.strip()}")
        req.add_header("User-Agent", "rlsbl-cli")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("private", False)
    except Exception:
        return None
