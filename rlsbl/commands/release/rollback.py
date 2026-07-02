"""Rollback helpers for cleaning up after a failed release."""

import os


def _cleanup_release_artifacts(project_dir: str, version: str, *,
                               changes_dir: str | None = None) -> None:
    """Best-effort removal of generated files that become orphaned after rollback.

    After `git reset --hard` reverts the release commits, files created during
    finalization (renamed JSONL, per-version markdown, renamed release TOML) are
    left as untracked because they never existed in the pre-release history.
    Removing them prevents a dirty working tree that blocks the next attempt.

    ``changes_dir`` overrides the default per-project ``.rlsbl/changes/``
    location -- in explicit releasable mode the finalized JSONL and its
    per-version .md live in the releasable's changes dir. The release TOML
    candidates stay per-project: the release-file finalization archives at
    ``<project>/.rlsbl/releases/`` regardless of mode.
    """
    try:
        resolved_changes = changes_dir or os.path.join(project_dir, ".rlsbl", "changes")
        candidates = [
            os.path.join(resolved_changes, f"{version}.jsonl"),
            os.path.join(resolved_changes, f"{version}.md"),
            os.path.join(project_dir, ".rlsbl", "releases", f"v{version}.toml"),
            os.path.join(project_dir, ".rlsbl", "releases", f"v{version}.md"),
        ]
        for path in candidates:
            if os.path.exists(path):
                # Released JSONL files are chmod 444; make writable before unlinking
                os.chmod(path, 0o644)
                os.unlink(path)
    except Exception:
        pass  # Best-effort: never mask the original error
