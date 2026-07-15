"""Rollback helpers for cleaning up after a failed release: deleting tags, reverting version bump commits, and removing GitHub Releases."""

import os

from ...utils import run


def _is_tracked(project_dir: str, path: str) -> bool:
    """Return True if ``path`` is tracked in git at the current HEAD/index.

    Uses ``git ls-files --error-unmatch`` which exits non-zero for paths
    that are not tracked. Any failure (untracked path, not a git repo, git
    unavailable) is treated as "not tracked" so cleanup falls through to its
    normal orphan-removal behavior.
    """
    try:
        run("git", ["ls-files", "--error-unmatch", path],
            cwd=project_dir, timeout=30)
        return True
    except Exception:
        return False


def _cleanup_release_artifacts(project_dir: str, version: str, *,
                               changes_dir: str | None = None,
                               releases_dir: str | None = None) -> None:
    """Best-effort removal of generated files that become orphaned after rollback.

    After `git reset --hard` reverts the release commits, files created during
    finalization (renamed JSONL, per-version markdown, renamed release TOML) are
    left as untracked because they never existed in the pre-release history.
    Removing them prevents a dirty working tree that blocks the next attempt.

    Tracked-file guard: a candidate that is TRACKED in git at the post-reset
    HEAD is left alone. This is the re-release case -- an earlier partial
    attempt committed finalize files (e.g. ``{version}.jsonl``); after
    ``git reset --hard`` restores them as tracked files, deleting them would
    leave `` D`` (deleted-from-index) entries that dirty the tree and block a
    retry with ``--no-allow-dirty``. Only genuinely orphaned (untracked)
    artifacts are removed.

    ``changes_dir`` overrides the default per-project ``.rlsbl/changes/``
    location -- in explicit releasable mode the finalized JSONL and its
    per-version .md live in the releasable's changes dir. ``releases_dir``
    overrides the default per-project ``.rlsbl/releases/`` location the
    same way -- in explicit releasable mode the release-file finalization
    archives v{version}.toml/.md under the releasable's own releases dir.
    """
    try:
        resolved_changes = changes_dir or os.path.join(project_dir, ".rlsbl", "changes")
        resolved_releases = releases_dir or os.path.join(project_dir, ".rlsbl", "releases")
        candidates = [
            os.path.join(resolved_changes, f"{version}.jsonl"),
            os.path.join(resolved_changes, f"{version}.md"),
            os.path.join(resolved_releases, f"v{version}.toml"),
            os.path.join(resolved_releases, f"v{version}.md"),
        ]
        for path in candidates:
            if os.path.exists(path):
                # Tracked at post-reset HEAD (re-release case): leave it alone.
                # Deleting it would create a ` D` index entry that blocks retry.
                if _is_tracked(project_dir, path):
                    continue
                # Released JSONL files are chmod 444; make writable before unlinking
                os.chmod(path, 0o644)
                os.unlink(path)
    except Exception:
        pass  # Best-effort: never mask the original error
