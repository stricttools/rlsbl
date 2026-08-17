"""Release scrub command: wraps safegit scrub with in-history JSONL hash remapping (--remap-shas-in), CHANGELOG verification, tag updates, and GitHub Release recreation."""

import difflib
import json
import os
import re
import sys

from .. import effects
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
from ..tag_glob import TagMode, parse_version_tag
from .release_reconcile import (
    push_ref_with_lease as _push_ref_with_lease_impl,
    push_rewritten_tags,
    recreate_github_releases,
    snapshot_remote_refs as _snapshot_remote_refs_impl,
)
from .. import effects

# Minimum safegit release the scrub flow is built against: the flow depends on
# --remap-shas-in (in-history changelog hash remapping), the persisted rewrite
# journal (.git/safegit/rewrite-maps.jsonl), and the
# cleanup_ok/cleanup_errors/pre_rewrite_remotes fields (all >= 0.22.0), and on
# >= 0.25.0 for two coupled behaviour changes: destructive rewrites in
# rlsbl-managed repos no longer require an orchestration handshake, and --json
# no longer answers the destructive confirmation, so the invocation below
# passes the confirmation-skip flag explicitly.
#
# 0.27.0 is where safegit's --json became the FRAMEWORK's machine mode: stdout
# carries exactly one document, the strictcli envelope, and safegit's own data
# is its `payload` member. That is a different document shape, so this flow
# reads envelopes only -- an older safegit's bare JSON is refused by name
# rather than half-parsed. There is no dual support: safegit is pre-stable, the
# two releases ship together, and a scrub is rare enough that requiring the
# matching pair is the honest cost.
#
# 0.28.0 is safegit built on the framework release whose envelope declares
# `interface_version` 2: the document grew a `writes` member (strictcli
# effects contract 19.2/27.5), the write set of a command declaring
# `update_of`, null on every command that declares none -- which is every
# scrub command, so this flow ignores its value but must not choke on the key.
# The payload keys this flow reads are unchanged. Version 1 is not read: same
# reasoning as above, dual recognition of two envelope versions would be a
# compatibility shim for a pre-stable single-consumer coupling.
#
# The integration test harness builds exactly this version.
SAFEGIT_MIN_VERSION = (0, 28, 0)

# The strictcli envelope version safegit's --json speaks at that floor.
SAFEGIT_INTERFACE_VERSION = 2


def _save_step(path, data, step_name):
    """Record a completed step in the scrub result file."""
    data["completed_steps"].append(step_name)
    effects.atomic_write_text(path, json.dumps(data, indent=2))


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


def _safegit_floor_str():
    return ".".join(str(p) for p in SAFEGIT_MIN_VERSION)


def _parse_safegit_envelope(output):
    """Parse safegit's --json stdout as the framework's machine-mode envelope.

    In machine mode stdout carries exactly ONE document (strictcli effects
    contract 19.1): the envelope, whose `payload` member is safegit's own data
    and whose `preview` member carries the recorded effects of a dry run. The
    whole stream is therefore parsed with a plain `json.loads` -- there is no
    trailing would-do log to tolerate any more, and no partial decode that
    could silently swallow a second document.

    The envelope this flow reads is version 2 (contract 19.2): the member set
    is `interface_version`, `app`, `app_version`, `command`, `exit_code`,
    `payload`, `dry_run`, `writes`, `preview`, `preview_error`, `diagnostics`.
    `writes` is null on a scrub command (it names the write set of an update
    command, contract 27.5) and this flow does not read it.

    Anything that is not an envelope is refused by name: the pre-0.27.0 shape
    was safegit's own bare JSON object, and reading it as a payload would
    produce a scrub state file missing every key this flow needs. An envelope
    declaring any other `interface_version` -- version 1, the pre-0.28.0
    safegit -- is refused the same way, by name and with the floor to install.
    """
    text = output.strip()
    if not text:
        raise ValueError(
            "safegit --json produced no output at all; the scrub flow needs "
            f"safegit >= {_safegit_floor_str()}, whose machine mode always "
            "emits an envelope"
        )
    try:
        envelope = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"safegit --json output is not JSON: {exc}") from exc
    if not isinstance(envelope, dict) or "interface_version" not in envelope:
        raise ValueError(
            "safegit --json output is not a strictcli envelope (no "
            f"interface_version). The scrub flow needs safegit >= "
            f"{_safegit_floor_str()}; an older safegit prints its own JSON "
            "object instead, which this flow no longer reads."
        )
    if envelope["interface_version"] != SAFEGIT_INTERFACE_VERSION:
        raise ValueError(
            "safegit's envelope declares interface_version "
            f"{envelope['interface_version']!r}, which this rlsbl does not "
            f"know how to read (expected {SAFEGIT_INTERFACE_VERSION}); the "
            f"scrub flow needs safegit >= {_safegit_floor_str()}"
        )
    return envelope


def _build_safegit_args(flags, mode, remap_globs):
    """Build the safegit scrub argument list for the selected mode.

    ``remap_globs`` (from ``changelog_remap_globs``) is passed as repeatable
    ``--remap-shas-in`` flags in every mode: safegit rewrites full 40-hex
    commit hashes inside the glob-matched changelog files at EVERY commit of
    the rewritten history, so all historical versions -- including HEAD --
    stay self-consistent.

    ``--approve-consequential`` is explicit in every mode: safegit declares
    all three scrub modes ``consequential``, so each prompts before dispatch
    and ``--json`` does not answer that prompt. Running this command IS the
    consent; the force-push that follows is confirmed separately.

    The flag is placed BEFORE the command tokens. Anywhere-in-argv
    recognition is the current contract, but it is a recent amendment and the
    Go implementation acquired it later than the Python one; the pre-command
    position is the one every implementation and every version has always
    recognized, so it cannot break on a callee whose framework build lags.
    """
    if mode == "match":
        args = ["--approve-consequential", "scrub", "match", "--json"]
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
        args = ["--approve-consequential", "scrub", "file", "--json"]
        if flags.get("dry-run"):
            args.append("--dry-run")
        args.extend(["--from", flags["from-commit"]])
        args.extend(_remap_glob_args(remap_globs))
        args.extend(["--reason", flags["reason"]])
        args.append(flags["file"])
        return args

    # Recipe mode: positional recipe path, range flags, reason.
    args = ["--approve-consequential", "scrub", "run", "--json"]
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




def _print_dry_run_summary(mode, data):
    """Print a per-mode dry-run preview from safegit's preview payload.

    safegit has one result type per command now; the preview-only members are
    present exactly when they were measured, so a dry run's payload carries
    the match counts and none of the rewrite counts.

    `objects_matched` is the count of distinct objects the pattern matched --
    NOT `objects_scanned`, which counts what the scan walked. safegit reported
    only the latter under a name that read like the former until 0.27.0.
    """
    if mode == "match":
        total = data.get("total_matches", 0)
        blobs = data.get("blob_matches", 0)
        msgs = data.get("commit_matches", 0)
        tag_m = data.get("tag_matches", 0)
        matched = data.get("objects_matched", 0)
        scanned = data.get("objects_scanned", 0)
        est = data.get("estimated_commits", 0)
        print(
            f"Dry run (match): {total} matches ({blobs} blob, {msgs} "
            f"commit-message, {tag_m} tag) across {matched} matching objects "
            f"of {scanned} scanned; ~{est} commits would be rewritten."
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
    print(
        "  Changelog hashes are repaired, but a rewrite also invalidates "
        "release metadata that lives outside the commit graph: the remote's "
        "tags and the GitHub Releases attached to them.\n"
        "  If this rewrite was not driven by `rlsbl release scrub`, reconcile "
        "them too:\n"
        "    rlsbl release reconcile --dry-run   # plan\n"
        "    rlsbl release reconcile             # re-push moved tags, "
        "recreate their Releases"
    )
    return True


def _no_match_validate_and_repair(project_root, workspace_root, workspace_projects):
    """Changelog hash validation for a scrub that found NOTHING to rewrite.

    A no-match scrub is the one moment damage from a PRIOR crashed or
    direct scrub is still cheaply repairable: the safegit rewrite journal
    is at hand and the repair path is wired up right here. Exiting
    "nothing to do" without validating would let dangling hashes go
    unnoticed until a later `rlsbl check`, when the journal recovery is
    unreachable and the operator is pointed at manual amends.

    No rewrite happened on this run, so there is nothing to force-push:
    validate, repair from the journal when possible, COMMIT the repaired
    files, and hard-error naming anything that remains dangling.
    """
    all_changes_dirs = enumerate_changelog_dirs(
        str(project_root), workspace_root, workspace_projects=workspace_projects,
    )
    repo_root = str(workspace_root) if workspace_root else str(project_root)
    failures = validate_all_hashes_resolve(all_changes_dirs, repo_root=repo_root)
    if not failures:
        return

    # Repairs mutate changelog files, so take the same lock the main flow
    # holds while touching them.
    lock_dir = ".rlsbl-monorepo" if workspace_root else ".rlsbl"
    acquire_lock(lock_dir=lock_dir, project_root=repo_root)
    try:
        # Stand-in for scrub_data: the recovery helper only records the
        # repaired paths under "remapped_files".
        tracking = {}
        if _recover_from_rewrite_journal(all_changes_dirs, failures, tracking):
            failures = validate_all_hashes_resolve(
                all_changes_dirs, repo_root=repo_root,
            )
        repaired = tracking.get("remapped_files", [])

        if failures:
            print(
                "Error: the scrub found nothing to rewrite, but some "
                "changelog commit hashes do not resolve -- likely left "
                "behind by a previous crashed or direct scrub -- and the "
                "rewrite journal could not fix them:",
                file=sys.stderr,
            )
            for filepath, hashes in failures.items():
                print(f"  {filepath}: {', '.join(hashes)}", file=sys.stderr)
            if repaired:
                print(
                    f"  ({len(repaired)} file(s) were repaired from the "
                    f"rewrite journal but NOT committed; commit or revert "
                    f"them after fixing the remaining entries.)",
                    file=sys.stderr,
                )
            print(
                "Fix the entries (e.g. rlsbl changelog amend) and re-run.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Everything repaired: commit. No rewrite happened, so no
        # force-push follows -- the repair commit rides the next normal
        # push like any other commit.
        try:
            run("safegit", [
                "commit", "-m",
                "scrub: repair changelog hashes from rewrite journal",
                "--",
            ] + sorted(repaired))
        except Exception as e:
            print(
                f"Error: failed to commit journal-repaired changelog "
                f"files: {e}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"Committed {len(repaired)} journal-repaired changelog file(s)."
        )
    finally:
        release_lock()


def _require_cleanup_ok(scrub_data, scrub_result_path):
    """Hard gate on safegit's machine-readable post-rewrite cleanup status.

    The hash validation gate silently DEPENDS on old objects being pruned:
    a dangling changelog hash is only detectable because the pre-rewrite
    object is gone. When safegit reports ``cleanup_ok: false`` the old
    objects may still resolve, validation would falsely pass, and the flow
    would push a repository whose next prune breaks the changelog -- so the
    scrub stops here, BEFORE the commit step, with resume state intact.

    Remediation re-check: on a resumed run after the operator completed the
    prune manually, the recorded flag is stale. The gate re-checks REALITY
    (does any pre-rewrite object still exist?) and proceeds -- updating the
    persisted state -- when the prune is confirmed done.
    """
    if scrub_data.get("cleanup_ok") is not False:
        return

    old_shas = [
        old for old, new in scrub_data.get("rewrites", {}).items()
        if old != new
    ]
    old_head = scrub_data.get("old_head")
    new_head = scrub_data.get("new_head")
    # A tag-annotation-only rewrite maps every commit to itself (safegit
    # 0.22.0 emits all-identity commit maps for those) and old_head ==
    # new_head. The head object then legitimately exists forever, so
    # demanding its prune would deadlock the gate permanently.
    if old_head and old_head != new_head and old_head not in old_shas:
        old_shas.append(old_head)

    if not old_shas:
        print(
            "Note: safegit reported a failed post-rewrite cleanup, but this "
            "rewrite maps every commit to itself (e.g. a tag-annotation-only "
            "rewrite), so no pre-rewrite commit object needs pruning. "
            "Continuing."
        )
        scrub_data["cleanup_ok"] = True
        effects.atomic_write_text(scrub_result_path, json.dumps(scrub_data, indent=2))
        return

    still_present = []
    for sha in old_shas:
        try:
            run("git", ["cat-file", "-e", sha])
        except Exception:
            continue
        still_present.append(sha)

    if not still_present:
        print(
            "Note: safegit reported a failed post-rewrite cleanup, but no "
            "pre-rewrite object resolves any more -- the prune has been "
            "completed since. Continuing."
        )
        scrub_data["cleanup_ok"] = True
        effects.atomic_write_text(scrub_result_path, json.dumps(scrub_data, indent=2))
        return

    print(
        "Error: safegit reported cleanup_ok=false -- the post-rewrite "
        "cleanup (reflog expire / repack / prune) did not fully succeed:",
        file=sys.stderr,
    )
    for err in scrub_data.get("cleanup_errors") or []:
        print(f"  - {err}", file=sys.stderr)
    print(
        f"  {len(still_present)} pre-rewrite object(s) still present "
        f"(e.g. {still_present[0][:12]}).\n"
        f"The changelog hash validation depends on old objects being "
        f"pruned, so continuing would be unsafe. Investigate the errors "
        f"above, complete the prune (e.g. `git reflog expire --expire=now "
        f"--all && git gc --prune=now`), and re-run this command to "
        f"resume; {scrub_result_path} is kept.",
        file=sys.stderr,
    )
    sys.exit(1)


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
                effects.remove(p)
        else:
            with effects.open_write(p, "wb") as f:
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
        f"\nAborting before commit/push; {scrub_result_path} is kept. If "
        f"the diff is generation drift, run `rlsbl changelog generate`, "
        f"commit the result with `rlsbl commit`, and re-run this command "
        f"to resume.",
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
                remote_refs = _snapshot_remote_refs_impl(git=run)
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

        try:
            output = run("safegit", safegit_args, timeout=600)
        except Exception as e:
            print(f"Error: safegit scrub failed: {e}", file=sys.stderr)
            sys.exit(1)

        if effects.unsettled(output):
            # Under --dry-run the child is recorded, never forked (a preview
            # that forks is a preview that can be wrong about what it forked),
            # so safegit's own findings are not available here. The would-do
            # log the framework prints names the exact invocation, including
            # the --dry-run it carries; running that command directly is how
            # you see safegit's counts.
            print(
                "Preview recorded the safegit invocation instead of running "
                "it; run the command shown in the preview to see safegit's "
                "own match counts."
            )
            return

        # The refusals _parse_safegit_envelope raises are written for a human
        # (they name the safegit version this flow needs), so they are printed
        # as an error rather than allowed to surface as a traceback.
        try:
            envelope = _parse_safegit_envelope(output)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        scrub_data = envelope.get("payload")

        # A run that supplied no payload is a run that found nothing to
        # rewrite: safegit returns before it reports figures. The envelope is
        # always there now, so the absence is in the payload, not in stdout.
        if scrub_data is None:
            print("No matches found, nothing to do.")
            # Even a no-op scrub validates the changelog hashes (and can
            # repair prior damage from the rewrite journal) -- but never on
            # a dry run, which must not mutate anything.
            if not flags.get("dry-run"):
                _no_match_validate_and_repair(
                    project_root, ctx.workspace_root, workspace_projects,
                )
            return

        if flags.get("dry-run"):
            _print_dry_run_summary(mode, scrub_data)
            return

        # LEASE AUTHORITY: the ls-remote snapshot above queried the ACTUAL
        # remote before the rewrite. safegit's pre_rewrite_remotes is the
        # LOCAL remote-tracking snapshot (refs/remotes/*), which may be
        # stale -- e.g. no fetch since someone else pushed. The
        # --force-with-lease expectations therefore always come from the
        # ls-remote snapshot; pre_rewrite_remotes is cross-checked here
        # purely informationally.
        prefix = "refs/remotes/origin/"
        for refname, tracking_sha in (
            scrub_data.get("pre_rewrite_remotes") or {}
        ).items():
            if not refname.startswith(prefix):
                continue
            branch_ref = "refs/heads/" + refname[len(prefix):]
            actual_sha = (remote_refs or {}).get(branch_ref)
            if actual_sha is not None and actual_sha != tracking_sha:
                print(
                    f"Warning: safegit's pre_rewrite_remotes snapshot "
                    f"disagrees with the remote for {branch_ref}: local "
                    f"tracking ref had {tracking_sha[:12]}, origin had "
                    f"{actual_sha[:12]}. The local tracking state was "
                    f"stale; the force-push lease uses the value read "
                    f"from origin.",
                    file=sys.stderr,
                )

        # Save scrub-result.json for resume support
        scrub_data["completed_steps"] = []
        scrub_data["remote_refs"] = remote_refs or {}
        effects.makedirs(os.path.dirname(scrub_result_path), exist_ok=True)
        effects.atomic_write_text(scrub_result_path, json.dumps(scrub_data, indent=2))

    # -- Parse results --
    rewrites = scrub_data.get("rewrites", {})
    tags = scrub_data.get("tags", [])
    completed = set(scrub_data.get("completed_steps", []))

    if not rewrites:
        print("No matches found, nothing to do.")
        # Clean up scrub-result.json if it exists
        if os.path.exists(scrub_result_path):
            effects.remove(scrub_result_path)
        # Same validation/repair as the empty-stdout no-match path above.
        _no_match_validate_and_repair(
            project_root, ctx.workspace_root, workspace_projects,
        )
        return

    # -- Announcement, not a gate --
    # `release scrub` declares itself `consequential`, so strictcli confirmed
    # once before dispatch and --approve-consequential skips that one prompt.
    # The counts are still printed: they are only known here, after safegit
    # computed the rewrite, and the framework prompt cannot show them.
    if not resuming:
        print(f"{len(rewrites)} commits rewritten, {len(tags)} tags affected.")
        print("Force-pushing rewritten history.")

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
        # -- Cleanup gate: the validation below is only meaningful when the
        # pre-rewrite objects were actually pruned. Not a recorded step: it
        # re-runs on every resume until cleanup is confirmed done. --
        _require_cleanup_ok(scrub_data, scrub_result_path)

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
                effects.remove(validated)
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
                effects.makedirs(os.path.dirname(archive_path), exist_ok=True)
                archive = _build_scrub_archive(scrub_data, mode, flags["reason"])
                effects.atomic_write_text(archive_path, json.dumps(archive, indent=2))
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
            branch = get_current_branch(cwd=str(ctx.project_root))
            branch_ref = f"refs/heads/{branch}"
            # Target: the local branch tip (new head plus the metadata commit).
            try:
                branch_target = run("git", ["rev-parse", branch_ref])
            except Exception:
                branch_target = ""
            _push_ref_with_lease_impl(
                branch_ref, remote_refs.get(branch_ref), branch_target,
                timeout=push_timeout, git=run,
            )

            _save_step(scrub_result_path, scrub_data, "BRANCH_PUSHED")

        # -- Force-push tags (explicit lease each; never plain --force) --
        # Shared with `rlsbl release reconcile`, which performs exactly these
        # two steps after an out-of-band rewrite.
        if "TAGS_PUSHED" not in completed:
            push_rewritten_tags(
                tags, remote_refs, push_timeout=push_timeout, git=run,
            )
            _save_step(scrub_result_path, scrub_data, "TAGS_PUSHED")

        # -- Recreate GitHub Releases --
        if "RELEASES_UPDATED" not in completed:
            recreate_github_releases(
                tags, ctx=ctx, project_root=project_root,
                workspace_projects=workspace_projects,
                tag_prefix_index=tag_prefix_index,
                gh=run_gh, gh_installed=check_gh_installed,
                gh_auth=check_gh_auth,
                extract_entry=extract_changelog_entry,
            )
            _save_step(scrub_result_path, scrub_data, "RELEASES_UPDATED")

        # -- Cleanup and summary --
        if os.path.exists(scrub_result_path):
            effects.remove(scrub_result_path)

        releases_count = sum(
            1 for t in tags
            if parse_version_tag(t.get("refname", ""), mode=TagMode.FINAL_ONLY)
        )
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
