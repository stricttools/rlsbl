"""Undo command that reverts the last release by deleting the GitHub Release, removing the git tag, and reverting the version bump commit."""

import os
import re
import sys
import traceback

from ..changelog.files import get_changes_dir, unfinalize_version
from ..changelog.generate import generate_changelog
from ..context import ProjectContext
from ..release_file import unfinalize_release_file
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


def run_cmd(registry, args, flags, *, ctx):
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
    start_path = str(ctx.project_root)
    ws_root = find_workspace_root(start_path)
    if ws_root:
        project = resolve_project(ws_root, start_path)
        if project is None:
            print("Error: current directory is inside a monorepo but not inside any project.", file=sys.stderr)
            sys.exit(1)
        monorepo_name = project["name"]
        monorepo_project_path = project["path"]

    # Find the latest tag (scoped to project in monorepo mode)
    if monorepo_name:
        abs_project_dir = os.path.join(ws_root, monorepo_project_path)
        target_entries = detect_targets(abs_project_dir)
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
        run("gh", ["release", "view", tag])
    except Exception:
        results.append(("Delete GitHub Release", SKIPPED, "no GitHub Release found"))
    else:
        try:
            run("gh", ["release", "delete", tag, "--yes"])
            results.append(("Delete GitHub Release", OK, "-"))
        except Exception:
            traceback.print_exc()
            results.append(("Delete GitHub Release", FAILED, f"gh release delete {tag} --yes"))

    # Delete remote tag (marked as release-authorized: undo is part of the
    # release flow, so the pre-push hook shouldn't warn about a manual push).
    try:
        undo_push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
        run("git", ["push", "origin", f":{tag}"], timeout=get_push_timeout(ctx.config), env=undo_push_env)
        results.append(("Delete remote tag", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Delete remote tag", FAILED, f"git push origin :{tag}"))

    # Delete local tag
    try:
        run("git", ["tag", "-d", tag])
        results.append(("Delete local tag", OK, "-"))
    except Exception:
        traceback.print_exc()
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

    project_path = os.path.join(ws_root, monorepo_project_path) if monorepo_name else str(ctx.project_root or ".")

    # Release commits can be up to 3 in sequence (newest first):
    #   3. "chore: finalize release file for X.Y.Z" (release file rename)
    #   2. "chore: finalize changelog for X.Y.Z" (changelog rename; skipped for dev nodes)
    #   1. version bump commit (tag string or "<project>: release vX.Y.Z")
    # We peel them off from HEAD in order, reverting each recognized commit.
    _CHANGELOG_FINALIZE_RE = re.compile(r"^chore: finalize changelog for (.+)$")
    _RELEASE_FILE_FINALIZE_RE = re.compile(r"^chore: finalize release file for (.+)$")

    reverted = False
    changelog_finalize_reverted = False
    any_finalize_reverted = False
    try:
        head_msg = run("git", ["log", "-1", "--format=%s"])

        # Peel release-file finalize commit if present
        if _RELEASE_FILE_FINALIZE_RE.match(head_msg):
            run("git", ["revert", "--no-edit", "HEAD"])
            any_finalize_reverted = True
            head_msg = run("git", ["log", "-1", "--format=%s"])

        # Peel changelog finalize commit if present
        if _CHANGELOG_FINALIZE_RE.match(head_msg):
            run("git", ["revert", "--no-edit", "HEAD"])
            changelog_finalize_reverted = True
            any_finalize_reverted = True
            head_msg = run("git", ["log", "-1", "--format=%s"])

        # Peel version bump commit
        if head_msg == expected_msg:
            run("git", ["revert", "--no-edit", "HEAD"])
            reverted = True
            results.append(("Revert commit", OK, "-"))
        elif any_finalize_reverted:
            # Finalize commits were reverted but version-bump wasn't at the expected position
            reverted = True
            results.append(("Revert commit", OK, "(finalize commit(s) only)"))
        else:
            results.append(("Revert commit", SKIPPED, f"HEAD ({head_msg}) does not match expected ({expected_msg})"))
    except Exception:
        traceback.print_exc()
        results.append(("Revert commit", FAILED, "git revert --no-edit HEAD"))

    # Restore changelog state if we reverted a changelog finalize commit
    if changelog_finalize_reverted:
        try:
            changes_dir = get_changes_dir(project_path)
            unfinalize_version(changes_dir, bare_version)
            generate_changelog(project_path)
            # Commit the restored changelog files
            run("git", ["add", changes_dir, os.path.join(project_path, "CHANGELOG.md")])
            run("git", ["commit", "-m", f"chore: restore changelog after undo of {tag}"])
            results.append(("Restore changelog", OK, "-"))
        except Exception:
            traceback.print_exc()
            results.append(("Restore changelog", FAILED, "manually restore .rlsbl/changes/ and regenerate CHANGELOG.md"))

    # Restore the release file (inverse of release-file finalization). When
    # the finalize commit was reverted above, git already restored the files
    # and this is a no-op; when it wasn't at HEAD (e.g., post-release hooks
    # added commits), the finalized read-only vX.Y.Z.toml and the fresh empty
    # unreleased.toml are still on disk and must be repaired directly.
    try:
        releases_dir = os.path.join(project_path, ".rlsbl", "releases")
        release_file_changed = unfinalize_release_file(releases_dir, bare_version)
        if release_file_changed:
            run("git", ["add", releases_dir])
            run("git", ["commit", "-m", f"chore: restore release file after undo of {tag}"])
            results.append(("Restore release file", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Restore release file", FAILED, f"manually restore .rlsbl/releases/unreleased.toml from v{bare_version}.toml"))

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
                push_if_needed(branch, env=push_env, config=ctx.config)
                results.append(("Push", OK, "-"))
            except Exception:
                traceback.print_exc()
                results.append(("Push", FAILED, "git push"))

    # Print summary: table only if something failed, otherwise a simple success message
    has_failure = any(status == FAILED for _, status, _ in results)
    if has_failure:
        _print_summary(results)
    else:
        print("\nUndo complete.")
