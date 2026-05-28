"""Undo command that reverts the last release by deleting the GitHub Release, removing the git tag, and reverting the version bump commit."""

import os
import re
import sys

from ..changelog.files import get_changes_dir, unfinalize_version
from ..changelog.generate import generate_changelog
from ..targets import TARGETS, detect_targets
from ..utils import run, check_gh_installed, check_gh_auth, get_push_timeout, get_current_branch, push_if_needed, is_clean_tree
from ..workspace import find_workspace_root, resolve_project

# Status constants for step results
OK = "OK"
FAILED = "FAILED"
SKIPPED = "SKIPPED"


def _print_summary(results):
    """Print a summary table of step results. Only called when at least one step failed."""
    # Calculate column widths
    step_width = max(len(r[0]) for r in results)
    status_width = max(len(r[1]) for r in results)

    header = f"{'Step':<{step_width}}  {'Status':<{status_width}}  Remediation"
    print(f"\n{header}")
    print("-" * len(header))
    for step_name, status, remediation in results:
        print(f"{step_name:<{step_width}}  {status:<{status_width}}  {remediation}")


def run_cmd(registry, args, flags, project_root=None):
    if not check_gh_installed():
        print("Error: gh CLI is not installed.", file=sys.stderr)
        sys.exit(1)
    if not check_gh_auth():
        print("Error: gh CLI is not authenticated.", file=sys.stderr)
        sys.exit(1)

    if not is_clean_tree():
        print("Error: working tree is not clean. Commit your changes first.", file=sys.stderr)
        sys.exit(1)

    # Monorepo detection
    monorepo_name = None
    monorepo_project_path = None
    start_path = str(project_root) if project_root else "."
    ws_root = find_workspace_root(start_path)
    if ws_root:
        project = resolve_project(ws_root, start_path)
        if project is None:
            print("Error: current directory is inside a monorepo but not inside any project.", file=sys.stderr)
            sys.exit(1)
        monorepo_name = project["name"]
        monorepo_project_path = project["path"]
        os.chdir(ws_root)

    # Find the latest tag (scoped to project in monorepo mode)
    if monorepo_name:
        target_entries = detect_targets(monorepo_project_path)
        if target_entries:
            target = TARGETS[target_entries[0].name]
            match_pattern = target.monorepo_tag_glob(monorepo_name, path=monorepo_project_path)
        else:
            match_pattern = f"{monorepo_name}@v*"
    else:
        match_pattern = "v*"
    try:
        tag = run("git", ["describe", "--tags", "--abbrev=0", "--match", match_pattern])
    except Exception:
        print("Error: no tags found. Nothing to undo.", file=sys.stderr)
        sys.exit(1)

    print(f"This will undo release {tag}:")
    print(f"  - Delete git tag {tag} (local + remote)")
    print(f"  - Revert the version bump commit")
    print(f"  - Delete the GitHub Release for {tag}")

    if not flags.get("yes"):
        try:
            answer = input("\nThis is destructive. Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    # Collect (step_name, status, remediation) for each step
    results = []

    # Delete GitHub Release
    try:
        run("gh", ["release", "delete", tag, "--yes"])
        results.append(("Delete GitHub Release", OK, "-"))
    except Exception:
        results.append(("Delete GitHub Release", FAILED, f"gh release delete {tag} --yes"))

    # Delete remote tag (marked as release-authorized: undo is part of the
    # release flow, so the pre-push hook shouldn't warn about a manual push).
    try:
        undo_push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
        run("git", ["push", "origin", f":{tag}"], timeout=get_push_timeout(project_root=project_root), env=undo_push_env)
        results.append(("Delete remote tag", OK, "-"))
    except Exception:
        results.append(("Delete remote tag", FAILED, f"git push origin :{tag}"))

    # Delete local tag
    try:
        run("git", ["tag", "-d", tag])
        results.append(("Delete local tag", OK, "-"))
    except Exception:
        results.append(("Delete local tag", FAILED, f"git tag -d {tag}"))

    # Revert release commits (should be HEAD, or HEAD + HEAD~1 for two-commit pattern)
    # In monorepo mode, commit message is "<project>: release v<version>"
    # In standalone mode, commit message is the tag string (e.g., "v1.2.3")
    if monorepo_name:
        # Extract version from tag: handles both name@v1.2.3 and path/v1.2.3
        _version_match = re.search(r"v(\d+\.\d+\.\d+)$", tag)
        version_part = f"v{_version_match.group(1)}" if _version_match else tag
        expected_msg = f"{monorepo_name}: release {version_part}"
    else:
        expected_msg = tag

    # Extract the bare version string (without "v" prefix) for changelog operations
    if monorepo_name:
        bare_version = version_part.lstrip("v")
    else:
        bare_version = tag.lstrip("v")

    _FINALIZE_RE = re.compile(r"^chore: finalize changelog for (.+)$")

    reverted = False
    finalize_reverted = False
    try:
        head_msg = run("git", ["log", "-1", "--format=%s"])

        # Two-commit pattern: HEAD is the finalize commit
        finalize_match = _FINALIZE_RE.match(head_msg)
        if finalize_match:
            finalize_version_str = finalize_match.group(1)
            run("git", ["revert", "--no-edit", "HEAD"])
            finalize_reverted = True

            # Now check if the new HEAD is the version-bump commit
            new_head_msg = run("git", ["log", "-1", "--format=%s"])
            if new_head_msg == expected_msg:
                run("git", ["revert", "--no-edit", "HEAD"])
                reverted = True
                results.append(("Revert commit", OK, "-"))
            else:
                # Finalize was reverted but version-bump wasn't at HEAD~1
                reverted = True
                results.append(("Revert commit", OK, "(finalize commit only)"))
        elif head_msg == expected_msg:
            # Single-commit pattern: HEAD is the version-bump commit
            run("git", ["revert", "--no-edit", "HEAD"])
            reverted = True
            results.append(("Revert commit", OK, "-"))
        else:
            results.append(("Revert commit", SKIPPED, f"HEAD ({head_msg}) does not match expected ({expected_msg})"))
    except Exception:
        results.append(("Revert commit", FAILED, "git revert --no-edit HEAD"))

    # Restore changelog state if we reverted a finalize commit
    if finalize_reverted:
        try:
            project_path = os.getcwd()
            changes_dir = get_changes_dir(project_path)
            unfinalize_version(changes_dir, bare_version)
            generate_changelog(project_path)
            # Commit the restored changelog files
            run("git", ["add", changes_dir, os.path.join(project_path, "CHANGELOG.md")])
            run("git", ["commit", "-m", f"chore: restore changelog after undo of {tag}"])
            results.append(("Restore changelog", OK, "-"))
        except Exception:
            results.append(("Restore changelog", FAILED, "manually restore .rlsbl/changes/ and regenerate CHANGELOG.md"))

    # Push the revert commit to remote
    if reverted:
        should_push = flags.get("yes")
        if not should_push:
            try:
                answer = input("\nPush revert to remote? [y/N] ").strip().lower()
                should_push = answer == "y"
            except (EOFError, KeyboardInterrupt):
                should_push = False

        if should_push:
            try:
                branch = get_current_branch()
                # Mark the revert push as release-authorized so the pre-push
                # hook doesn't warn about a "manual push" to the release
                # branch -- undo is part of the release flow.
                push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
                push_if_needed(branch, env=push_env, project_root=project_root)
                results.append(("Push", OK, "-"))
            except Exception:
                results.append(("Push", FAILED, "git push"))

    # Print summary: table only if something failed, otherwise a simple success message
    has_failure = any(status == FAILED for _, status, _ in results)
    if has_failure:
        _print_summary(results)
    else:
        print("\nUndo complete.")
