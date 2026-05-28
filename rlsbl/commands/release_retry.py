"""Release retry command that dispatches CI/CD workflows for an existing GitHub Release.

When a GitHub Release exists but CI/CD never ran (e.g., GitHub Actions outage),
this command dispatches all workflows listed in ``retry.toml`` via
``gh workflow run``. The GitHub Release itself is left untouched.

The command is file-driven: it reads ``.rlsbl/releases/retry.toml`` for
configuration (version, dispatch, ref). If the file does not exist, it
auto-scaffolds one from project state and proceeds.
"""

import json
import os
import re
import subprocess
import sys
import time

import tomlkit

from ..release_file import get_retry_file_path, read_retry_file
from ..targets import TARGETS, detect_targets
from ..utils import check_gh_auth, check_gh_installed, run
from ..workspace import find_workspace_root, resolve_project
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
    dispatch = _find_dispatch_workflows()

    # Write retry.toml
    doc = tomlkit.document()
    doc.add("version", version)
    doc.add("dispatch", dispatch)
    doc.add(tomlkit.comment("Git ref to dispatch CI against. Examples: v1.2.3 (tag), main (branch)"))
    doc.add("ref", "")

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


def run_cmd(retry_config, flags, project_root=None):
    """Dispatch CI/CD workflows for an existing GitHub Release.

    Verifies the GitHub Release exists for the configured version, then
    dispatches all workflows listed in the retry config via
    ``gh workflow run``. The release itself is not modified.

    Args:
        retry_config: RetryConfig instance, or None to auto-scaffold.
        flags: dict with keys ``dry-run``, ``yes``, ``quiet``, ``watch``.
        project_root: Path to the project root directory, or None for cwd.
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
    start_path = str(project_root) if project_root else "."
    monorepo_root = find_workspace_root(start_path)
    if monorepo_root:
        project = resolve_project(monorepo_root, start_path)
        if project is not None:
            monorepo_name = project["name"]
            monorepo_project_path = project["path"]

    # Version directory: absolute path to the project
    if monorepo_name:
        version_dir = os.path.join(monorepo_root, monorepo_project_path)
    else:
        version_dir = start_path

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
            try:
                retry_config = _scaffold_retry_file(
                    retry_path, version_dir, target,
                    monorepo_name, monorepo_project_path, log,
                )
            except ValueError as e:
                print(f"Error in retry file: {e}", file=sys.stderr)
                sys.exit(1)

    # Use config values
    version = retry_config.version
    dispatch = retry_config.dispatch

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

    # Get commit SHA for the tag (needed for watch later)
    commit_sha = run("git", ["rev-list", "-1", tag])

    if dry_run:
        log(f"Would dispatch {len(dispatch)} workflow(s) for {tag}")
        log(f"Tag commit: {commit_sha[:12]}")
        for filename in dispatch:
            log(f"  {filename}")
        return

    # Confirmation prompt
    if not flags.get("yes"):
        log(f"Will dispatch {len(dispatch)} workflow(s) for {tag}. Continue? [y/N]")
        try:
            answer = input("").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            sys.exit(1)

    # Dispatch all configured workflows and capture run IDs
    log("Dispatching workflows...")
    collected_run_ids = []
    for filename in dispatch:
        try:
            output = run("gh", ["workflow", "run", filename, "--ref", retry_config.ref])
            log(f"  Dispatched: {filename}")
            # gh workflow run may return a URL like https://github.com/owner/repo/actions/runs/12345
            match = re.search(r'/actions/runs/(\d+)', output)
            if match:
                collected_run_ids.append(match.group(1))
            else:
                # Fallback: poll for the most recent run of this workflow
                time.sleep(2)  # brief delay for GitHub to register the run
                try:
                    poll_output = run("gh", ["run", "list", "--workflow", filename, "--limit", "1", "--json", "databaseId"])
                    poll_data = json.loads(poll_output)
                    if poll_data:
                        collected_run_ids.append(str(poll_data[0]["databaseId"]))
                except Exception:
                    pass  # best-effort -- if polling fails, we fall back to SHA-based watching
        except Exception as e:
            print(f"  Warning: failed to dispatch {filename}: {e}", file=sys.stderr)

    # Clean up retry.toml after successful retry
    if os.path.exists(retry_path):
        _cleanup_retry_file(retry_path, log)

    # Watch CI or print hint
    if watch:
        if collected_run_ids:
            watch_run_cmd(None, [], {"run-id": collected_run_ids})
        else:
            watch_run_cmd(None, [commit_sha], {})
    else:
        if collected_run_ids:
            log(f"Watch CI: rlsbl watch --run-id {' --run-id '.join(collected_run_ids)}")
        else:
            log(f"Watch CI: rlsbl watch {commit_sha}")
