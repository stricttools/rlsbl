"""Release scrub command: wraps safegit scrub with in-history JSONL hash remapping (--remap-shas-in), CHANGELOG verification, tag updates, and GitHub Release recreation."""

import difflib
import json
import os
import re
import sys

from ..changelog.files import (
    can_remap_hash,
    changelog_remap_globs,
    changes_dir_exists,
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

# Minimum safegit release the scrub flow is built against: the flow depends
# on >= 0.22.0 for --remap-shas-in (in-history changelog hash remapping), the
# persisted rewrite journal (.git/safegit/rewrite-maps.jsonl), and the
# cleanup_ok/cleanup_errors/pre_rewrite_remotes JSON fields. The integration
# test harness builds exactly this version.
SAFEGIT_MIN_VERSION = (0, 22, 0)


def _tag_name_from_refname(refname):
    """Return the tag name for a ``refs/tags/...`` refname, else None.

    safegit's tags[] list should only contain tag refs, but a stray
    non-tag refname (e.g. ``refs/heads/x``) must never be treated as a
    tag: ``removeprefix`` alone would leave it unchanged and the tag step
    would force-push it.
    """
    if not refname.startswith("refs/tags/"):
        return None
    return refname[len("refs/tags/"):] or None


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


def _remap_glob_args(remap_globs):
    """Repeatable --remap-shas-in flag pairs for the safegit invocation."""
    args = []
    for glob in remap_globs:
        args.extend(["--remap-shas-in", glob])
    return args


def _build_safegit_args(flags, mode, remap_globs):
    """Build the safegit scrub argument list for the selected mode.

    ``remap_globs`` (from ``changelog_remap_globs``) is passed as repeatable
    ``--remap-shas-in`` flags in every mode: safegit rewrites full 40-hex
    commit hashes inside the glob-matched changelog files at EVERY commit of
    the rewritten history, so all historical versions -- including HEAD --
    stay self-consistent.
    """
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
        args.extend(_remap_glob_args(remap_globs))
        args.extend(["--reason", flags["reason"]])
        return args

    if mode == "file":
        # File mode: positional path last, --from mandatory.
        args = ["scrub", "file", "--json"]
        if flags.get("dry-run"):
            args.append("--dry-run")
        args.extend(["--from", flags["from-commit"]])
        args.extend(_remap_glob_args(remap_globs))
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
    args.extend(_remap_glob_args(remap_globs))
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


def _snapshot_remote_refs():
    """Snapshot the remote's refs BEFORE the rewrite.

    Returns {refname: sha}. These are the only trustworthy lease
    expectations for the post-scrub force-pushes: after the rewrite, bare
    --force-with-lease is useless because safegit rewrites the
    remote-tracking refs, and tags carry no tracking information at all.
    """
    out = run("git", ["ls-remote", "origin"], timeout=120)
    refs = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            sha, ref = parts
            refs[ref.strip()] = sha.strip()
    return refs


def _push_ref_with_lease(refname, expected_sha, target_sha, *, timeout, env):
    """Force-push one ref with an explicit lease expectation.

    ``expected_sha`` is the remote value captured before the scrub (None
    when the ref did not exist remotely). ``target_sha`` is the value the
    remote should end up with; if the push is rejected but the remote
    already equals ``target_sha`` (a resumed run), the push is treated as
    done. Any other rejection is a hard error: the remote changed under us
    and force-pushing would destroy someone's work.
    """
    lease = f"--force-with-lease={refname}:{expected_sha or ''}"
    try:
        run("git", ["push", lease, "origin", f"{refname}:{refname}"],
            timeout=timeout, env=env)
        return
    except Exception as push_exc:
        # Idempotence: a previous (partially completed) run may have
        # already pushed this ref.
        try:
            out = run("git", ["ls-remote", "origin", refname], timeout=120)
            current = out.split()[0] if out.split() else ""
        except Exception:
            current = ""
        if target_sha and current == target_sha:
            print(f"{refname} already up to date on origin.")
            return
        print(
            f"Error: failed to push {refname}: {push_exc}\n"
            f"  expected remote value: {expected_sha or '<absent>'}\n"
            f"  current remote value:  {current or '<unknown>'}\n"
            f"The remote changed since the scrub started; refusing to "
            f"force-push over it.",
            file=sys.stderr,
        )
        sys.exit(1)


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


def _load_rewrite_journal():
    """Load the LAST rewrite group from safegit's persisted rewrite journal.

    ``.git/safegit/rewrite-maps.jsonl`` holds up to three phase records per
    rewrite (start/refs/complete) sharing one id. The start record carries
    the full old-to-new commit map and is written BEFORE any refs move, so
    even a crashed rewrite leaves its mapping recoverable.

    Lines can be MB-scale (full commit maps), so the file is streamed one
    ``json.loads`` per line with no line-length assumptions. Multiple
    rewrite ids are tolerated: the group whose start record appears last
    wins. A corrupt line is a hard error -- recovering with a partial map
    would silently mis-repair changelogs.

    Returns ``{"id", "op", "reason", "created_at", "commit_map",
    "complete", "path"}`` or None when no journal (or no start record)
    exists.
    """
    try:
        git_dir = run("git", ["rev-parse", "--git-dir"])
    except Exception:
        return None
    if not git_dir:
        return None
    journal_path = os.path.join(
        os.path.abspath(git_dir), "safegit", "rewrite-maps.jsonl"
    )
    if not os.path.isfile(journal_path):
        return None

    groups: dict[str, dict] = {}
    order: list[str] = []
    with open(journal_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError as e:
                _fail(
                    f"corrupt safegit rewrite journal "
                    f"{journal_path} line {lineno}: {e}"
                )
            rid = rec.get("id")
            phase = rec.get("phase")
            if not rid or not phase:
                continue
            group = groups.setdefault(rid, {})
            if phase == "start" and "start" not in group:
                order.append(rid)
                group["start"] = rec
            elif phase == "complete":
                group["complete"] = rec

    for rid in reversed(order):
        start = groups[rid]["start"]
        return {
            "id": rid,
            "op": start.get("op", ""),
            "reason": start.get("reason", ""),
            "created_at": start.get("created_at", ""),
            "commit_map": start.get("commit_map", {}) or {},
            "complete": "complete" in groups[rid],
            "path": journal_path,
        }
    return None


def _recover_from_rewrite_journal(all_changes_dirs, failures, scrub_data):
    """Repair dangling changelog hashes from the persisted rewrite journal.

    Fallback for a scrub whose in-history remap did not cover the working
    tree -- e.g. a scrub interrupted after safegit finished but before
    rlsbl's steps completed, or a scrub someone ran orchestrated but outside
    ``rlsbl release scrub`` (without ``--remap-shas-in``). Applies ONLY when
    the journal's commit map can actually fix at least one dangling hash;
    otherwise returns False and leaves every file untouched.

    Repaired file paths are recorded in ``scrub_data["remapped_files"]`` so
    the commit step includes them (and a resumed run still commits them).
    """
    journal = _load_rewrite_journal()
    if journal is None:
        return False
    commit_map = journal["commit_map"]
    fixable = any(
        can_remap_hash(h, commit_map)
        for hashes in failures.values()
        for h in hashes
    )
    if not fixable:
        return False

    print(
        f"Dangling changelog hashes found; recovering from the safegit "
        f"rewrite journal:\n"
        f"  journal: {journal['path']}\n"
        f"  rewrite: id={journal['id']} op={journal['op']} "
        f"created_at={journal['created_at']}"
    )
    if not journal["complete"]:
        print(
            "Warning: the journal's last rewrite has a 'start' record but "
            "no 'complete' record -- that rewrite CRASHED partway. The "
            "commit map was persisted before any refs moved, so it is used "
            "for recovery, but investigate the crashed rewrite (safegit "
            "doctor) before trusting the repository state.",
            file=sys.stderr,
        )

    repaired = []
    for changes_dir in all_changes_dirs:
        report = remap_jsonl_hashes(changes_dir, commit_map)
        repaired.extend(report.results)
    for r in repaired:
        print(
            f"  repaired {r.path}: {r.hashes_remapped} hash(es) in "
            f"{r.entries_modified} entrie(s)"
        )
    scrub_data["remapped_files"] = sorted(
        set(scrub_data.get("remapped_files", [])) | {r.path for r in repaired}
    )
    return True


def _read_file_bytes(path):
    """File content as bytes, or None when the file does not exist."""
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _regenerate_and_assert_unchanged(proj_path, scrub_result_path):
    """Regenerate the changelog and assert it is byte-identical to disk.

    With in-history hash remapping, HEAD's JSONL already carries the new
    SHAs when safegit returns, so regeneration must be a no-op. A diff means
    something ELSE is wrong (hand-edited CHANGELOG.md, generation drift,
    inconsistent JSONL) -- hard error with the diff shown, originals
    restored, and resume state intact. Nothing is committed when unchanged.
    """
    changes_dir = get_changes_dir(proj_path)
    changelog_path = os.path.join(proj_path, "CHANGELOG.md")

    watched = {changelog_path: _read_file_bytes(changelog_path)}
    if os.path.isdir(changes_dir):
        for fname in os.listdir(changes_dir):
            if fname.endswith(".md"):
                p = os.path.join(changes_dir, fname)
                watched[p] = _read_file_bytes(p)

    generate_changelog(proj_path)

    # Include files the regeneration may have newly created.
    after_paths = set(watched)
    if os.path.isdir(changes_dir):
        after_paths.update(
            os.path.join(changes_dir, fname)
            for fname in os.listdir(changes_dir)
            if fname.endswith(".md")
        )

    diffs = []
    for p in sorted(after_paths):
        before = watched.get(p)
        after = _read_file_bytes(p)
        if before != after:
            diffs.append((p, before, after))

    if not diffs:
        return

    # Restore the on-disk originals: the working tree must stay exactly as
    # the scrub left it so the operator diagnoses against reality.
    for p, before, _after in diffs:
        if before is None:
            if os.path.exists(p):
                os.unlink(p)
        else:
            with open(p, "wb") as f:
                f.write(before)

    print(
        "Error: regenerating the changelog produced content that differs "
        "from what is on disk. With in-history hash remapping the "
        "changelog at HEAD must already be consistent -- a diff means "
        "something else is wrong (e.g. a hand-edited CHANGELOG.md, or "
        "files generated by a different rlsbl version). The regenerated "
        "content was NOT kept. Diff (on disk -> regenerated):",
        file=sys.stderr,
    )
    for p, before, after in diffs:
        before_text = (before or b"").decode("utf-8", errors="replace")
        after_text = (after or b"").decode("utf-8", errors="replace")
        diff_lines = difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=f"{p} (on disk)",
            tofile=f"{p} (regenerated)",
        )
        sys.stderr.writelines(diff_lines)
    print(
        f"\nAborting before commit/push. Fix the inconsistency and re-run "
        f"to resume; {scrub_result_path} is kept.",
        file=sys.stderr,
    )
    sys.exit(1)


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

    # -- Cache workspace projects (also needed for the remap globs) --
    workspace_projects = load_workspace(str(ctx.workspace_root)) if ctx.workspace_root else None

    # -- If not resuming, build and run safegit command --
    if not resuming:
        # Snapshot remote refs BEFORE rewriting: these are the lease
        # expectations for the force-pushes later. Only needed when the
        # rewrite will actually happen.
        remote_refs = None
        if not flags.get("dry-run"):
            try:
                remote_refs = _snapshot_remote_refs()
            except Exception as e:
                print(
                    f"Error: cannot read remote refs from origin: {e}\n"
                    f"A scrub force-pushes rewritten history, so push access "
                    f"to origin is required before starting.",
                    file=sys.stderr,
                )
                sys.exit(1)

        remap_globs = changelog_remap_globs(
            str(project_root), ctx.workspace_root,
            workspace_projects=workspace_projects,
        )
        safegit_args = _build_safegit_args(flags, mode, remap_globs)

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
        scrub_data["remote_refs"] = remote_refs or {}
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

    # -- Build tag prefix index for monorepo tag-to-project lookup --
    tag_prefix_index = None
    if workspace_projects is not None:
        tag_prefix_index = {f"{proj.name}@": proj for proj in workspace_projects}

    # -- Acquire lock --
    lock_dir = ".rlsbl-monorepo" if ctx.workspace_root else ".rlsbl"
    lock_root = str(ctx.workspace_root) if ctx.workspace_root else str(project_root)
    acquire_lock(lock_dir=lock_dir, project_root=lock_root)

    # Every changelog dir with hash-bearing JSONL files: per-project
    # .rlsbl/changes/ plus releasable-level dirs in monorepos.
    all_changes_dirs = enumerate_changelog_dirs(
        str(project_root), ctx.workspace_root, workspace_projects=workspace_projects,
    )

    try:
        # -- Validation gate (in-history remap makes a worktree remap
        # unnecessary: safegit's --remap-shas-in already rewrote the JSONL
        # hashes at every commit, and Finalize synced the worktree) --
        # Every hash in every changelog dir must resolve after the rewrite.
        # When some do not, the persisted rewrite journal is the explicit
        # recovery fallback (e.g. a scrub run without --remap-shas-in, or
        # one interrupted before rlsbl's steps completed). Anything still
        # dangling afterwards aborts loudly BEFORE the commit/push steps,
        # keeping scrub-result.json for resume.
        if "HASHES_VALIDATED" not in completed:
            failures = validate_all_hashes_resolve(
                all_changes_dirs, repo_root=lock_root,
            )
            if failures:
                if _recover_from_rewrite_journal(
                    all_changes_dirs, failures, scrub_data,
                ):
                    failures = validate_all_hashes_resolve(
                        all_changes_dirs, repo_root=lock_root,
                    )
            if failures:
                print(
                    "Error: after the history rewrite, some changelog commit "
                    "hashes do not resolve (and no rewrite journal could fix "
                    "them):",
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

        # -- Verify CHANGELOG.md (regenerate-and-assert-unchanged) --
        # In-history remap means HEAD's JSONL was already consistent when
        # safegit returned, so regeneration must reproduce what is on disk
        # byte-for-byte; a diff is a hard error (something else is wrong).
        # Only for projects that actually have a changes dir: calling the
        # generator on a project without one would fabricate a stub
        # CHANGELOG.md (e.g. releasable members keep their changelog at the
        # releasable level, not the project root).
        if "CHANGELOG_VERIFIED" not in completed:
            if ctx.workspace_root:
                for proj in workspace_projects:
                    if not proj.is_releasable:
                        continue
                    proj_path = os.path.join(str(ctx.workspace_root), proj.path)
                    if changes_dir_exists(proj_path):
                        _regenerate_and_assert_unchanged(
                            proj_path, scrub_result_path,
                        )
            else:
                if changes_dir_exists(str(project_root)):
                    _regenerate_and_assert_unchanged(
                        str(project_root), scrub_result_path,
                    )

            _save_step(scrub_result_path, scrub_data, "CHANGELOG_VERIFIED")

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
            # The scrub commit carries only rlsbl's own artifacts: files the
            # journal recovery repaired (persisted in remapped_files so a
            # resumed run still commits them), tracked .validated deletions,
            # and the audit archive below. Changelog files are NOT collected:
            # the in-history remap already made HEAD consistent, and the
            # CHANGELOG step above asserted regeneration is a no-op.
            modified_files = list(scrub_data.get("remapped_files", []))
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

        # -- Force-push branch (explicit lease from the pre-scrub snapshot) --
        remote_refs = scrub_data.get("remote_refs", {})
        push_timeout = get_push_timeout(ctx.config)

        if "BRANCH_PUSHED" not in completed:
            push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
            branch = get_current_branch()
            branch_ref = f"refs/heads/{branch}"
            # Target: the local branch tip (new head plus the metadata commit).
            try:
                branch_target = run("git", ["rev-parse", branch_ref])
            except Exception:
                branch_target = ""
            _push_ref_with_lease(
                branch_ref, remote_refs.get(branch_ref), branch_target,
                timeout=push_timeout, env=push_env,
            )

            _save_step(scrub_result_path, scrub_data, "BRANCH_PUSHED")

        # -- Force-push tags (explicit lease each; never plain --force) --
        if "TAGS_PUSHED" not in completed:
            push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
            for tag_info in tags:
                refname = tag_info.get("refname", "")
                if _tag_name_from_refname(refname) is None:
                    print(
                        f"Warning: skipping non-tag refname in scrub tag "
                        f"list: {refname!r}",
                        file=sys.stderr,
                    )
                    continue
                _push_ref_with_lease(
                    refname, remote_refs.get(refname), tag_info.get("new_sha", ""),
                    timeout=push_timeout, env=push_env,
                )

            _save_step(scrub_result_path, scrub_data, "TAGS_PUSHED")

        # -- Recreate GitHub Releases --
        if "RELEASES_UPDATED" not in completed:
            if check_gh_installed() and check_gh_auth():
                for tag_info in tags:
                    refname = tag_info.get("refname", "")
                    tag_name = _tag_name_from_refname(refname)
                    if tag_name is None:
                        print(
                            f"Warning: skipping non-tag refname in scrub "
                            f"tag list: {refname!r}",
                            file=sys.stderr,
                        )
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

        releases_count = sum(1 for t in tags if re.search(r"v\d+\.\d+\.\d+$", t.get("refname", "")))
        repaired_count = len(scrub_data.get("remapped_files", []))
        repaired_note = (
            f" {repaired_count} changelog file(s) repaired from the rewrite "
            f"journal."
            if repaired_count else ""
        )
        print(f"\nScrub complete. {len(rewrites)} commits rewritten, "
              f"{len(tags)} tags updated, {releases_count} releases "
              f"recreated.{repaired_note}")

    finally:
        release_lock()
