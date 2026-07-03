"""Release scrub command: wraps safegit scrub with JSONL hash remapping, CHANGELOG regeneration, tag updates, and GitHub Release recreation."""

import json
import os
import re
import sys

from ..changelog.files import (
    enumerate_changelog_dirs,
    get_changes_dir,
    remap_jsonl_hashes,
    validate_all_hashes_resolve,
)
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

# Minimum safegit release the scrub flow is built against: the fixes here
# depend on >= 0.19 recipe mode and >= 0.20.x dry-run JSON behavior. The
# integration test harness builds exactly this version.
SAFEGIT_MIN_VERSION = (0, 21, 1)


def _save_step(path, data, step_name):
    """Record a completed step in the scrub result file."""
    data["completed_steps"].append(step_name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _fail(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _select_and_validate_mode(flags):
    """Determine the scrub mode from flags and validate the per-mode contract.

    safegit's actual CLI contracts (verified against safegit source):
    - ``scrub match --pattern <re>`` with ``--replace``/``--mangle`` and
      ``--from``/``--entire-history``.
    - ``scrub file <path>`` takes a POSITIONAL path and only supports
      ``--from`` (required) and ``--reason``. There is no ``--file``,
      ``--replace``, ``--mangle``, or ``--entire-history`` flag; strictcli-go
      hard-errors on unknown flags.

    - ``scrub run <recipe.toml>`` takes a POSITIONAL recipe path with
      ``--from``/``--entire-history`` and ``--reason``; per-operation
      pattern/replace/mangle live inside the recipe file.

    Returns the mode string: "match", "file", or "recipe".
    """
    selectors = [name for name in ("pattern", "file", "recipe") if flags.get(name)]
    if len(selectors) != 1:
        _fail("exactly one of --pattern, --file, or --recipe must be provided.")
    mode = {"pattern": "match", "file": "file", "recipe": "recipe"}[selectors[0]]

    if not flags.get("reason"):
        _fail("--reason is required.")

    if mode == "match":
        if not flags.get("replace") and not flags.get("mangle"):
            _fail("either --replace or --mangle must be provided.")
        if not flags.get("from-commit") and not flags.get("entire-history"):
            _fail("either --from-commit or --entire-history must be provided.")
    elif mode == "file":
        if flags.get("replace") or flags.get("mangle"):
            _fail(
                "--replace/--mangle are match-mode flags; file mode replaces "
                "the file with its current on-disk content (or removes it if "
                "absent)."
            )
        if flags.get("entire-history"):
            _fail(
                "safegit scrub file has no --entire-history; pass "
                "--from-commit <root-sha> to cover the full history."
            )
        if not flags.get("from-commit"):
            _fail("--from-commit is required in file mode (safegit scrub file requires --from).")
    elif mode == "recipe":
        if flags.get("replace") or flags.get("mangle"):
            _fail(
                "--replace/--mangle are match-mode flags; recipe operations "
                "define their own replace/mangle inside the TOML file."
            )
        if not flags.get("from-commit") and not flags.get("entire-history"):
            _fail("either --from-commit or --entire-history must be provided.")
        if not os.path.isfile(flags["recipe"]):
            _fail(f"recipe file not found: {flags['recipe']}")

    return mode


def _build_safegit_args(flags, mode):
    """Build the safegit scrub argument list for the selected mode."""
    if mode == "match":
        args = ["scrub", "match", "--json"]
        if flags.get("dry-run"):
            args.append("--dry-run")
        args.extend(["--pattern", flags["pattern"]])
        if flags.get("replace"):
            args.extend(["--replace", flags["replace"]])
        else:
            args.append("--mangle")
        if flags.get("from-commit"):
            args.extend(["--from", flags["from-commit"]])
        else:
            args.append("--entire-history")
        args.extend(["--reason", flags["reason"]])
        return args

    if mode == "file":
        # File mode: positional path last, --from mandatory.
        args = ["scrub", "file", "--json"]
        if flags.get("dry-run"):
            args.append("--dry-run")
        args.extend(["--from", flags["from-commit"]])
        args.extend(["--reason", flags["reason"]])
        args.append(flags["file"])
        return args

    # Recipe mode: positional recipe path, range flags, reason.
    args = ["scrub", "run", "--json"]
    if flags.get("dry-run"):
        args.append("--dry-run")
    args.append(flags["recipe"])
    if flags.get("from-commit"):
        args.extend(["--from", flags["from-commit"]])
    else:
        args.append("--entire-history")
    args.extend(["--reason", flags["reason"]])
    return args


# Fields allowed in the archived TagRewrite records.
_ARCHIVE_TAG_KEYS = ("refname", "old_sha", "new_sha", "annotated")


def _build_scrub_archive(scrub_data, mode, reason):
    """Build the committed audit archive from the working scrub state.

    HARD SCHEMA RULE: the archive is committed to the repo, so it must never
    re-introduce what was scrubbed. Fields are WHITELISTED explicitly --
    commit SHAs, tag refnames, reason, mode, and the step list only. No
    patterns, no replacement strings, no file paths, no matched content, and
    nothing that arrives unexpectedly in safegit's JSON or rlsbl's state.
    """
    tags = [
        {k: t[k] for k in _ARCHIVE_TAG_KEYS if k in t}
        for t in scrub_data.get("tags", [])
    ]
    return {
        "schema_version": 1,
        "mode": mode,
        "reason": reason,
        "old_head": scrub_data.get("old_head"),
        "new_head": scrub_data.get("new_head"),
        "rewrites": scrub_data.get("rewrites", {}),
        "tags": tags,
        "commits_rewritten": scrub_data.get("commits_rewritten"),
        "completed_steps": list(scrub_data.get("completed_steps", [])),
    }


def _get_archive_path(scrub_result_path, new_head):
    """Archive location: a scrubs/ dir sibling to the releases/ state dir
    (so releasable-mode archives live under the releasable directory)."""
    state_home = os.path.dirname(os.path.dirname(scrub_result_path))
    return os.path.join(state_home, "scrubs", f"scrub-{new_head[:12]}.json")


def _print_dry_run_summary(mode, data):
    """Print a per-mode dry-run preview from safegit's REAL dry-run JSON.

    Dry-run schemas differ per mode and have NO rewrites/tags keys:
    - match: ScrubMatchDryRunResult (total_matches, estimated_commits, ...)
    - file:  ScrubFileDryRunResult (commit_count, mode, file)
    """
    if mode == "match":
        total = data.get("total_matches", 0)
        blobs = data.get("blob_matches", 0)
        msgs = data.get("commit_matches", 0)
        tag_m = data.get("tag_matches", 0)
        scanned = data.get("objects_scanned", 0)
        est = data.get("estimated_commits", 0)
        print(
            f"Dry run (match): {total} matches ({blobs} blob, {msgs} "
            f"commit-message, {tag_m} tag) across {scanned} objects; "
            f"~{est} commits would be rewritten."
        )
    elif mode == "file":
        action = "replaced" if data.get("mode") == "replace" else "removed"
        print(
            f"Dry run (file): {data.get('file')} would be {action}; "
            f"{data.get('commit_count', 0)} commits would be rewritten."
        )
    elif mode == "recipe":
        ops = data.get("operation_count", 0)
        blobs = data.get("total_blob_matches", 0)
        msgs = data.get("total_commit_matches", 0)
        tag_m = data.get("total_tag_matches", 0)
        est = data.get("estimated_commits", 0)
        print(
            f"Dry run (recipe): {ops} operations; {blobs} blob, {msgs} "
            f"commit-message, {tag_m} tag matches; "
            f"~{est} commits would be rewritten."
        )


def run_cmd(flags, *, ctx):
    # -- Validate inputs --
    mode = _select_and_validate_mode(flags)

    # -- Check safegit >= SAFEGIT_MIN_VERSION --
    require_tool("safegit", purpose="for history scrubbing")
    version_out = run("safegit", ["--version"])
    # version_out is like "safegit 0.21.1" or "safegit 0.21.1+dirty"
    version_str = version_out.strip().split()[-1]
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", version_str)
    if not m:
        print(f"Error: cannot parse safegit version from {version_out!r}", file=sys.stderr)
        sys.exit(1)
    version_tuple = tuple(int(g) for g in m.groups())
    if version_tuple < SAFEGIT_MIN_VERSION:
        min_str = ".".join(str(p) for p in SAFEGIT_MIN_VERSION)
        print(f"Error: safegit >= {min_str} required, found {version_str}", file=sys.stderr)
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
        safegit_args = _build_safegit_args(flags, mode)

        # Orchestration handshake: tells safegit this scrub is driven by
        # rlsbl (safegit will enforce this in a future release).
        scrub_env = {**os.environ, "RLSBL_SCRUB_ORCHESTRATED": "1"}
        try:
            output = run("safegit", safegit_args, timeout=600, env=scrub_env)
        except Exception as e:
            print(f"Error: safegit scrub failed: {e}", file=sys.stderr)
            sys.exit(1)

        # safegit emits NO JSON (empty stdout) when there is nothing to
        # rewrite, in both execute and some scoped paths.
        if not output.strip():
            print("No matches found, nothing to do.")
            return

        scrub_data = json.loads(output)

        if flags.get("dry-run"):
            _print_dry_run_summary(mode, scrub_data)
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

    # Every changelog dir with hash-bearing JSONL files: per-project
    # .rlsbl/changes/ plus releasable-level dirs in monorepos.
    all_changes_dirs = enumerate_changelog_dirs(
        str(project_root), ctx.workspace_root, workspace_projects=workspace_projects,
    )

    try:
        # -- Remap JSONL hashes --
        if "JSONL_REMAPPED" not in completed:
            for changes_dir in all_changes_dirs:
                report = remap_jsonl_hashes(changes_dir, rewrites)
                all_remap_results.extend(report.results)

            # Persist the modified paths so a resumed run can still commit
            # files remapped before an interruption.
            scrub_data["remapped_files"] = [r.path for r in all_remap_results]
            _save_step(scrub_result_path, scrub_data, "JSONL_REMAPPED")

        # -- Post-remap validation gate --
        # Every hash in every changelog dir must either have been remapped or
        # still resolve after the rewrite. Otherwise abort loudly BEFORE the
        # commit/push steps, keeping scrub-result.json for resume.
        if "HASHES_VALIDATED" not in completed:
            failures = validate_all_hashes_resolve(all_changes_dirs)
            if failures:
                print(
                    "Error: after the history rewrite, some changelog commit "
                    "hashes neither were remapped nor resolve:",
                    file=sys.stderr,
                )
                for filepath, hashes in failures.items():
                    print(f"  {filepath}: {', '.join(hashes)}", file=sys.stderr)
                print(
                    f"Aborting before commit/push. Fix the entries (e.g. "
                    f"rlsbl changelog amend) and re-run to resume; "
                    f"{scrub_result_path} is kept.",
                    file=sys.stderr,
                )
                sys.exit(1)

            _save_step(scrub_result_path, scrub_data, "HASHES_VALIDATED")

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
            deleted_validated = []
            for changes_dir in all_changes_dirs:
                validated = os.path.join(changes_dir, ".validated")
                if not os.path.exists(validated):
                    continue
                # Only tracked files can (and must) have their deletion
                # committed; untracked caches are just removed.
                tracked = True
                try:
                    run("git", ["ls-files", "--error-unmatch", validated])
                except Exception:
                    tracked = False
                os.unlink(validated)
                if tracked:
                    deleted_validated.append(validated)

            # Persist so a resumed run still commits the deletions.
            scrub_data["deleted_validated"] = deleted_validated
            _save_step(scrub_result_path, scrub_data, "VALIDATED_DELETED")

        # -- Commit --
        if "COMMITTED" not in completed:
            # Collect all modified files. Remapped paths come from the
            # persisted state (survives resume); fresh runs stored them in
            # the JSONL_REMAPPED step above.
            modified_files = list(scrub_data.get("remapped_files", []))

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

            # Include tracked .validated deletions so the tree is clean
            # after the scrub commit.
            modified_files.extend(scrub_data.get("deleted_validated", []))

            # Write the committed audit archive (whitelisted schema). It is
            # always a new file, so the scrub commit is never empty.
            new_head = scrub_data.get("new_head", "")
            if new_head:
                archive_path = _get_archive_path(scrub_result_path, new_head)
                os.makedirs(os.path.dirname(archive_path), exist_ok=True)
                archive = _build_scrub_archive(scrub_data, mode, flags["reason"])
                tmp_archive = archive_path + ".tmp"
                with open(tmp_archive, "w", encoding="utf-8") as f:
                    json.dump(archive, f, indent=2)
                os.replace(tmp_archive, archive_path)
                modified_files.append(archive_path)

            # Only commit if there are modified files
            if modified_files:
                reason = flags.get("reason", "scrub")
                commit_msg = f"scrub: {reason}"
                commit_args = ["commit", "-m", commit_msg]
                old_head = scrub_data.get("old_head")
                new_head = scrub_data.get("new_head")
                if old_head and new_head:
                    # Machine-greppable audit trailer linking the scrub
                    # commit to the exact head remap.
                    commit_args.extend(
                        ["--trailer", f"Scrub-remap: {old_head}..{new_head}"]
                    )
                try:
                    run("safegit", commit_args + ["--"] + modified_files)
                except Exception as e:
                    # Never proceed to force-push without the metadata
                    # repairs committed -- that would publish inconsistent
                    # history. Abort with resume state intact.
                    print(f"Error: scrub metadata commit failed: {e}", file=sys.stderr)
                    print(
                        f"Aborting before push; fix the issue and re-run to "
                        f"resume ({scrub_result_path} is kept).",
                        file=sys.stderr,
                    )
                    sys.exit(1)

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
