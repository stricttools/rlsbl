"""Shared utilities: subprocess runner, git helpers, version bumping, changelog extraction, commit tooling, and GitHub API queries."""

import glob
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from enum import Enum

from .errors import ConfigError, GitError, VersionError
from . import effects


def run(cmd, args=None, timeout=120, env=None, cwd=None):
    """Run a command with args, return trimmed stdout. Raise on failure.

    Under ``--dry-run`` a non-observe command is recorded rather than executed,
    and there is no stdout to trim: the carrier standing in for the run is
    returned instead.  A caller that ignores the return value (every mutation
    that only needed to happen) previews cleanly; a caller that reads the
    string truncates the preview, which is the honest answer for output that
    was never produced.
    """
    full_cmd = [cmd] + (args or [])
    result = effects.run(full_cmd, capture_output=True, text=True, check=True, timeout=timeout, env=env, cwd=cwd)
    if effects.unsettled(result):
        return result
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
    current = os.path.realpath(start or ".")
    while True:
        if os.path.isdir(os.path.join(current, ".rlsbl")):
            return current
        if os.path.isdir(os.path.join(current, ".rlsbl-monorepo")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def detect_uv_workspace_root(project_dir: str) -> str | None:
    """Walk up from project_dir to find a uv workspace root that includes it as a member.

    Checks each ancestor for a pyproject.toml with [tool.uv.workspace.members].
    If found, expands member globs and excludes, then checks if project_dir
    is in the resolved member set.

    Returns the workspace root directory, or None if project_dir is not a
    member of any uv workspace.
    """
    abs_project = os.path.abspath(project_dir)
    current = abs_project
    while True:
        pyproject_path = os.path.join(current, "pyproject.toml")
        if os.path.isfile(pyproject_path):
            try:
                with open(pyproject_path, "rb") as f:
                    data = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError):
                pass
            else:
                members_patterns = (
                    data.get("tool", {}).get("uv", {}).get("workspace", {}).get("members")
                )
                if members_patterns is not None:
                    # Expand member globs
                    member_dirs: set[str] = set()
                    for pattern in members_patterns:
                        for match in glob.glob(os.path.join(current, pattern)):
                            if os.path.isdir(match):
                                member_dirs.add(os.path.abspath(match))

                    # Expand and remove exclude globs
                    exclude_patterns = (
                        data.get("tool", {}).get("uv", {}).get("workspace", {}).get("exclude", [])
                    )
                    for pattern in exclude_patterns:
                        for match in glob.glob(os.path.join(current, pattern)):
                            abs_match = os.path.abspath(match)
                            member_dirs.discard(abs_match)

                    if abs_project in member_dirs:
                        return current

        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def is_virtual_uv_root(project_dir: str) -> bool:
    """True iff *project_dir* is a virtual uv workspace root.

    A virtual root has a ``pyproject.toml`` declaring a ``[tool.uv.workspace]``
    but no ``[project]`` table. It aggregates member packages for editable
    installs but is not itself a distributable package -- it has no name and
    no version, so ``pypi.detect()`` must not claim it and version/name/
    publish checks do not apply to it.

    Returns False when there is no pyproject.toml, when it cannot be parsed,
    when there is no ``[tool.uv.workspace]``, or when a ``[project]`` table is
    present (a real package that also happens to define a workspace).
    """
    pyproject_path = os.path.join(project_dir, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return False
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    has_workspace = (
        data.get("tool", {}).get("uv", {}).get("workspace") is not None
    )
    return has_workspace and "project" not in data


def is_clean_tree():
    """Returns True if the git working tree is clean (no uncommitted changes)."""
    status = run("git", ["--no-optional-locks", "status", "--porcelain"])
    return len(status) == 0


def get_last_version_tag(tag_glob: str = "v*", *, cwd=None) -> str | None:
    """Get the most recent version tag reachable from HEAD.

    When tag_glob is set (e.g. ``mylib@v*`` in monorepo mode), uses it
    as the ``--match`` pattern so each project resolves its own last
    release tag.

    ``cwd`` selects the repository the git commands run in; None means the
    process cwd. Callers that resolve a tag for a specific project (external
    checks, which may run with a ``cwd`` override) must pass it.

    Returns the tag string on success. On failure, checks whether the
    repo is a shallow clone:
    - If shallow: raises GitError (shallow clones lack the history needed
      for changelog validation).
    - If not shallow: returns None (no tags exist -- genuine first release).
    """
    try:
        return run(
            "git", ["describe", "--tags", "--abbrev=0", "--match", tag_glob],
            timeout=10, cwd=cwd,
        )
    except subprocess.CalledProcessError:
        # git describe failed -- check if this is a shallow clone
        try:
            is_shallow = run("git", ["rev-parse", "--is-shallow-repository"], timeout=10, cwd=cwd)
        except subprocess.CalledProcessError:
            # Can't determine shallow status -- assume not shallow
            return None
        if is_shallow == "true":
            raise GitError(
                "Shallow clone detected — rlsbl requires full history for "
                "changelog validation. Run 'git fetch --unshallow' first."
            )
        return None


def get_current_branch(*, cwd):
    """Returns the current git branch name for the repo at ``cwd``.

    ``cwd`` is REQUIRED (keyword-only) and must be the project/repo directory:
    there is intentionally no default that falls back to the process cwd. A
    default-to-process-cwd let a test run this (and the subsequent
    ``push_if_needed``) against whatever repo the test process happened to be
    in -- the real dev repo -- causing a stray ``git push``. Every caller now
    declares which repo it means.

    Raises GitError when HEAD is detached (git returns the literal string
    "HEAD"), since callers like push_if_needed would silently misbehave
    by operating on ``origin/HEAD``.
    """
    result = run("git", ["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if effects.unsettled(result):
        raise GitError(
            "the current branch cannot be read here: this is a preview that "
            "has already recorded a mutation, and the framework answers every "
            "observe after one with a stale carrier. The branch is fixed for "
            "the whole run -- resolve it before the first recorded effect and "
            "pass it down."
        )
    if result == "HEAD":
        raise GitError("HEAD is detached — release operations require a named branch")
    return result


# Shipped timeout defaults. Deliberately generous: they are a backstop against
# a genuinely hung subprocess, not a performance budget, and every observed real
# cost (slow pushes over saturated links, full test suites with a cold uv cache)
# must fit comfortably underneath. Override per project with the matching
# config key in .rlsbl/config.json, or per invocation with the CLI flag.
DEFAULT_PUSH_TIMEOUT = 300
DEFAULT_CHECK_TIMEOUT = 900
# Budget for the release's in-process CI wait (main-as-candidate ordering): the
# release pushes the candidate untagged and blocks here until CI concludes.
# Real CI matrices routinely run 20-40 minutes; one hour is the backstop.
DEFAULT_CI_TIMEOUT = 3600


def _resolve_timeout(config, key, default, *, override=None):
    """Resolve a timeout: explicit override > config key > shipped default.

    There is deliberately NO environment-variable layer. A timeout is either
    declared in the project's config or passed explicitly on the command line;
    an ambient env var configuring release behavior is exactly the kind of
    invisible state this tool refuses to have.

    A present-but-invalid value (config or override) is a hard error naming the
    key and the value -- never silently ignored, never silently defaulted.
    """
    if override is not None:
        if not isinstance(override, int) or isinstance(override, bool) or override <= 0:
            raise ConfigError(
                f"Invalid --{key.replace('_', '-')} value: {override!r}. "
                f"Must be a positive integer."
            )
        return override

    if config is not None:
        value = config.get(key)
        if value is not None:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(
                    f"Invalid {key} in .rlsbl/config.json: {value!r}. "
                    f"Must be a positive integer."
                )
            return value

    return default


def get_push_timeout(config=None, *, override=None):
    """Return the push timeout in seconds.

    Precedence: ``override`` (the ``--push-timeout`` CLI flag) > config dict
    ``push_timeout`` > :data:`DEFAULT_PUSH_TIMEOUT`.

    ``config`` may be None (override > default only).
    """
    return _resolve_timeout(config, "push_timeout", DEFAULT_PUSH_TIMEOUT,
                            override=override)


def get_check_timeout(config=None, *, override=None):
    """Return the check timeout in seconds.

    Precedence: ``override`` > config dict ``check_timeout`` >
    :data:`DEFAULT_CHECK_TIMEOUT`.

    ``config`` may be None (override > default only).
    """
    return _resolve_timeout(config, "check_timeout", DEFAULT_CHECK_TIMEOUT,
                            override=override)


def get_ci_timeout(config=None, *, override=None):
    """Return the release CI-wait timeout in seconds.

    Precedence: ``override`` (the ``--ci-timeout`` CLI flag) > config dict
    ``ci_timeout`` > :data:`DEFAULT_CI_TIMEOUT`.

    ``config`` may be None (override > default only).
    """
    return _resolve_timeout(config, "ci_timeout", DEFAULT_CI_TIMEOUT,
                            override=override)


def get_hook_timeout(config=None, *, override=None):
    """Return the release-hook timeout in seconds, or None for no timeout.

    Precedence: ``override`` > config dict ``hook_timeout`` > None. The default
    is deliberately "no timeout": release hooks legitimately run whole test
    suites and deploys, and killing one mid-flight is worse than waiting.

    A present-but-invalid value is a hard error (:class:`ConfigError`).
    """
    return _resolve_timeout(config, "hook_timeout", None, override=override)


def validate_timeout_override(config_key, value):
    """Validate a CLI timeout override, naming the FLAG in the error.

    ``--check-timeout`` and ``--hook-timeout`` reach their consumers by being
    written into the in-memory config (see ``apply_timeout_overrides``), which
    means an invalid flag value used to surface much later as "Invalid
    check_timeout in .rlsbl/config.json" -- pointing the operator at a file
    they never touched. Validating at the argv boundary keeps the blame where
    it belongs.
    """
    _resolve_timeout(None, config_key, None, override=value)


def remote_branch_exists(branch, cwd=None):
    """Check whether origin/{branch} exists as a valid ref."""
    try:
        run("git", ["rev-parse", "--verify", f"origin/{branch}"], cwd=cwd)
        return True
    except subprocess.CalledProcessError:
        return False


def tag_exists_locally(tag, cwd=None):
    """Check whether a git tag exists in the local repository.

    Answers False when the question is unanswerable -- a preview past its first
    recorded mutation, where the framework replies to every observe with a
    stale carrier. "The tag is not there yet" is the state a preview is
    describing anyway: a preview creates no tags, so one it cannot see is one
    the run it previews would still have to create.
    """
    output = run("git", ["tag", "-l", tag], cwd=cwd)
    if effects.unsettled(output):
        return False
    return bool(output.strip())


def tag_exists_on_remote(tag, cwd=None):
    """Check whether a git tag exists on the origin remote.

    False when unanswerable, for the same reason as
    :func:`tag_exists_locally`.
    """
    output = run("git", ["ls-remote", "--tags", "origin", tag], cwd=cwd)
    if effects.unsettled(output):
        return False
    return bool(output.strip())


class RemoteTagState(Enum):
    """Tri-state outcome of a commit-aware remote tag lookup."""

    PRESENT = "present"
    ABSENT = "absent"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class RemoteTagResult:
    """Result of resolving a tag to its peeled commit on a remote.

    Attributes:
        state: PRESENT (tag found; ``commit`` is set), ABSENT (ls-remote
            succeeded but no matching ref), or INCONCLUSIVE (ls-remote failed
            due to network/auth/timeout; ``error`` carries the underlying text).
        commit: the peeled commit SHA the tag points to. Only set when state
            is PRESENT; None otherwise. For annotated tags this is the ``^{}``
            peeled line (the commit), not the tag-object SHA.
        error: underlying error text when state is INCONCLUSIVE; None otherwise.
    """

    state: RemoteTagState
    commit: str | None = None
    error: str | None = None


def remote_tag_commit(tag, cwd=None, remote="origin", timeout=30):
    """Resolve a tag to the commit it points to on ``remote`` (default origin).

    Runs ``git ls-remote --tags <remote> <tag>`` and returns a tri-state
    ``RemoteTagResult``:

    - PRESENT with the peeled commit SHA. For annotated tags, ls-remote prints
      two lines -- ``<tag-object-sha> refs/tags/<tag>`` and
      ``<commit-sha> refs/tags/<tag>^{}``. The ``^{}`` peeled line is the
      commit and is preferred. For lightweight tags there is a single line
      whose SHA is already the commit.
    - ABSENT when ls-remote succeeds but returns no matching ref.
    - INCONCLUSIVE when ls-remote fails (network, auth, timeout); the failure
      text is carried in ``error``.

    Both ``refs/tags/<tag>`` and ``refs/tags/<tag>^{}`` are passed as explicit
    match patterns: a bare ``<tag>`` pattern matches only the direct ref and
    suppresses the peeled ``^{}`` line, which would make annotated tags
    false-negative to the tag-object SHA instead of the commit.
    """
    full_ref = f"refs/tags/{tag}"
    try:
        result = effects.run(
            ["git", "ls-remote", "--tags", remote, full_ref, f"{full_ref}^{{}}"],
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        error_text = getattr(e, "stderr", None) or str(e)
        return RemoteTagResult(RemoteTagState.INCONCLUSIVE, error=str(error_text).strip())
    if effects.unsettled(result):
        # A preview past its first recorded mutation: the framework answers
        # observes with a stale carrier. ABSENT rather than INCONCLUSIVE --
        # INCONCLUSIVE is fail-closed and would abort a preview that has
        # created no tag for the remote to disagree with.
        return RemoteTagResult(RemoteTagState.ABSENT)

    direct = None
    peeled = None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref == full_ref:
            direct = sha
        elif ref == f"{full_ref}^{{}}":
            peeled = sha

    commit = peeled or direct
    if commit is None:
        return RemoteTagResult(RemoteTagState.ABSENT)
    return RemoteTagResult(RemoteTagState.PRESENT, commit=commit)


def resolve_tag_push_plan(tags, cwd=None, remote="origin"):
    """Commit-aware decision for pushing one or more release tags.

    Each tag in ``tags`` must exist locally; its peeled commit is resolved via
    ``git rev-parse refs/tags/<tag>^{}`` (which yields the commit for both
    lightweight and annotated tags). For each tag the remote state is resolved
    with :func:`remote_tag_commit`:

    - INCONCLUSIVE (ls-remote failed): raise :class:`GitError` carrying the
      underlying error. A release must never push blind after an inconclusive
      remote probe -- the old bare ``except Exception: pass`` skip that pushed
      anyway is exactly the bug this replaces.
    - PRESENT at a different commit than the local tag: raise :class:`GitError`
      naming the tag, the local SHA, and the divergent remote SHA. A tag must
      never be force-moved by a release.
    - PRESENT at the same commit: idempotent -- already pushed.
    - ABSENT: needs pushing.

    Returns ``True`` when at least one tag needs pushing (the caller runs the
    push; git no-ops the already-present-identical refs pushed alongside the
    missing ones -- verified: ``git push origin <present-identical> <absent>``
    exits 0). Returns ``False`` when every tag is already present at the
    matching commit (the caller skips the push entirely -- the idempotent
    resume/re-entry case).
    """
    needs_push = False
    for tag in tags:
        local_commit = run(
            "git", ["rev-parse", f"refs/tags/{tag}^{{}}"], cwd=cwd
        ).strip()
        result = remote_tag_commit(tag, cwd=cwd, remote=remote)
        if result.state is RemoteTagState.INCONCLUSIVE:
            raise GitError(
                f"Could not determine the remote state of tag {tag} before "
                f"pushing (ls-remote failed): {result.error}. Refusing to push "
                f"blind -- resolve the remote access issue and retry."
            )
        if result.state is RemoteTagState.ABSENT:
            needs_push = True
            continue
        # PRESENT: must point at the same commit as the local tag.
        if result.commit != local_commit:
            raise GitError(
                f"Tag {tag} already exists on {remote} at a different commit "
                f"(local {local_commit}, remote {result.commit}). Refusing to "
                f"overwrite -- a release must never force-move an existing tag. "
                f"Investigate the divergence before retrying."
            )
    return needs_push


def push_if_needed(branch, *, config, cwd, sha=None):
    """Push the branch to origin if local is ahead of remote.

    The push runs with ``--no-verify``: this is a release-internal push, and
    the pre-push hook exists to catch MANUAL pushes to release branches. There
    is no environment-variable handshake -- bypassing the hook is expressed by
    not running it.

    Args:
        branch: branch name to push.
        config: project config dict forwarded to get_push_timeout.
        cwd: REQUIRED (keyword-only) repo directory the git commands run from.
            There is deliberately no process-cwd default: an unanchored push
            once executed a real ``git push`` from the test-runner's own repo.
            Callers must pass the project root explicitly.
        sha: optional explicit commit to publish as ``<sha>:refs/heads/<branch>``
            instead of pushing the branch ref by name. The release flow always
            passes it: the commit that CI verified is the commit that is
            published, and a ride-in that landed on the local branch after the
            candidate was pinned can never be swept along by the push.
    """
    timeout = get_push_timeout(config)
    refspec = f"{sha}:refs/heads/{branch}" if sha else branch
    local = run("git", ["rev-parse", sha or branch], cwd=cwd)
    if not remote_branch_exists(branch, cwd=cwd):
        try:
            run("git", ["push", "--no-verify", "-u", "origin", refspec], timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise GitError(f"Push timed out after {timeout}s — remote state may be inconsistent. Check with: git push --dry-run") from e
        return
    remote = run("git", ["rev-parse", f"origin/{branch}"], cwd=cwd)
    if local != remote:
        try:
            run("git", ["push", "--no-verify", "origin", refspec], timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise GitError(f"Push timed out after {timeout}s — remote state may be inconsistent. Check with: git push --dry-run") from e


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
        run_gh_unscoped(["--version"])
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def check_gh_auth():
    """Check that the gh CLI is authenticated for github.com.

    The host is named explicitly: github.com is the only forge rlsbl talks to,
    and the observe allowlist pins this exact argv so the bare ``gh auth
    status`` prefix cannot also admit ``--show-token``.
    """
    try:
        run_gh_unscoped(["auth", "status", "--hostname", "github.com"])
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
        diff = run("git", ["--no-optional-locks", "diff", "--name-only", "--", p], cwd=cwd) if os.path.exists(abs_p) else ""
        status = run("git", ["--no-optional-locks", "status", "--porcelain", "--", p], cwd=cwd)
        if diff or status:
            return True
    return False


def partition_stageable(
    paths: list[str], cwd: str | None = None,
) -> tuple[list[str], list[str]]:
    """Split ``paths`` into (stageable, ignored), preserving the given order.

    *Stageable* means git reports some status for the path -- modified, staged,
    deleted or untracked -- so naming it in a commit contributes something to
    the tree. A path git reports nothing for contributes nothing: ``git add``
    stages a no-op and safegit 0.29+ refuses the whole commit with "nothing to
    commit for <path>: staging it leaves the tree of <ref> unchanged".

    *Ignored* means the path is untracked AND matched by a gitignore rule. That
    is not a no-op, it is a mistake -- a commit was told to carry a file the
    repository has been configured to exclude -- so it is reported back rather
    than quietly dropped, and :func:`commit_files` refuses on it.

    Paths must be relative to ``cwd`` (or absolute), matching the convention
    every commit helper here uses.
    """
    stageable: list[str] = []
    ignored: list[str] = []
    for p in paths:
        status = run(
            "git",
            ["--no-optional-locks", "status", "--porcelain", "--ignored=matching", "--", p],
            cwd=cwd,
        )
        lines = [ln for ln in status.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(ln.startswith("!!") for ln in lines):
            ignored.append(p)
        else:
            stageable.append(p)
    return stageable, ignored


def assert_git_toplevel(cwd: str | None, expected_root: str) -> None:
    """Hard-error if the git repo discovered from ``cwd`` is not ``expected_root``.

    Closes the junk-commit class: a commit whose ``cwd`` is a non-git fixture
    directory (e.g. a TMPDIR created inside the repo) makes git walk UP to the
    nearest enclosing repo -- the real dev repo -- and commit there. Comparing
    the resolved ``git rev-parse --show-toplevel`` against the caller's declared
    project root and refusing on mismatch makes that impossible.

    Silent no-op when ``cwd`` is not inside any git repo (callers gate commits
    on :func:`is_git_repo` separately); a mismatch is a hard :class:`GitError`.
    """
    try:
        toplevel = run("git", ["rev-parse", "--show-toplevel"], cwd=cwd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    resolved = os.path.realpath(toplevel)
    expected = os.path.realpath(expected_root)
    if resolved != expected:
        raise GitError(
            f"refusing to commit: resolved git repo {resolved!r} is not the "
            f"expected project root {expected!r}. The commit cwd resolved into a "
            f"different repository (a TMPDIR-inside-repo or mis-anchored path). "
            f"Anchor the commit to the intended project root."
        )


def commit_files(
    message: str,
    files: list[str],
    allow_failure: bool = False,
    autogenerated: bool = True,
    cwd: str | None = None,
    expected_root: str | None = None,
    return_result: bool = False,
    require_change: bool = False,
) -> bool:
    """Commit specific files using safegit (preferred) or git.

    When autogenerated is True, passes ``--trailer "Autogenerated: true"`` to
    the commit command (supported by git 2.32+ and safegit 0.10.0+).

    When cwd is set, the commit tool runs from that directory. File paths
    must be relative to cwd (or absolute). This is needed in monorepo mode
    where paths are relative to the repo root but the process CWD is a
    sub-project directory.

    When ``expected_root`` is set, the git repo discovered from ``cwd`` must be
    that project root (:func:`assert_git_toplevel`) or the commit is refused
    with a hard error -- the guard against committing into the wrong repository.

    Returns True on success. When allow_failure is True, catches errors and
    returns False with a warning to stderr. When False, exceptions propagate.

    ``return_result`` returns the commit run's own result instead of True.
    Under a preview that result is the framework's carrier standing in for the
    commit that was recorded rather than made -- the Phase-A executor forwards
    it into the candidate push, which is how the preview renders the push of a
    commit that does not exist. Callers that only need success must NOT set it:
    branching on a carrier truncates the preview. A skipped commit (see below)
    returns True even under ``return_result``, since no run happened; in live
    mode nothing reads that value, and a preview never skips.

    **Unchanged files are never named.** Callers list a fixed file set -- the
    release's finalize step names ``CHANGELOG.md`` whether or not the
    regeneration altered a byte -- and safegit 0.29+ refuses a commit that
    names a path whose staging leaves the tree unchanged. The list is filtered
    through :func:`partition_stageable` first, so every caller inherits the
    behavior; a gitignored path is a hard error rather than a drop, and an
    empty result is a stated no-op. ``require_change`` turns that no-op into a
    hard error, for the caller whose expected change MUST have materialized.

    The filter is skipped under a preview: the writes a preview's commit would
    carry were recorded rather than performed, so asking the working tree about
    them would drop every file from a commit that is itself only recorded.
    """
    if expected_root is not None:
        assert_git_toplevel(cwd, expected_root)
    if not effects.previewing():
        stageable, ignored = partition_stageable(files, cwd=cwd)
        if ignored:
            raise GitError(
                f"refusing to commit: {', '.join(ignored)} "
                f"{'is' if len(ignored) == 1 else 'are'} gitignored and untracked, "
                f"so the commit {message!r} would carry nothing for "
                f"{'it' if len(ignored) == 1 else 'them'}. Un-ignore the path or "
                f"stop naming it in this commit."
            )
        if not stageable:
            if require_change:
                raise GitError(
                    f"refusing to commit {message!r}: nothing to commit -- none of "
                    f"the named files changed ({', '.join(files) or 'no files named'}). "
                    f"The change this commit exists to record did not materialize."
                )
            print(
                f"Nothing to commit for {message!r} (named files are unchanged); "
                f"skipping."
            )
            return True
        files = stageable
    try:
        trailer_args = ["--trailer", "Autogenerated: true"] if autogenerated else []
        tool = find_commit_tool()
        if tool == "safegit":
            result = run(
                tool, ["commit", *trailer_args, "-m", message, "--", *files],
                cwd=cwd,
            )
        else:
            run("git", ["add", *files], cwd=cwd)
            result = run("git", ["commit", *trailer_args, "-m", message], cwd=cwd)
        return result if return_result else True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if allow_failure:
            print(f"Warning: commit failed: {e}", file=sys.stderr)
            return False
        raise


def is_git_repo(path: str | None = None) -> bool:
    """Return True if ``path`` (default: process cwd) is inside a git work tree."""
    try:
        out = run("git", ["rev-parse", "--is-inside-work-tree"], cwd=path)
        return out.strip() == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def commit_scaffold_file(
    message: str,
    files: list[str],
    *,
    cwd: str | None = None,
    expected_root: str | None = None,
) -> None:
    """Commit a freshly-scaffolded file, failing loudly on commit error.

    The scaffold write has already succeeded by the time this is called.
    Outside a git repository there is nothing to commit, so this is a no-op
    (matching the project convention: never commit or git-init in a non-git
    directory). Inside a git repository, a commit failure is a hard error with
    an actionable message -- it is never silently swallowed, because a
    scaffolded release file that is on disk but not committed can silently
    block or corrupt a later release.

    Args:
        message: commit message.
        files: file paths to commit (relative to ``cwd`` or absolute).
        cwd: directory the commit tool runs from; also the directory whose
            git-repo membership is checked.
        expected_root: when set, the git repo discovered from ``cwd`` must be
            this project root or the commit is refused (hard error). Guards
            against a mis-anchored ``cwd`` walking up into the wrong repo.
    """
    if not is_git_repo(cwd):
        return
    if expected_root is not None:
        assert_git_toplevel(cwd, expected_root)
    try:
        commit_files(message, files, allow_failure=False, cwd=cwd)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        joined = ", ".join(files)
        print(
            f"Error: scaffolded {joined} but failed to commit it: {e}\n"
            f"The file was written successfully; commit it manually with "
            f'\'safegit commit -m "{message}" -- {joined}\'.',
            file=sys.stderr,
        )
        sys.exit(1)


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
    status = run("git", ["--no-optional-locks", "status", "--porcelain", "--", *files], cwd=cwd)
    if not status:
        print(skip_message)
        return False
    commit_files(message, files, allow_failure=False, autogenerated=autogenerated, cwd=cwd)
    return True


PREID_ORDER = ("alpha", "beta", "rc", "stable")


def _parse_prerelease_suffix(version):
    """Parse the pre-release suffix from a semver version string.

    Returns (preid, counter) if the version has a suffix like "-alpha.0",
    or (None, None) if no suffix is present.

    Raises VersionError if the suffix format is invalid.
    """
    if "-" not in version:
        return None, None
    _, suffix = version.split("-", 1)
    parts = suffix.rsplit(".", 1)
    if len(parts) != 2:
        raise VersionError(
            f'Invalid pre-release suffix in "{version}": '
            f'expected format "preid.N" (e.g. "alpha.0")'
        )
    preid, counter_str = parts
    try:
        counter = int(counter_str)
    except ValueError:
        raise VersionError(
            f'Invalid pre-release counter in "{version}": '
            f'"{counter_str}" is not an integer'
        )
    return preid, counter


def bump_version(version, bump_type, preid=""):
    """Bump a semver version string by the given type.

    Supported bump types: patch, minor, major, infra, prerelease.

    When preid is set with a standard bump (patch/minor/major), the bumped
    base version gets a pre-release suffix appended: e.g. minor + alpha
    on "0.42.0" produces "0.43.0-alpha.0".

    When bump_type is "prerelease":
    - The current version must have a pre-release suffix.
    - If preid is empty or matches the current preid: increment the counter.
    - If preid is "stable": strip the suffix, return the base version.
    - If preid is higher in the ordering (alpha < beta < rc < stable): promote.
    - If preid is lower: error (cannot demote).

    Infra with preid is a hard error.

    Without preid, the existing behavior is preserved: strip any pre-release
    suffix and bump the base version normally.
    """
    if bump_type == "infra" and preid:
        raise VersionError("infra releases cannot be pre-releases")

    # Parse base version
    base_version = version.split("-", 1)[0]
    parts = base_version.split(".")
    if len(parts) != 3:
        raise VersionError(f'Invalid semver version: "{version}"')
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        raise VersionError(f'Invalid semver version: "{version}"')

    if bump_type == "prerelease":
        current_preid, current_counter = _parse_prerelease_suffix(version)
        if current_preid is None:
            raise VersionError(
                f'Cannot bump prerelease on stable version "{version}": '
                f'no pre-release suffix found'
            )

        if not preid or preid == current_preid:
            # Increment counter
            return f"{major}.{minor}.{patch}-{current_preid}.{current_counter + 1}"

        if preid == "stable":
            return f"{major}.{minor}.{patch}"

        # Validate preid ordering
        if preid not in PREID_ORDER:
            raise VersionError(
                f'Unknown preid "{preid}". '
                f"Must be one of: {', '.join(PREID_ORDER)}"
            )
        current_rank = PREID_ORDER.index(current_preid) if current_preid in PREID_ORDER else -1
        new_rank = PREID_ORDER.index(preid)
        if new_rank <= current_rank:
            raise VersionError(
                f'Cannot demote pre-release from "{current_preid}" to "{preid}"'
            )
        # Promote to new preid
        return f"{major}.{minor}.{patch}-{preid}.0"

    # Standard bumps: patch, minor, major, infra
    if bump_type == "major":
        new_base = f"{major + 1}.0.0"
    elif bump_type == "minor":
        new_base = f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        new_base = f"{major}.{minor}.{patch + 1}"
    elif bump_type == "infra":
        new_base = f"{major}.{minor}.{patch + 1}"
    else:
        raise VersionError(
            f'Invalid bump type: "{bump_type}". '
            f'Use patch, minor, major, infra, or prerelease.'
        )

    if preid:
        if preid not in PREID_ORDER:
            raise VersionError(
                f'Unknown preid "{preid}". '
                f"Must be one of: {', '.join(PREID_ORDER)}"
            )
        return f"{new_base}-{preid}.0"

    return new_base


def is_private_repo():
    """Detect if the current repo is private via GitHub API.

    Returns True if private, False if public, None if detection fails.

    The query goes through ``gh api``, which resolves and applies the
    credential inside its own process.  rlsbl deliberately never asks for the
    token itself: a raw credential on a captured stdout pipe is exactly what
    the observe standard forbids (see :mod:`rlsbl.observe_allowlist`).
    """
    try:
        remote = run("git", ["remote", "get-url", "origin"])
        repo_name = extract_github_repo_from_remote(remote)
        if not repo_name:
            return None
        owner, repo = repo_name.split("/", 1)

        answer = run_gh_unscoped([
            "api", "--method", "GET", f"repos/{owner}/{repo}",
            "--jq", ".private",
        ], timeout=15)
        if effects.unsettled(answer):
            return None
        return answer.strip().lower() == "true"
    except Exception:
        return None


def extract_github_repo_from_remote(remote_url: str) -> str | None:
    """Extract owner/repo from a git remote URL.

    Supports:
    - SCP-style: git@github.com:owner/repo.git, git@gw:owner/repo.git, gp:owner/repo.git
    - HTTPS: https://github.com/owner/repo.git

    Returns "owner/repo" or None if the URL doesn't match.
    """
    if not remote_url:
        return None

    # HTTPS: https://host/owner/repo[.git] -- checked first because the
    # SCP regex would match "https:" as host:path.
    https_match = re.match(r'^https?://[^/]+/(.+/.+)$', remote_url)
    if https_match:
        path = https_match.group(1).removesuffix('.git')
        # Take the last two segments
        parts = path.rstrip('/').split('/')
        if len(parts) >= 2:
            owner = parts[-2]
            repo = parts[-1]
            if owner and repo:
                return f'{owner}/{repo}'
        return None

    # SCP-style: [user@]host:owner/repo[.git]
    scp_match = re.match(r'^(?:[^@/:]+@)?[^@/:]+:(.+/.+)$', remote_url)
    if scp_match:
        path = scp_match.group(1).removesuffix('.git')
        # Validate it looks like owner/repo (exactly two segments)
        parts = path.split('/')
        if len(parts) == 2 and parts[0] and parts[1]:
            return path
        return None

    return None


def get_origin_repo() -> str | None:
    """Get owner/repo for the origin remote of the current git repo.

    Returns None on any error (no remote, not a git repo, unparseable URL).
    """
    try:
        url = run("git", ["remote", "get-url", "origin"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return extract_github_repo_from_remote(url)


def get_github_repo(config: dict | None = None) -> str | None:
    """Resolve the GitHub owner/repo slug from config or the git remote.

    Precedence:
    1. config["github_repo"] if config is provided and the key is set.
    2. get_origin_repo() to parse the origin remote URL.

    Returns "owner/repo" or None if neither source provides a slug.
    """
    if config is not None:
        repo = config.get("github_repo")
        if repo:
            return repo
    return get_origin_repo()



def run_gh(args: list, config: dict | None = None, **kwargs) -> str:
    """Run a ``gh`` CLI command with automatic GH_REPO resolution.

    Resolves the repo slug via get_github_repo(config) and, if found,
    sets GH_REPO in a per-call env dict so ``gh`` targets the correct
    repository.  Does NOT mutate os.environ (critical for thread-safety
    in watch.py's ThreadPoolExecutor).

    Accepts ``timeout``, ``env`` and ``cwd``; anything else is a hard error.
    The call goes through ``effects.gh``, the chokepoint's named entry point
    for the gh CLI, so every GitHub verb in the codebase passes one
    identifiable seam.
    """
    timeout = kwargs.pop("timeout", 120)
    env = kwargs.pop("env", None)
    cwd = kwargs.pop("cwd", None)
    # Validated BEFORE the command runs: a rejected call must not have already
    # created or deleted a Release on the way to raising.
    if kwargs:
        raise TypeError(f"run_gh got unexpected keyword arguments: {sorted(kwargs)}")
    result = effects.gh(
        args,
        repo=get_github_repo(config),
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )
    if effects.unsettled(result):
        return result
    return result.stdout.strip()


def run_gh_unscoped(args: list, *, timeout: int = 120, cwd: str | None = None) -> str:
    """Invoke ``gh`` WITHOUT injecting GH_REPO; return trimmed stdout.

    For the gh calls that must not be scoped to the current project: the
    repo-independent ones (``--version``, ``auth status``) and
    the ones that name their own ``--repo`` explicitly.  They deliberately skip
    :func:`run_gh`, but they still route through ``effects.gh``, so the gh
    family remains one enumerable surface.

    Contract matches :func:`run`: capture, text, ``check=True``, 120s default.
    """
    result = effects.gh(
        args,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=True,
    )
    if effects.unsettled(result):
        return result
    return result.stdout.strip()


def read_go_module_path(project_dir: str) -> str | None:
    """Read the module path from go.mod.

    Returns None if go.mod does not exist or cannot be parsed.
    """
    go_mod = os.path.join(project_dir, "go.mod")
    if not os.path.isfile(go_mod):
        return None
    try:
        with open(go_mod, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("module "):
                    return line[len("module "):].strip()
    except (OSError, UnicodeDecodeError):
        pass
    return None
