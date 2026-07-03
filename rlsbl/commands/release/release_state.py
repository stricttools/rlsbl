"""Release state file: JSON persistence for idempotent release flow, tracking which steps completed so failed releases can resume from the last successful step.

Tracks which release steps have completed so that a failed release can be
resumed without re-executing already-done work.

Canonical step list
-------------------
``RELEASE_STEPS`` is the single ordered source of truth for every step a
release records.  The run guard message, ``save_step`` validation, resume
skip logic, and auto-clear completeness checks all derive from it — never
hardcode step counts or step-name lists elsewhere.

Two marker kinds are recorded in the state file:

- **Success markers** (``completed_steps`` list): the step finished (or was
  not applicable and is provably done).  These gate resume-skip.
- **Failure markers** (``failed_steps`` dict of step -> message): the step
  ran and failed.  They do NOT gate resume-skip (a resume re-attempts the
  step) — they feed the completion summary.  Steps in ``FATAL_STEPS`` abort
  the release when they fail (state preserved, resumable); non-fatal steps
  record the failure and let the release complete.

A state file is *provably complete* when every canonical step has a success
or failure marker and no fatal step failed (``is_state_complete``).  Only
then may the success path clear it.

State file location
-------------------
- Standalone projects and implicit-monorepo packages:
  ``<project_dir>/.rlsbl/releases/in-progress.json``
- Releasable releases (explicit monorepo mode): the state belongs to the
  releasable, not the representative member package, and lives at
  ``<workspace_root>/.rlsbl-monorepo/releasables/<name>/releases/in-progress.json``.

ALL derivations of the state path must go through :func:`get_state_path`
(with the releasable dir from :func:`resolve_releasable_dir` when in a
monorepo) so the run guard, resume CLI, executor, unexpected-files
whitelist, and scrub agree on a single location.

The file is written at the start of the mutating phase, deleted on
success, and left in place on failure.
"""

import json
import os
import tempfile


STATE_FILENAME = "in-progress.json"
SCRUB_RESULT_FILENAME = "scrub-result.json"

# Ordered steps of the mutating phase (rolled back or resumed on failure).
MUTATING_STEPS = (
    "VERSION_BUMPED",
    "COMMITTED",
    "CHANGELOG_FINALIZED",
    "RELEASE_FILE_FINALIZED",
    "TAGGED",
    "PUSHED",
    "GITHUB_RELEASE",
)

# Ordered steps of the post-release phase (after the GitHub Release).
POST_RELEASE_STEPS = (
    "ASSETS_UPLOADED",
    "PIPELINES_PUBLISHED",
    "DEPLOYED",
    "POST_HOOKS_RUN",
    "SNAPSHOT_REGENERATED",
)

# The canonical ordered list of ALL release steps.
RELEASE_STEPS = MUTATING_STEPS + POST_RELEASE_STEPS

# Steps whose failure aborts the release (state preserved, resumable).
# Deploy, post-release hooks, and snapshot regeneration are non-fatal:
# their failures are recorded and loudly reported, but the release
# completes and the state file is cleared.
FATAL_STEPS = frozenset(MUTATING_STEPS) | {
    "ASSETS_UPLOADED",
    "PIPELINES_PUBLISHED",
}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def get_state_dir(project_dir: str, *, releasable_dir: str | None = None) -> str:
    """Return the directory holding release state files.

    ``releasable_dir`` is the releasable's state directory
    (``.rlsbl-monorepo/releasables/<name>/``); when given, state lives in
    its ``releases/`` subdirectory instead of the project's
    ``.rlsbl/releases/``. Delegates to the single releases-dir derivation
    in :mod:`rlsbl.release_file` (shared with the release-file family:
    unreleased.toml, v{x}.toml, unreleased.md, v{x}.md).
    """
    from ...release_file import get_releases_dir

    return get_releases_dir(project_dir, releasable_dir=releasable_dir)


def get_state_path(project_dir: str, *, releasable_dir: str | None = None) -> str:
    """Return the path to the release state file (in-progress.json).

    This is the ONLY function that may derive the state file location.
    Pass ``releasable_dir`` (from :func:`resolve_releasable_dir` or an
    already-resolved releasable config dir) for releasable releases.
    """
    return os.path.join(
        get_state_dir(project_dir, releasable_dir=releasable_dir), STATE_FILENAME,
    )


def get_scrub_result_path(project_dir: str, *, releasable_dir: str | None = None) -> str:
    """Return the path to the scrub-result.json file (same home as the
    release state file)."""
    return os.path.join(
        get_state_dir(project_dir, releasable_dir=releasable_dir),
        SCRUB_RESULT_FILENAME,
    )


def resolve_releasable_dir(project_dir, workspace_root) -> str | None:
    """Resolve the releasable state dir for a project, or None.

    Returns ``<workspace_root>/.rlsbl-monorepo/releasables/<name>/`` when
    ``project_dir`` is a member of a releasable in an explicit-mode
    workspace; None for standalone projects, implicit-mode packages,
    non-member projects, or when ``workspace_root`` is None.
    """
    if workspace_root is None:
        return None
    from pathlib import Path

    from ...context import resolve_releasable_config_dir

    return resolve_releasable_config_dir(
        Path(str(project_dir)), Path(str(workspace_root)),
    )


class StateResolutionError(Exception):
    """Raised when the resume source cannot be resolved unambiguously."""


def resolve_resume_source(workspace_root, cwd=".") -> tuple[str, str]:
    """Resolve ``(project_dir, state_path)`` for ``rlsbl release resume``.

    - Standalone (``workspace_root`` is None): state under
      ``<cwd>/.rlsbl/releases/``.
    - Inside a member package dir: the releasable-aware state path.  If
      in-flight state exists only at the legacy per-project location, a
      :class:`StateResolutionError` with a migration hint is raised (never
      silently ignore pre-existing in-flight state).
    - At the workspace root: finds the single releasable whose state file
      exists and resolves the representative member (from the saved
      ``monorepo_name``, falling back to the first member).  Errors on
      zero or multiple in-flight releasables.
    """
    if workspace_root is None:
        return cwd, get_state_path(cwd)

    from ...workspace import load_workspace, members_of, resolve_project

    workspace_root = str(workspace_root)
    project = resolve_project(workspace_root, cwd)
    if project is None:
        # Not inside a member package (e.g. at the workspace root):
        # look for releasable in-flight state.
        rel_states = find_releasable_state_files(workspace_root)
        if not rel_states:
            raise StateResolutionError(
                "cannot resume from monorepo root. "
                "cd to the package directory where the release was started."
            )
        if len(rel_states) > 1:
            names = ", ".join(name for name, _ in rel_states)
            raise StateResolutionError(
                f"multiple releasables have in-progress releases: {names}. "
                f"cd into a member package directory of the releasable "
                f"you want to resume."
            )
        rel_name, state_path = rel_states[0]
        saved = load_release_state(state_path) or {}
        projects = load_workspace(workspace_root)
        rep_name = saved.get("monorepo_name")
        rep = None
        if rep_name:
            rep = next((p for p in projects if p["name"] == rep_name), None)
        if rep is None:
            members = members_of(rel_name, projects)
            rep = members[0] if members else None
        if rep is None:
            raise StateResolutionError(
                f"cannot resolve a member package for releasable {rel_name!r}."
            )
        return os.path.join(workspace_root, rep["path"]), state_path

    project_dir = os.path.join(workspace_root, project["path"])
    rel_dir = resolve_releasable_dir(project_dir, workspace_root)
    state_path = get_state_path(project_dir, releasable_dir=rel_dir)
    if rel_dir is not None and not os.path.isfile(state_path):
        legacy_path = get_state_path(project_dir)
        if os.path.isfile(legacy_path):
            raise StateResolutionError(
                f"found in-progress release state at the legacy location "
                f"{legacy_path}. Releasable release state now lives at "
                f"{state_path}. Move the file to the new location and "
                f"re-run `rlsbl release resume`."
            )
    return project_dir, state_path


def find_releasable_state_files(workspace_root) -> list[tuple[str, str]]:
    """Scan all releasables in a workspace for in-progress release state.

    Returns a sorted list of ``(releasable_name, state_path)`` tuples for
    every releasable whose ``releases/in-progress.json`` exists.  Used by
    the resume CLI when invoked from the workspace root.
    """
    from ...workspace import RELEASABLES_DIR, WORKSPACE_DIR

    base = os.path.join(str(workspace_root), WORKSPACE_DIR, RELEASABLES_DIR)
    if not os.path.isdir(base):
        return []
    results = []
    for name in sorted(os.listdir(base)):
        rel_dir = os.path.join(base, name)
        if not os.path.isdir(rel_dir):
            continue
        state_path = get_state_path("", releasable_dir=rel_dir)
        if os.path.isfile(state_path):
            results.append((name, state_path))
    return results


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def save_release_state(state_path: str, state_dict: dict) -> None:
    """Atomically write the release state dict to disk (tmp + os.replace)."""
    parent = os.path.dirname(state_path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".in-progress.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state_dict, f, indent=2)
            f.write("\n")
        os.replace(tmp, state_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_release_state(state_path: str) -> dict | None:
    """Read and parse the release state file.  Returns None if missing."""
    if not os.path.isfile(state_path):
        return None
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _require_known_step(step_name: str) -> None:
    if step_name not in RELEASE_STEPS:
        raise ValueError(
            f"unknown release step {step_name!r}; "
            f"must be one of: {', '.join(RELEASE_STEPS)}"
        )


def save_step(state_path: str, step_name: str) -> None:
    """Record a successful step: load, append to completed_steps, save.

    Also clears any failure marker for the step (a resume that re-attempts
    a previously-failed step replaces the failure with success).
    Raises ValueError for step names not in :data:`RELEASE_STEPS`.
    """
    _require_known_step(step_name)
    state = load_release_state(state_path)
    if state is None:
        state = {"completed_steps": []}
    steps = state.setdefault("completed_steps", [])
    if step_name not in steps:
        steps.append(step_name)
    failed = state.get("failed_steps")
    if failed and step_name in failed:
        del failed[step_name]
    save_release_state(state_path, state)


def save_step_failure(state_path: str, step_name: str, message: str) -> None:
    """Record a step failure marker with a human-readable message.

    Failure markers do NOT gate resume-skip; they feed the completion
    summary.  A failure replaces any prior success marker for the step.
    Raises ValueError for step names not in :data:`RELEASE_STEPS`.
    """
    _require_known_step(step_name)
    state = load_release_state(state_path)
    if state is None:
        state = {"completed_steps": []}
    failed = state.setdefault("failed_steps", {})
    failed[step_name] = message
    steps = state.get("completed_steps")
    if steps and step_name in steps:
        steps.remove(step_name)
    save_release_state(state_path, state)


def clear_release_state(state_path: str) -> None:
    """Delete the state file and its parent dir if empty (no-op if already absent).

    This is an unconditional removal — used by the success epilogue (after
    :func:`is_state_complete` verification), rollback paths (state is
    useless after a local rollback), and PR-mode handoff (only the local
    mutating phase is tracked; publishing happens in CI).
    """
    try:
        os.unlink(state_path)
    except FileNotFoundError:
        pass
    # Remove parent directory if it's now empty (best-effort).
    # The state file may have created .rlsbl/releases/ which would be
    # left as an untracked directory after git reset --hard.
    try:
        parent = os.path.dirname(state_path)
        if parent and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# State inspection (all derive from RELEASE_STEPS)
# ---------------------------------------------------------------------------


def get_failed_steps(state: dict) -> dict[str, str]:
    """Return the failure markers dict (step -> message) from a state dict."""
    return dict(state.get("failed_steps") or {})


def get_missing_steps(state: dict) -> list[str]:
    """Return canonical steps that have neither a success nor a failure
    marker, in canonical order."""
    completed = set(state.get("completed_steps") or [])
    failed = set(state.get("failed_steps") or {})
    return [s for s in RELEASE_STEPS if s not in completed and s not in failed]


def has_fatal_failure(state: dict) -> bool:
    """Return True if any fatal step has a failure marker."""
    return any(s in FATAL_STEPS for s in (state.get("failed_steps") or {}))


def is_state_complete(state: dict) -> bool:
    """Return True if the state is provably complete: every canonical step
    has a success-or-failure marker AND no fatal step failed."""
    return not get_missing_steps(state) and not has_fatal_failure(state)
