"""Release retry command that re-creates a GitHub Release to re-trigger CI/CD workflows.

When a GitHub Release exists but CI/CD never ran (e.g., GitHub Actions outage),
this command deletes the release and re-creates it with the correct release notes.
This fires a new ``release: published`` event, re-triggering the Publish workflow.
It also re-uploads release assets if configured, and falls back to
``gh workflow run`` if the event still doesn't trigger workflows.
"""

import os
import sys
import time

from ..config import read_project_config
from ..targets import TARGETS, detect_targets
from ..utils import check_gh_auth, check_gh_installed, extract_changelog_entry, run
from ..workspace import find_workspace_root, resolve_project
from .release import upload_release_assets
from .watch import poll_runs, run_cmd as watch_run_cmd


def _find_dispatch_workflows():
    """Scan .github/workflows/ for YAML files that contain ``workflow_dispatch``.

    Returns a list of filenames (not full paths) that support manual dispatch.
    """
    workflow_dir = os.path.join(".github", "workflows")
    if not os.path.isdir(workflow_dir):
        return []
    results = []
    for filename in sorted(os.listdir(workflow_dir)):
        if not (filename.endswith(".yml") or filename.endswith(".yaml")):
            continue
        filepath = os.path.join(workflow_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            if "workflow_dispatch" in content:
                results.append(filename)
        except OSError:
            continue
    return results


def run_cmd(args, flags):
    """Re-create a GitHub Release to re-trigger CI/CD workflows.

    Deletes the existing GitHub Release for the given (or current) version
    and re-creates it with the same changelog notes. This fires a new
    ``release: published`` event. Re-uploads release assets if configured.
    Falls back to ``gh workflow run`` if no CI runs appear.

    Args:
        args: optional list where args[0] is the version to retry.
        flags: dict with keys ``dry-run``, ``yes``, ``quiet``, ``watch``.
    """
    dry_run = flags.get("dry-run", False)
    quiet = flags.get("quiet", False)
    watch = flags.get("watch", False)

    def log(msg):
        if not quiet:
            print(msg)

    # Prerequisites
    if not check_gh_installed():
        print("Error: gh CLI is not installed.", file=sys.stderr)
        sys.exit(1)
    if not check_gh_auth():
        print("Error: gh CLI is not authenticated.", file=sys.stderr)
        sys.exit(1)

    # Detect monorepo context
    monorepo_name = None
    monorepo_project_path = None
    monorepo_root = find_workspace_root(".")
    if monorepo_root:
        project = resolve_project(monorepo_root, ".")
        if project is not None:
            monorepo_name = project["name"]
            monorepo_project_path = project["path"]
            os.chdir(monorepo_root)

    # Version directory: project subdir in monorepo, repo root otherwise
    version_dir = monorepo_project_path if monorepo_name else "."

    # Detect primary target
    entries = detect_targets(version_dir)
    if not entries:
        print("Error: no package.json, pyproject.toml, or go.mod found.", file=sys.stderr)
        sys.exit(1)
    primary = entries[0]
    target = TARGETS[primary.name]

    # Resolve version
    if args:
        raw_version = args[0]
    else:
        raw_version = target.read_version(primary.path)

    # Normalize: strip leading "v" for changelog lookup
    version = raw_version.lstrip("v")

    # Build the tag: monorepo format or standalone
    if monorepo_name:
        tag = target.monorepo_tag_format(monorepo_name, version, path=monorepo_project_path)
    else:
        tag = target.tag_format(version)

    # Verify the GitHub Release exists
    try:
        run("gh", ["release", "view", tag])
    except Exception:
        print(f"Error: no GitHub Release found for {tag}.", file=sys.stderr)
        sys.exit(1)

    # Extract changelog entry
    changelog_path = os.path.join(version_dir, "CHANGELOG.md")
    if not os.path.exists(changelog_path):
        print("Error: CHANGELOG.md not found.", file=sys.stderr)
        sys.exit(1)

    changelog_entry = extract_changelog_entry(changelog_path, version)
    if not changelog_entry:
        print(
            f"Error: no changelog entry found for version {version} in CHANGELOG.md.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Get commit SHA for the tag (needed for watch later)
    commit_sha = run("git", ["rev-list", "-1", tag])

    if dry_run:
        log(f"Would delete and re-create GitHub Release for {tag}")
        log(f"Tag commit: {commit_sha[:12]}")
        log(f"Changelog entry:\n{changelog_entry}")
        return

    # Confirmation prompt
    if not flags.get("yes"):
        log(f"Will delete and re-create GitHub Release for {tag}.")
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            sys.exit(1)

    # Delete the existing GitHub Release
    release_deleted = False
    try:
        run("gh", ["release", "delete", tag, "--yes"])
        release_deleted = True
        log(f"Deleted GitHub Release: {tag}")
    except Exception as e:
        print(f"Error: failed to delete GitHub Release for {tag}: {e}", file=sys.stderr)
        sys.exit(1)

    # Re-create the GitHub Release
    notes_file = f".rlsbl-notes-{int(time.time() * 1000)}.tmp"
    writing_file = notes_file + ".writing"
    try:
        with open(writing_file, "w", encoding="utf-8") as f:
            f.write(changelog_entry)
        os.rename(writing_file, notes_file)
        run("gh", ["release", "create", tag, "--title", tag, "--notes-file", notes_file])
        log(f"Created GitHub Release: {tag}")
    except Exception as e:
        print(
            f"Warning: GitHub Release for {tag} was deleted but re-creation failed: {e}",
            file=sys.stderr,
        )
        print(
            f"You can manually re-create it with:\n"
            f"  gh release create {tag} --title {tag} --notes-file <notes-file>",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        for tmp in (notes_file, writing_file):
            if os.path.exists(tmp):
                os.unlink(tmp)

    # Re-upload release assets
    upload_release_assets(tag, version_dir, version, log, flags)

    # Poll for workflow runs
    runs = poll_runs(commit_sha)

    if not runs:
        # Dispatch fallback: trigger workflows that support workflow_dispatch
        dispatch_files = _find_dispatch_workflows()
        if dispatch_files:
            log("No CI runs found after polling. Dispatching workflows manually...")
            for filename in dispatch_files:
                try:
                    run("gh", ["workflow", "run", filename, "--ref", tag])
                    log(f"  Dispatched: {filename}")
                except Exception as e:
                    print(f"  Warning: failed to dispatch {filename}: {e}", file=sys.stderr)
        else:
            log("No CI runs found and no workflow_dispatch workflows available.")

    # Watch CI or print hint
    if watch:
        watch_run_cmd(None, [commit_sha], {})
    else:
        log(f"Watch CI: rlsbl watch {commit_sha}")
