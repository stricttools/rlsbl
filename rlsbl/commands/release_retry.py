"""Release retry command that re-creates a GitHub Release to re-trigger CI/CD workflows.

When a GitHub Release exists but CI/CD never ran (e.g., GitHub Actions outage),
this command deletes the release and re-creates it with the correct release notes.
This fires a new ``release: published`` event, re-triggering the Publish workflow.
It also re-uploads release assets if configured, and dispatches all workflows
listed in ``retry.toml`` via ``gh workflow run``.

The command is file-driven: it reads ``.rlsbl/releases/retry.toml`` for
configuration (version, workflows, ci_ref, assets). If the file does not
exist, it auto-scaffolds one from project state and proceeds.
"""

import os
import subprocess
import sys
import time

import tomlkit

from ..config import read_project_config
from ..release_file import RetryConfig, get_retry_file_path, read_retry_file
from ..targets import TARGETS, detect_targets
from ..utils import check_gh_auth, check_gh_installed, extract_changelog_entry, run
from ..workspace import find_workspace_root, resolve_project
from .release import upload_release_assets
from .watch import run_cmd as watch_run_cmd


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


def _has_assets_config():
    """Check whether any target has assets enabled in .rlsbl/config.json."""
    config = read_project_config()
    publish = config.get("publish", {})
    if not isinstance(publish, dict):
        return False
    for _target_name, target_cfg in publish.items():
        if isinstance(target_cfg, dict) and target_cfg.get("assets"):
            return True
    return False


def _scaffold_retry_file(retry_path, version_dir, target, monorepo_name, monorepo_project_path, log):
    """Auto-scaffold retry.toml from project state.

    Returns the RetryConfig after writing the file.
    """
    # Auto-detect version
    entries = detect_targets(version_dir)
    if not entries:
        print("Error: no package.json, pyproject.toml, or go.mod found.", file=sys.stderr)
        sys.exit(1)
    primary = entries[0]
    tgt = TARGETS[primary.name]
    raw_version = tgt.read_version(primary.path)
    version = raw_version.lstrip("v")

    # Build tag
    if monorepo_name:
        tag = tgt.monorepo_tag_format(monorepo_name, version, path=monorepo_project_path)
    else:
        tag = tgt.tag_format(version)

    # Auto-detect dispatchable workflows
    workflows = _find_dispatch_workflows()

    # Assets from config
    assets = _has_assets_config()

    # Write retry.toml
    doc = tomlkit.document()
    doc.add("version", version)
    doc.add("workflows", workflows)
    doc.add("ci_ref", tag)
    doc.add("assets", assets)

    os.makedirs(os.path.dirname(retry_path), exist_ok=True)
    tmp_path = retry_path + ".writing"
    with open(tmp_path, "w", encoding="utf-8") as f:
        tomlkit.dump(doc, f)
    os.rename(tmp_path, retry_path)

    log(f"Auto-scaffolded retry file: {retry_path}")
    with open(retry_path, "r", encoding="utf-8") as f:
        log(f.read().rstrip())

    return read_retry_file(retry_path)


def _cleanup_retry_file(retry_path, log):
    """Delete retry.toml via saferm after successful retry."""
    try:
        subprocess.run(
            ["saferm", "delete", "--description", "Retry completed successfully", retry_path],
            check=True,
            capture_output=True,
            text=True,
        )
        log(f"Cleaned up: {retry_path}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        # Non-fatal: warn but don't fail the retry
        print(f"Warning: failed to clean up {retry_path}: {e}", file=sys.stderr)


def run_cmd(retry_config, flags):
    """Re-create a GitHub Release to re-trigger CI/CD workflows.

    Deletes the existing GitHub Release for the configured version
    and re-creates it with the same changelog notes. This fires a new
    ``release: published`` event. Re-uploads release assets if configured.
    Dispatches all workflows listed in retry config via ``gh workflow run``.

    Args:
        retry_config: RetryConfig instance, or None to auto-scaffold.
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

    # Detect primary target (needed for tag format)
    entries = detect_targets(version_dir)
    if not entries:
        print("Error: no package.json, pyproject.toml, or go.mod found.", file=sys.stderr)
        sys.exit(1)
    primary = entries[0]
    target = TARGETS[primary.name]

    # Auto-scaffold retry.toml if not provided
    retry_path = get_retry_file_path(version_dir)
    if retry_config is None:
        if os.path.exists(retry_path):
            # File exists but wasn't read by the caller -- read it now
            try:
                retry_config = read_retry_file(retry_path)
            except ValueError as e:
                print(f"Error in retry file: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            retry_config = _scaffold_retry_file(
                retry_path, version_dir, target,
                monorepo_name, monorepo_project_path, log,
            )

    # Use config values
    version = retry_config.version

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
    try:
        run("gh", ["release", "delete", tag, "--yes"])
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

    # Re-upload release assets (gated by config.assets)
    if retry_config.assets:
        upload_release_assets(tag, version_dir, version, log, flags)

    # Dispatch all configured workflows
    if retry_config.workflows:
        log("Dispatching workflows...")
        for filename in retry_config.workflows:
            try:
                run("gh", ["workflow", "run", filename, "--ref", retry_config.ci_ref])
                log(f"  Dispatched: {filename}")
            except Exception as e:
                print(f"  Warning: failed to dispatch {filename}: {e}", file=sys.stderr)

    # Clean up retry.toml after successful retry
    if os.path.exists(retry_path):
        _cleanup_retry_file(retry_path, log)

    # Watch CI or print hint
    if watch:
        watch_run_cmd(None, [commit_sha], {})
    else:
        log(f"Watch CI: rlsbl watch {commit_sha}")
