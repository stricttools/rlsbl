"""Undo command that reverts a release.

The undo flow is plan-driven: an :class:`UndoPlan` is computed UPFRONT (for
both the latest-release path and the ``--version`` non-latest path) before
anything is mutated. The plan enumerates the release commits to revert (found
by walking git history from the release tag toward the pre-release boundary),
companion tags to delete, whether a GitHub Release exists, and the registry
evidence verdict. ``--dry-run`` prints the plan and exits without touching
anything; a real run consumes the identical plan object.
"""

import dataclasses
import os
import re
import sys
import traceback
from types import SimpleNamespace

from ..changelog.files import get_changes_dir, read_coverage_unit, unfinalize_changeset_version, unfinalize_version
from ..changelog.generate import generate_changelog
from ..config import read_project_config
from ..evidence_gate import EvidenceKind, Verdict, run_evidence_gate, write_undo_audit
from ..member_context import resolve_member_context
from ..release_file import unfinalize_release_file
from ..targets import TARGETS, detect_targets
from ..utils import run, run_gh, check_gh_installed, check_gh_auth, get_push_timeout, get_current_branch, push_if_needed, is_clean_tree
from ..workspace import find_workspace_root, resolve_project

# Status constants for step results
OK = "OK"
FAILED = "FAILED"
SKIPPED = "SKIPPED"

# Release-commit message shapes. The version-bump commit is matched exactly
# against the expected message (tag string, or "<name>: release vX.Y.Z"); the
# remaining shapes are the chore commits the release flow emits after it. These
# are used to VALIDATE what the history walk finds, never as loop state.
_FINALIZE_CHANGELOG_RE = re.compile(r"^chore: finalize changelog for (.+)$")
_FINALIZE_RELEASE_FILE_RE = re.compile(r"^chore: finalize release file for (.+)$")
_CLEAN_STALE_RE = re.compile(r"^chore: clean \d+ stale batch exclusion\(s\) from config\.json$")
_REGEN_MD_RE = re.compile(r"^chore: regenerate (.+)\.md from archived release metadata$")


@dataclasses.dataclass
class UndoPlan:
    """A fully-computed description of what an undo will do.

    Computed once, before any mutation. ``--dry-run`` prints it and exits;
    a real run executes it. ``revert_shas`` is a list of ``(sha, subject)``
    tuples ordered newest-first (the order they must be reverted in so each
    revert applies cleanly).
    """

    tag: str
    is_latest: bool
    version: str
    revert_shas: list
    companion_tags: list
    github_release_exists: bool
    gate_result: object
    project_path: str
    ws_root: object
    releasable_name: object
    monorepo_name: object
    monorepo_project_path: object
    audit_dir: str
    captured_finalize_changelog: bool = False
    captured_finalize_release_file: bool = False


def _resolve_undo_changelog_paths(project_path, ws_root, releasable_name):
    """Resolve the changes dir, regenerate callable, and git-add paths for
    changelog restoration -- releasable-aware.

    In explicit releasable mode the finalized JSONL lives in the releasable's
    changes dir and the canonical CHANGELOG.md in the releasable dir (plus
    the combined root CHANGELOG.md); otherwise everything is per-project.
    """
    if releasable_name and ws_root:
        from ..changelog.home import (
            generate_workspace_changelog,
            get_changelog_home,
            get_workspace_changelog_path,
        )
        from ..release_file import get_releases_dir
        from ..workspace import get_releasable_changes_dir, get_releasable_dir

        rel_dir = get_releasable_dir(str(ws_root), releasable_name)
        changes_dir = get_releasable_changes_dir(str(ws_root), releasable_name)
        canonical = get_changelog_home(project_path, releasable_dir=rel_dir)

        def regenerate():
            generate_changelog(
                project_path,
                changes_dir_override=changes_dir,
                changelog_output_path=canonical,
                releases_dir_override=get_releases_dir(
                    project_path, releasable_dir=rel_dir,
                ),
            )
            generate_workspace_changelog(str(ws_root))

        add_paths = [
            changes_dir, canonical, get_workspace_changelog_path(str(ws_root)),
        ]
        return changes_dir, regenerate, add_paths

    changes_dir = get_changes_dir(project_path)

    def regenerate():
        generate_changelog(project_path)

    add_paths = [changes_dir, os.path.join(project_path, "CHANGELOG.md")]
    return changes_dir, regenerate, add_paths


def _resolve_undo_releases_dir(project_path, ws_root, releasable_name):
    """Resolve the releases dir holding the release-file family -- releasable-aware.

    In explicit releasable mode the archived v{x}.toml (and unreleased.toml)
    live under the releasable's own releases dir; otherwise per-project.
    """
    from ..release_file import get_releases_dir

    rel_dir = None
    if releasable_name and ws_root:
        from ..workspace import get_releasable_dir
        rel_dir = get_releasable_dir(str(ws_root), releasable_name)
    return get_releases_dir(project_path, releasable_dir=rel_dir)


def _clear_release_state(project_path, ws_root):
    """Clear any in-progress release state after a successful rollback.

    A rolled-back release's preserved in-progress.json is meaningless and
    would hard-block the next `rlsbl release run`.
    """
    from .release.release_state import (
        clear_release_state,
        get_state_path,
        resolve_releasable_dir,
    )

    rel_dir = resolve_releasable_dir(project_path, ws_root) if ws_root else None
    clear_release_state(get_state_path(project_path, releasable_dir=rel_dir))


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


# ---------------------------------------------------------------------------
# Context + plan computation (no mutations)
# ---------------------------------------------------------------------------


def _resolve_context(ctx):
    """Detect the monorepo / releasable context shared by both undo paths."""
    monorepo_name = None
    monorepo_project_path = None
    releasable_name = None
    releasable_config_dir = None
    releasables = None
    start_path = str(ctx.project_root)
    ws_root = find_workspace_root(start_path)
    if ws_root:
        project = resolve_project(ws_root, start_path)
        if project is None:
            print("Error: current directory is inside a monorepo but not inside any project.", file=sys.stderr)
            sys.exit(1)
        monorepo_name = project["name"]
        monorepo_project_path = project["path"]
        from ..targets import resolve_releasable_config_dir
        releasable_config_dir = resolve_releasable_config_dir(project, ws_root)

        from ..workspace import is_explicit_mode, load_releasables, load_workspace as _load_ws, resolve_releasable_for_project
        if is_explicit_mode(ws_root):
            ws_projects = _load_ws(ws_root)
            releasables = load_releasables(ws_root, ws_projects)
            rel = resolve_releasable_for_project(project, releasables)
            if rel:
                releasable_name = rel.name

    project_path = os.path.join(ws_root, monorepo_project_path) if monorepo_name else str(ctx.project_root or ".")
    return SimpleNamespace(
        start_path=start_path,
        ws_root=ws_root,
        monorepo_name=monorepo_name,
        monorepo_project_path=monorepo_project_path,
        releasable_name=releasable_name,
        releasable_config_dir=releasable_config_dir,
        releasables=releasables,
        project_path=project_path,
    )


def _find_latest_tag(uc):
    """Resolve the latest release tag (scoped to releasable/project in monorepos)."""
    if uc.releasable_name and uc.ws_root:
        from ..commands.release.validate import _releasable_tag_glob
        rel = next(r for r in uc.releasables if r.name == uc.releasable_name)
        match_pattern = _releasable_tag_glob(rel.tag_format, uc.releasable_name)
    elif uc.monorepo_name:
        abs_project_dir = os.path.join(uc.ws_root, uc.monorepo_project_path)
        target_entries = detect_targets(abs_project_dir)
        if target_entries:
            target = TARGETS[target_entries[0].name]
            match_pattern = target.monorepo_tag_glob(uc.monorepo_name, path=uc.monorepo_project_path)
        else:
            match_pattern = f"{uc.monorepo_name}@v*"
    else:
        match_pattern = "v*"
    try:
        return run("git", ["describe", "--tags", "--abbrev=0", "--match", match_pattern]).strip()
    except Exception:
        print("Error: no tags found. Nothing to undo.", file=sys.stderr)
        sys.exit(1)


def _version_and_msg(uc, tag):
    """Return (bare_version, expected_version_bump_message) for a tag."""
    if uc.releasable_name or uc.monorepo_name:
        m = re.search(r"v(\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?)$", tag)
        version_part = f"v{m.group(1)}" if m else tag
        name = uc.releasable_name or uc.monorepo_name
        return version_part.lstrip("v"), f"{name}: release {version_part}"
    return tag.lstrip("v"), tag


def _build_tag_from_version(uc, version):
    """Build the release tag string for a given version (non-latest path)."""
    member = resolve_member_context(
        uc.start_path, releasable_config_dir=uc.releasable_config_dir,
    )
    entries = member.targets
    if not entries:
        return f"v{version}"
    target_obj = TARGETS[entries[0].name]
    if uc.releasable_name and uc.ws_root:
        from ..workspace import load_releasables as _lr, load_workspace as _lw, is_explicit_mode as _ie
        if _ie(uc.ws_root):
            _rels = _lr(uc.ws_root, _lw(uc.ws_root))
            _rel = next((r for r in _rels if r.name == uc.releasable_name), None)
            if _rel and _rel.tag_format:
                from .release.validate import _format_releasable_tag
                return _format_releasable_tag(_rel.tag_format, uc.releasable_name, version)
        return target_obj.monorepo_tag_format(uc.monorepo_name, version, path=uc.monorepo_project_path)
    if uc.monorepo_name:
        return target_obj.monorepo_tag_format(uc.monorepo_name, version, path=uc.monorepo_project_path)
    return target_obj.tag_format(version)


def _classify_release_commit(subject, expected_msg):
    """Return the shape of a release commit, or None if it is not one."""
    if subject == expected_msg:
        return "version_bump"
    if _FINALIZE_CHANGELOG_RE.match(subject):
        return "finalize_changelog"
    if _FINALIZE_RELEASE_FILE_RE.match(subject):
        return "finalize_release_file"
    if _CLEAN_STALE_RE.match(subject):
        return "clean_stale"
    if _REGEN_MD_RE.match(subject):
        return "regenerate_md"
    return None


def _walk_release_commits(tag, expected_msg):
    """Walk history from the tag's commit toward the pre-release boundary.

    Collects contiguous release-shaped commits newest-first. Message-shape
    matching only VALIDATES what the walk finds; the walk itself (not the
    shape of the current HEAD) drives iteration, so it is immune to the
    "revert changes HEAD's subject" bug. Stops at the first non-release-shaped
    commit (the boundary) or after collecting the version-bump commit (the
    oldest commit of a release -- this keeps back-to-back releases from
    bleeding into each other).

    Returns ``(revert_shas, captured_finalize_changelog,
    captured_finalize_release_file)``.
    """
    try:
        sha = run("git", ["rev-list", "-n", "1", tag]).strip()
    except Exception:
        return [], False, False

    collected = []
    captured_cl = False
    captured_rf = False
    seen = set()
    while sha and sha not in seen:
        seen.add(sha)
        try:
            subject = run("git", ["log", "-1", "--format=%s", sha]).strip()
        except Exception:
            break
        shape = _classify_release_commit(subject, expected_msg)
        if shape is None:
            break  # pre-release boundary
        collected.append((sha, subject))
        if shape == "finalize_changelog":
            captured_cl = True
        elif shape == "finalize_release_file":
            captured_rf = True
        if shape == "version_bump":
            break  # oldest release commit reached
        try:
            sha = run("git", ["rev-parse", f"{sha}^"]).strip()
        except Exception:
            break
    return collected, captured_cl, captured_rf


def _plan_companion_tags(uc, tag, version):
    """Enumerate companion tags to delete (both paths, releasable mode only)."""
    if not (uc.releasable_name and uc.ws_root):
        return []
    try:
        from ..commands.release.execute import collect_companion_tags
        from ..workspace import get_releasable_dir, load_workspace, members_of

        ws_projects = load_workspace(uc.ws_root)
        member_paths = [p["path"] for p in members_of(uc.releasable_name, ws_projects)]
        rel_cfg_dir = get_releasable_dir(str(uc.ws_root), uc.releasable_name)
        return collect_companion_tags(
            member_paths, uc.ws_root, version, tag,
            releasable_config_dir=rel_cfg_dir,
        )
    except Exception:
        traceback.print_exc()
        return []


def _gh_release_exists(tag, ctx):
    try:
        run_gh(["release", "view", tag], config=ctx.config)
        return True
    except Exception:
        return False


def _plan_gate(uc, ctx, version):
    """Run the registry evidence gate for the release (both paths)."""
    member = resolve_member_context(
        uc.start_path, releasable_config_dir=uc.releasable_config_dir,
    )
    try:
        entries = member.targets
    except Exception:
        entries = []
    target_objects = [TARGETS[e.name] for e in entries] if entries else []
    return run_evidence_gate(target_objects, uc.start_path, version, ctx)


def _audit_dir(uc):
    if uc.releasable_name and uc.ws_root:
        from ..workspace import get_releasable_dir
        return get_releasable_dir(str(uc.ws_root), uc.releasable_name)
    return os.path.join(uc.project_path, ".rlsbl")


def _build_plan(uc, flags, ctx):
    """Compute the full UndoPlan for either path, before any mutation."""
    version_flag = flags.get("version")
    is_latest = not version_flag
    if is_latest:
        tag = _find_latest_tag(uc)
        version, expected_msg = _version_and_msg(uc, tag)
        revert_shas, cap_cl, cap_rf = _walk_release_commits(tag, expected_msg)
        # Completeness guard: if the walk collected release-shaped commits
        # but never reached the version-bump commit (e.g. a foreign commit
        # was interleaved and stopped the walk early), refuse the undo
        # rather than silently performing a partial revert.
        if revert_shas:
            has_version_bump = any(
                _classify_release_commit(subj, expected_msg) == "version_bump"
                for _, subj in revert_shas
            )
            if not has_version_bump:
                # Identify the commit that stopped the walk (one parent past
                # the oldest collected commit).
                oldest_sha = revert_shas[-1][0]
                try:
                    boundary_sha = run("git", ["rev-parse", f"{oldest_sha}^"]).strip()
                    boundary_subject = run("git", ["log", "-1", "--format=%s", boundary_sha]).strip()
                    boundary_desc = f"{boundary_sha[:10]} ({boundary_subject})"
                except Exception:
                    boundary_desc = "unknown"
                print(
                    f"Error: release commit walk for {tag} collected "
                    f"{len(revert_shas)} commit(s) but never reached the "
                    f"version bump commit. An unexpected commit stopped the "
                    f"walk: {boundary_desc}. Refusing to partially undo.",
                    file=sys.stderr,
                )
                sys.exit(1)
    else:
        version = version_flag.lstrip("v")
        tag = _build_tag_from_version(uc, version)
        revert_shas, cap_cl, cap_rf = [], False, False

    return UndoPlan(
        tag=tag,
        is_latest=is_latest,
        version=version,
        revert_shas=revert_shas,
        companion_tags=_plan_companion_tags(uc, tag, version),
        github_release_exists=_gh_release_exists(tag, ctx),
        gate_result=_plan_gate(uc, ctx, version),
        project_path=uc.project_path,
        ws_root=uc.ws_root,
        releasable_name=uc.releasable_name,
        monorepo_name=uc.monorepo_name,
        monorepo_project_path=uc.monorepo_project_path,
        audit_dir=_audit_dir(uc),
        captured_finalize_changelog=cap_cl,
        captured_finalize_release_file=cap_rf,
    )


# ---------------------------------------------------------------------------
# Gate enforcement + plan display
# ---------------------------------------------------------------------------


def _enforce_gate(plan):
    """Refuse the undo when the evidence gate did not clear.

    PUBLISHED -> route to `release yank` / `release deprecate` (no bypass).
    INCONCLUSIVE / all-inconclusive -> hard block (same semantics as the
    non-latest path). Applies to BOTH the latest and non-latest paths.
    """
    gr = plan.gate_result
    if gr is None or gr.verdict == Verdict.CLEARED:
        return
    has_published = any(e.kind == EvidenceKind.PUBLISHED for e in gr.evidence)
    print(f"Error: cannot undo {plan.tag} -- {gr.reason}", file=sys.stderr)
    for e in gr.evidence:
        print(f"  {e.source}/{e.target}: {e.kind.value} -- {e.message}", file=sys.stderr)
    if has_published:
        v = plan.version
        print(
            "\nThis release appears to be PUBLISHED; undoing it is unsafe. "
            "Use a registry-aware removal instead:",
            file=sys.stderr,
        )
        print(f"  rlsbl release yank {v}       # remove it from registries", file=sys.stderr)
        print(f"  rlsbl release deprecate {v}  # mark it deprecated in place", file=sys.stderr)
    sys.exit(1)


def _print_plan(plan):
    kind = "latest" if plan.is_latest else "non-latest"
    print(f"Undo plan for {plan.tag} ({kind} release):")
    if plan.github_release_exists:
        print(f"  - Delete the GitHub Release for {plan.tag}")
    else:
        print(f"  - GitHub Release for {plan.tag}: none found (skip)")
    print(f"  - Delete git tag {plan.tag} (local + remote)")
    for ct in plan.companion_tags:
        print(f"  - Delete companion tag {ct} (local + remote)")
    if plan.revert_shas:
        print(f"  - Revert {len(plan.revert_shas)} release commit(s) (newest first):")
        for sha, subject in plan.revert_shas:
            print(f"      {sha[:10]}  {subject}")
    elif plan.is_latest:
        print("  - No release commits found to revert")
    else:
        print(f"  - Un-finalize changelog for {plan.version} (no commit revert -- history moved on)")
    if plan.gate_result is not None:
        print("  Evidence:")
        for e in plan.gate_result.evidence:
            print(f"    {e.source}/{e.target}: {e.kind.value} -- {e.message}")
    print("  - Write undo audit record")


# ---------------------------------------------------------------------------
# Execution (consumes the plan)
# ---------------------------------------------------------------------------


def _delete_tag(tag, ctx, results):
    try:
        undo_push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
        run("git", ["push", "origin", f":{tag}"], timeout=get_push_timeout(ctx.config), env=undo_push_env)
        results.append(("Delete remote tag", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Delete remote tag", FAILED, f"git push origin :{tag}"))
    try:
        run("git", ["tag", "-d", tag])
        results.append(("Delete local tag", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Delete local tag", FAILED, f"git tag -d {tag}"))


def _restore_changelog(plan, uc, results):
    """Reconcile CHANGELOG.md with the restored changelog state.

    Runs on BOTH paths. It first un-finalizes the version's JSONL (renaming
    ``{version}.jsonl`` back to ``unreleased.jsonl``; a no-op when a revert
    already restored it, and the repair when it didn't -- e.g. the non-latest
    path or a finalize commit outside the collected release commits). It then
    ALWAYS regenerates CHANGELOG.md so the generated file matches the restored
    ``unreleased.jsonl`` -- reverting the finalize commit restores the JSONL
    but leaves CHANGELOG.md showing the released version rather than
    ``## Unreleased``. Commits only when git reports an actual change, so it is
    idempotent.
    """
    try:
        changes_dir, regenerate_changelog, add_paths = _resolve_undo_changelog_paths(
            plan.project_path, uc.ws_root, plan.releasable_name,
        )
        _cfg = read_project_config(plan.project_path)
        if read_coverage_unit(_cfg) == "changeset-file":
            unfinalize_changeset_version(changes_dir, plan.version)
        else:
            unfinalize_version(changes_dir, plan.version)
        regenerate_changelog()
        status = run("git", ["status", "--porcelain", "--", *add_paths]).strip()
        if status:
            run("git", ["add", *add_paths])
            run("git", ["commit", "-m", f"chore: restore changelog after undo of {plan.tag}"])
            results.append(("Restore changelog", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Restore changelog", FAILED, "manually restore the changes dir and regenerate CHANGELOG.md"))


def _restore_release_file(plan, uc, results):
    """Repair the release file when its finalize commit was NOT reverted.

    ``unfinalize_release_file`` is a no-op when the file is already restored
    (e.g. the finalize commit was reverted), so this is safe on both paths.
    """
    try:
        releases_dir = _resolve_undo_releases_dir(
            plan.project_path, uc.ws_root, plan.releasable_name,
        )
        changed = unfinalize_release_file(releases_dir, plan.version)
        if changed:
            run("git", ["add", releases_dir])
            run("git", ["commit", "-m", f"chore: restore release file after undo of {plan.tag}"])
            results.append(("Restore release file", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Restore release file", FAILED, f"manually restore unreleased.toml from v{plan.version}.toml in the releases dir"))


def _execute_plan(plan, uc, flags, ctx):
    """Execute a computed UndoPlan. Writes the audit journal BEFORE any deletion."""
    results = []
    tag = plan.tag

    # 1. Audit journal -- written and committed BEFORE any destructive action
    #    on EVERY path, capturing the full plan (tag, target SHAs, evidence).
    try:
        audit_path = write_undo_audit(
            plan.audit_dir, plan.version, tag, plan.gate_result,
            operator_context={
                "is_latest": plan.is_latest,
                "revert_shas": [s for s, _ in plan.revert_shas],
                "companion_tags": list(plan.companion_tags),
                "github_release_deleted": plan.github_release_exists,
            },
        )
        run("git", ["add", audit_path])
        try:
            run("git", ["commit", "-m", f"chore: audit record for undo of {tag}"])
        except Exception:
            pass  # nothing to commit (audit file unchanged) is acceptable
        results.append(("Write audit record", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Write audit record", FAILED, "manually write undo-audit.json"))

    # 2. Delete the GitHub Release
    if plan.github_release_exists:
        try:
            run_gh(["release", "delete", tag, "--yes"], config=ctx.config)
            results.append(("Delete GitHub Release", OK, "-"))
        except Exception:
            traceback.print_exc()
            results.append(("Delete GitHub Release", FAILED, f"gh release delete {tag} --yes"))
    else:
        results.append(("Delete GitHub Release", SKIPPED, "no GitHub Release found"))

    # 3. Delete the primary tag (remote + local)
    _delete_tag(tag, ctx, results)

    # 4. Delete companion tags (both paths)
    if plan.companion_tags:
        for ct in plan.companion_tags:
            try:
                undo_push_env_ct = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
                run("git", ["push", "origin", f":{ct}"], timeout=get_push_timeout(ctx.config), env=undo_push_env_ct)
            except Exception:
                pass  # remote tag may not exist
            try:
                run("git", ["tag", "-d", ct])
            except Exception:
                pass  # local tag may not exist
        results.append(("Delete companion tags", OK, f"deleted {len(plan.companion_tags)} companion tag(s)"))

    # 5. Revert release commits, newest-first, targeting the collected SHAs
    #    explicitly (never re-deriving from HEAD). Newest-first so each revert
    #    applies cleanly against the working tree.
    if plan.revert_shas:
        try:
            for sha, _subject in plan.revert_shas:
                run("git", ["revert", "--no-edit", sha])
            results.append(("Revert commits", OK, f"reverted {len(plan.revert_shas)} commit(s)"))
        except Exception:
            traceback.print_exc()
            results.append(("Revert commits", FAILED, "git revert --no-edit <sha>"))

    # 6. Reconcile CHANGELOG.md with the restored JSONL (both paths; idempotent).
    #    Skipped only when the release has no changes dir at all.
    _restore_changelog(plan, uc, results)
    # Release-file repair: a no-op when the finalize-release-file commit was
    # reverted (its rename is already undone); the repair when it wasn't
    # (non-latest path, or a finalize commit outside the collected commits).
    if not plan.captured_finalize_release_file:
        _restore_release_file(plan, uc, results)

    # 7. Push the branch (revert + restore + audit commits) to remote
    should_push = flags.get("yes")
    if not should_push:
        try:
            answer = input("\nPush changes to remote? [y/N] ").strip().lower()
            should_push = answer == "y"
        except (EOFError, KeyboardInterrupt):
            should_push = False
    if should_push:
        try:
            branch = get_current_branch()
            push_env = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
            push_if_needed(branch, env=push_env, config=ctx.config)
            results.append(("Push", OK, "-"))
        except Exception:
            traceback.print_exc()
            results.append(("Push", FAILED, "git push"))

    # 8. Summary + state cleanup
    has_failure = any(status == FAILED for _, status, _ in results)
    if not has_failure:
        # A rolled-back release's preserved in-progress.json is meaningless and
        # would hard-block the next `rlsbl release run`.
        _clear_release_state(plan.project_path, uc.ws_root)
        print(f"\nUndo of {tag} complete.")
    else:
        _print_summary(results)


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

    uc = _resolve_context(ctx)
    plan = _build_plan(uc, flags, ctx)

    # Refuse the operation if the evidence gate did not clear (applies to both
    # paths, dry-run included -- a forbidden operation cannot be previewed).
    _enforce_gate(plan)

    _print_plan(plan)

    if flags.get("dry_run"):
        print("\n[dry-run] No changes were made.")
        return

    if not flags.get("yes"):
        try:
            answer = input("\nThis is destructive. Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    _execute_plan(plan, uc, flags, ctx)
