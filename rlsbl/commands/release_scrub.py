"""Release scrub command: wraps safegit scrub with JSONL hash remapping, CHANGELOG regeneration, tag updates, and GitHub Release recreation."""

import json
import os
import re
import sys

from ..changelog.files import get_changes_dir, remap_jsonl_hashes
from ..changelog.generate import generate_changelog
from ..lock import acquire_lock, release_lock
from ..utils import (
    run,
    run_gh,
    require_tool,
    check_gh_installed,
    check_gh_auth,
    extract_changelog_entry,
    get_current_branch,
    get_push_timeout,
)
from ..workspace import load_workspace


def _save_step(path, data, step_name):
    """Record a completed step in the scrub result file."""
    data["completed_steps"].append(step_name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def run_cmd(flags, *, ctx):
    # -- Validate inputs --
    if not flags.get("pattern") and not flags.get("file"):
        print("Error: either --pattern or --file must be provided.", file=sys.stderr)
        sys.exit(1)

    if not flags.get("replace") and not flags.get("mangle"):
        print("Error: either --replace or --mangle must be provided.", file=sys.stderr)
        sys.exit(1)

    if not flags.get("from-commit") and not flags.get("entire-history"):
        print("Error: either --from-commit or --entire-history must be provided.", file=sys.stderr)
        sys.exit(1)

    if not flags.get("reason"):
        print("Error: --reason is required.", file=sys.stderr)
        sys.exit(1)

    # -- Check safegit >= 0.18.0 --
    require_tool("safegit", purpose="for history scrubbing")
    version_out = run("safegit", ["--version"])
    # version_out is like "safegit 0.18.0" or just "0.18.0"
    version_str = version_out.strip().split()[-1]
    parts = version_str.split(".")
    version_tuple = tuple(int(p) for p in parts[:3])
    if version_tuple < (0, 18, 0):
        print(f"Error: safegit >= 0.18.0 required, found {version_str}", file=sys.stderr)
        sys.exit(1)

    # -- Check for scrub-result.json (resume support) --
    # Same home as the release state file: releasable members keep it under
    # .rlsbl-monorepo/releasables/<name>/releases/.
    from .release.release_state import get_scrub_result_path, resolve_releasable_dir

    project_root = ctx.project_root
    _scrub_releasable_dir = None
    if ctx.workspace_root:
        _scrub_releasable_dir = resolve_releasable_dir(
            str(project_root), str(ctx.workspace_root),
        )
    scrub_result_path = get_scrub_result_path(
        str(project_root), releasable_dir=_scrub_releasable_dir,
    )

    resuming = False
    scrub_data = None
    if os.path.exists(scrub_result_path):
        with open(scrub_result_path, "r", encoding="utf-8") as f:
            scrub_data = json.load(f)
        # Check if the saved new_head matches current HEAD
        current_head = run("git", ["rev-parse", "HEAD"])
        saved_head = scrub_data.get("new_head")
        if saved_head and saved_head == current_head:
            print("Resuming from saved scrub result...")
            resuming = True
        else:
            print(
                "Error: stale scrub-result.json found. Current HEAD does not match saved new_head.\n"
                f"  saved:   {saved_head}\n"
                f"  current: {current_head}\n"
                f"Delete {scrub_result_path} or run from the correct branch.",
                file=sys.stderr,
            )
            sys.exit(1)

    # -- If not resuming, build and run safegit command --
    if not resuming:
        # Determine subcommand: "match" for pattern, "file" for file
        if flags.get("pattern"):
            safegit_sub = "match"
            safegit_target_flag = "--pattern"
            safegit_target_value = flags["pattern"]
        else:
            safegit_sub = "file"
            safegit_target_flag = "--file"
            safegit_target_value = flags["file"]

        safegit_args = ["scrub", safegit_sub, "--json", safegit_target_flag, safegit_target_value]

        if flags.get("replace"):
            safegit_args.extend(["--replace", flags["replace"]])
        elif flags.get("mangle"):
            safegit_args.append("--mangle")

        if flags.get("from-commit"):
            safegit_args.extend(["--from", flags["from-commit"]])
        elif flags.get("entire-history"):
            safegit_args.append("--entire-history")

        safegit_args.extend(["--reason", flags["reason"]])

        if flags.get("dry-run"):
            safegit_args.append("--dry-run")

        try:
            output = run("safegit", safegit_args, timeout=600)
        except Exception as e:
            print(f"Error: safegit scrub failed: {e}", file=sys.stderr)
            sys.exit(1)

        scrub_data = json.loads(output)

        if flags.get("dry-run"):
            rewrites = scrub_data.get("rewrites", {})
            tags = scrub_data.get("tags", [])
            print(f"Dry run: {len(rewrites)} commits would be rewritten, {len(tags)} tags affected.")
            return

        # Save scrub-result.json for resume support
        scrub_data["completed_steps"] = []
        os.makedirs(os.path.dirname(scrub_result_path), exist_ok=True)
        tmp_path = scrub_result_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(scrub_data, f, indent=2)
        os.replace(tmp_path, scrub_result_path)

    # -- Parse results --
    rewrites = scrub_data.get("rewrites", {})
    tags = scrub_data.get("tags", [])
    completed = set(scrub_data.get("completed_steps", []))

    if not rewrites:
        print("No matches found, nothing to do.")
        # Clean up scrub-result.json if it exists
        if os.path.exists(scrub_result_path):
            os.unlink(scrub_result_path)
        return

    # -- Confirmation prompt (unless --yes or resuming) --
    if not resuming and not flags.get("yes"):
        print(f"{len(rewrites)} commits rewritten, {len(tags)} tags affected.")
        print("This will force-push rewritten history. Continue? [y/N]")
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    # -- Cache workspace projects --
    workspace_projects = load_workspace(str(ctx.workspace_root)) if ctx.workspace_root else None

    # -- Build tag prefix index for monorepo tag-to-project lookup --
    tag_prefix_index = None
    if workspace_projects is not None:
        tag_prefix_index = {f"{proj.name}@": proj for proj in workspace_projects}

    # -- Acquire lock --
    lock_dir = ".rlsbl-monorepo" if ctx.workspace_root else ".rlsbl"
    lock_root = str(ctx.workspace_root) if ctx.workspace_root else str(project_root)
    acquire_lock(lock_dir=lock_dir, project_root=lock_root)

    all_remap_results = []
    all_changes_dirs = []

    try:
        # -- Remap JSONL hashes --
        if "JSONL_REMAPPED" not in completed:
            if ctx.workspace_root:
                # Monorepo: remap all projects
                for proj in workspace_projects:
                    proj_path = os.path.join(str(ctx.workspace_root), proj.path)
                    changes_dir = get_changes_dir(proj_path)
                    all_changes_dirs.append(changes_dir)
                    results = remap_jsonl_hashes(changes_dir, rewrites)
                    all_remap_results.extend(results)
            else:
                # Standalone project
                changes_dir = get_changes_dir(str(project_root))
                all_changes_dirs.append(changes_dir)
                results = remap_jsonl_hashes(changes_dir, rewrites)
                all_remap_results.extend(results)

            _save_step(scrub_result_path, scrub_data, "JSONL_REMAPPED")

        # -- Regenerate CHANGELOG.md --
        if "CHANGELOG_GENERATED" not in completed:
            if ctx.workspace_root:
                for proj in workspace_projects:
                    if not proj.is_releasable:
                        continue
                    proj_path = os.path.join(str(ctx.workspace_root), proj.path)
                    generate_changelog(proj_path)
            else:
                generate_changelog(str(project_root))

            _save_step(scrub_result_path, scrub_data, "CHANGELOG_GENERATED")

        # -- Delete .validated caches --
        if "VALIDATED_DELETED" not in completed:
            if not all_changes_dirs:
                # Rebuild the list if resuming
                if ctx.workspace_root:
                    all_changes_dirs = [get_changes_dir(os.path.join(str(ctx.workspace_root), p.path)) for p in workspace_projects]
                else:
                    all_changes_dirs = [get_changes_dir(str(project_root))]
            for changes_dir in all_changes_dirs:
                validated = os.path.join(changes_dir, ".validated")
                try:
                    os.unlink(validated)
                except FileNotFoundError:
                    pass

            _save_step(scrub_result_path, scrub_data, "VALIDATED_DELETED")

        # -- Commit --
        if "COMMITTED" not in completed:
            # Collect all modified files
            modified_files = []
            for r in all_remap_results:
                modified_files.append(r.path)

            # Add CHANGELOG.md files
            if ctx.workspace_root:
                for proj in workspace_projects:
                    if not proj.is_releasable:
                        continue
                    proj_path = os.path.join(str(ctx.workspace_root), proj.path)
                    cl = os.path.join(proj_path, "CHANGELOG.md")
                    if os.path.exists(cl):
                        modified_files.append(cl)
                    # Also add per-version .md files in changes dir
                    changes_dir = get_changes_dir(proj_path)
                    if os.path.isdir(changes_dir):
                        for fname in os.listdir(changes_dir):
                            if fname.endswith(".md"):
                                modified_files.append(os.path.join(changes_dir, fname))
            else:
                cl = os.path.join(str(project_root), "CHANGELOG.md")
                if os.path.exists(cl):
                    modified_files.append(cl)
                changes_dir = get_changes_dir(str(project_root))
                if os.path.isdir(changes_dir):
                    for fname in os.listdir(changes_dir):
                        if fname.endswith(".md"):
                            modified_files.append(os.path.join(changes_dir, fname))

            # Only commit if there are modified files
            if modified_files:
                reason = flags.get("reason", "scrub")
                commit_msg = f"scrub: {reason}"
                try:
                    run("safegit", ["commit", "-m", commit_msg, "--"] + modified_files)
                except Exception as e:
                    print(f"Warning: commit failed: {e}", file=sys.stderr)
                    print("You may need to commit the changes manually.", file=sys.stderr)

            _save_step(scrub_result_path, scrub_data, "COMMITTED")

        # -- Force-push branch --
        if "BRANCH_PUSHED" not in completed:
            push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
            branch = get_current_branch()
            try:
                run("git", ["push", "--force-with-lease", "origin", branch],
                    timeout=get_push_timeout(ctx.config), env=push_env)
            except Exception as e:
                print(f"Error: failed to push branch: {e}", file=sys.stderr)
                sys.exit(1)

            _save_step(scrub_result_path, scrub_data, "BRANCH_PUSHED")

        # -- Force-push tags --
        if "TAGS_PUSHED" not in completed:
            push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
            for tag_info in tags:
                refname = tag_info.get("refname", "")
                tag_name = refname.removeprefix("refs/tags/")
                if not tag_name:
                    continue
                try:
                    run("git", ["push", "--force", "origin", tag_name],
                        timeout=get_push_timeout(ctx.config), env=push_env)
                except Exception as e:
                    print(f"Warning: failed to push tag {tag_name}: {e}", file=sys.stderr)

            _save_step(scrub_result_path, scrub_data, "TAGS_PUSHED")

        # -- Recreate GitHub Releases --
        if "RELEASES_UPDATED" not in completed:
            if check_gh_installed() and check_gh_auth():
                for tag_info in tags:
                    refname = tag_info.get("refname", "")
                    tag_name = refname.removeprefix("refs/tags/")
                    if not tag_name:
                        continue

                    # Check if a GitHub Release exists for this tag
                    try:
                        run_gh(["release", "view", tag_name, "--json", "body"], config=ctx.config)
                    except Exception:
                        # No release exists for this tag -- skip
                        continue

                    # Delete existing release
                    try:
                        run_gh(["release", "delete", tag_name, "--yes"], config=ctx.config)
                    except Exception as e:
                        print(f"Warning: failed to delete release {tag_name}: {e}", file=sys.stderr)
                        continue

                    # Extract version from tag name
                    # Handle monorepo format: "project@v1.2.3" or "project/v1.2.3"
                    # Handle standalone format: "v1.2.3"
                    version_match = re.search(r"v(\d+\.\d+\.\d+)$", tag_name)
                    if not version_match:
                        print(f"Warning: cannot extract version from tag {tag_name}", file=sys.stderr)
                        continue
                    version = version_match.group(1)

                    # Find the right project's CHANGELOG.md
                    changelog_notes = None
                    if ctx.workspace_root:
                        # Use prefix index to match tag to project
                        matched_proj = None
                        if tag_prefix_index:
                            for prefix, proj in tag_prefix_index.items():
                                if tag_name.startswith(prefix):
                                    matched_proj = proj
                                    break
                        if matched_proj is not None:
                            # Matched a monorepo project by tag prefix
                            proj_path = os.path.join(str(ctx.workspace_root), matched_proj.path)
                            changelog_path = os.path.join(proj_path, "CHANGELOG.md")
                            if os.path.exists(changelog_path):
                                changelog_notes = extract_changelog_entry(changelog_path, version)
                        elif re.match(r"^v\d+\.\d+\.\d+$", tag_name):
                            # Standalone tag format (vX.Y.Z with no project prefix) -- use project root CHANGELOG
                            changelog_path = os.path.join(str(ctx.workspace_root), "CHANGELOG.md")
                            if os.path.exists(changelog_path):
                                changelog_notes = extract_changelog_entry(changelog_path, version)
                        else:
                            # Fallback: iterate all projects with a warning
                            print(f"Warning: no prefix match for tag {tag_name}, scanning all projects", file=sys.stderr)
                            for proj in workspace_projects:
                                proj_path = os.path.join(str(ctx.workspace_root), proj.path)
                                changelog_path = os.path.join(proj_path, "CHANGELOG.md")
                                if os.path.exists(changelog_path):
                                    entry = extract_changelog_entry(changelog_path, version)
                                    if entry:
                                        changelog_notes = entry
                                        break
                    else:
                        changelog_path = os.path.join(str(project_root), "CHANGELOG.md")
                        if os.path.exists(changelog_path):
                            changelog_notes = extract_changelog_entry(changelog_path, version)

                    if not changelog_notes:
                        changelog_notes = f"Release {version}"

                    # Create new release
                    try:
                        run_gh(["release", "create", tag_name,
                               "--title", tag_name,
                               "--notes", changelog_notes], config=ctx.config)
                    except Exception as e:
                        print(f"Warning: failed to recreate release {tag_name}: {e}", file=sys.stderr)

            _save_step(scrub_result_path, scrub_data, "RELEASES_UPDATED")

        # -- Cleanup and summary --
        if os.path.exists(scrub_result_path):
            os.unlink(scrub_result_path)

        total_hashes = sum(r.hashes_remapped for r in all_remap_results)
        total_files = len(all_remap_results)
        releases_count = sum(1 for t in tags if re.search(r"v\d+\.\d+\.\d+$", t.get("refname", "")))
        print(f"\nScrub complete. {total_hashes} hashes remapped across {total_files} files, "
              f"{len(tags)} tags updated, {releases_count} releases recreated.")

    finally:
        release_lock()
