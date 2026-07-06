"""Undo command that reverts the last release by deleting the GitHub Release, removing the git tag, and reverting the version bump commit."""

import os
import re
import sys
import traceback

from ..changelog.files import get_changes_dir, unfinalize_changeset_version, unfinalize_version
from ..changelog.generate import generate_changelog
from ..config import read_project_config
from ..release_file import unfinalize_release_file
from ..targets import TARGETS, detect_targets
from ..utils import run, run_gh, check_gh_installed, check_gh_auth, get_push_timeout, get_current_branch, push_if_needed, is_clean_tree
from ..workspace import find_workspace_root, resolve_project

# Status constants for step results
OK = "OK"
FAILED = "FAILED"
SKIPPED = "SKIPPED"


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

    # Non-latest undo: --version flag triggers the stillborn undo path
    version_flag = flags.get("version")
    if version_flag:
        _run_non_latest_undo(version_flag, flags, ctx=ctx)
        return

    # Monorepo detection
    monorepo_name = None
    monorepo_project_path = None
    releasable_name = None
    start_path = str(ctx.project_root)
    ws_root = find_workspace_root(start_path)
    if ws_root:
        project = resolve_project(ws_root, start_path)
        if project is None:
            print("Error: current directory is inside a monorepo but not inside any project.", file=sys.stderr)
            sys.exit(1)
        monorepo_name = project["name"]
        monorepo_project_path = project["path"]

        # Detect explicit releasable mode
        from ..workspace import is_explicit_mode, load_releasables, load_workspace as _load_ws, resolve_releasable_for_project
        if is_explicit_mode(ws_root):
            ws_projects = _load_ws(ws_root)
            releasables = load_releasables(ws_root, ws_projects)
            rel = resolve_releasable_for_project(project, releasables)
            if rel:
                releasable_name = rel.name

    # Find the latest tag (scoped to releasable or project in monorepo mode)
    if releasable_name and ws_root:
        from ..commands.release.validate import _releasable_tag_glob
        rel = next(r for r in releasables if r.name == releasable_name)
        match_pattern = _releasable_tag_glob(rel.tag_format, releasable_name)
    elif monorepo_name:
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
        run_gh(["release", "view", tag], config=ctx.config)
    except Exception:
        results.append(("Delete GitHub Release", SKIPPED, "no GitHub Release found"))
    else:
        try:
            run_gh(["release", "delete", tag, "--yes"], config=ctx.config)
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

    # Delete companion tags (e.g. Go module proxy tags in releasable mode)
    if releasable_name and ws_root:
        try:
            from ..commands.release.execute import collect_companion_tags
            from ..workspace import get_releasable_dir as _get_rel_dir2
            from ..workspace import load_workspace as _load_ws2, members_of as _members_of2

            _version_for_companion = re.search(r"v(\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?)$", tag)
            if _version_for_companion:
                _ver = _version_for_companion.group(1)
                _ws_projects2 = _load_ws2(ws_root)
                _member_projs2 = _members_of2(releasable_name, _ws_projects2)
                _member_paths2 = [p["path"] for p in _member_projs2]
                _rel_cfg_dir2 = _get_rel_dir2(str(ws_root), releasable_name)
                _companion_tags = collect_companion_tags(
                    _member_paths2, ws_root, _ver, tag,
                    releasable_config_dir=_rel_cfg_dir2,
                )
                for ctag in _companion_tags:
                    try:
                        undo_push_env_ct = {**os.environ, "RLSBL_RELEASE_PUSH": "1"}
                        run("git", ["push", "origin", f":{ctag}"], timeout=get_push_timeout(ctx.config), env=undo_push_env_ct)
                    except Exception:
                        pass  # Remote tag may not exist
                    try:
                        run("git", ["tag", "-d", ctag])
                    except Exception:
                        pass  # Local tag may not exist
                if _companion_tags:
                    results.append(("Delete companion tags", OK, f"deleted {len(_companion_tags)} companion tag(s)"))
        except Exception:
            traceback.print_exc()
            results.append(("Delete companion tags", FAILED, "manually delete Go companion tags"))

    # Revert release commits (should be HEAD, or HEAD + HEAD~1 for two-commit pattern)
    # In explicit releasable mode, commit message is "<releasable>: release v<version>"
    # In implicit monorepo mode, commit message is "<project>: release v<version>"
    # In standalone mode, commit message is the tag string (e.g., "v1.2.3")
    if releasable_name:
        _version_match = re.search(r"v(\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?)$", tag)
        version_part = f"v{_version_match.group(1)}" if _version_match else tag
        expected_msg = f"{releasable_name}: release {version_part}"
    elif monorepo_name:
        # Extract version from tag: handles both name@v1.2.3 and path/v1.2.3
        _version_match = re.search(r"v(\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?)$", tag)
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
    # (releasable-aware: the finalized JSONL and canonical CHANGELOG.md live
    # at the releasable level in explicit releasable mode)
    if changelog_finalize_reverted:
        try:
            changes_dir, regenerate_changelog, changelog_add_paths = (
                _resolve_undo_changelog_paths(project_path, ws_root, releasable_name)
            )
            # Pick the right unfinalize based on coverage mode
            _cfg = read_project_config(project_path)
            if _cfg.get("coverage_unit") == "changeset-file":
                unfinalize_changeset_version(changes_dir, bare_version)
            else:
                unfinalize_version(changes_dir, bare_version)
            regenerate_changelog()
            # Commit the restored changelog files
            run("git", ["add", *changelog_add_paths])
            run("git", ["commit", "-m", f"chore: restore changelog after undo of {tag}"])
            results.append(("Restore changelog", OK, "-"))
        except Exception:
            traceback.print_exc()
            results.append(("Restore changelog", FAILED, "manually restore the changes dir and regenerate CHANGELOG.md"))

    # Restore the release file (inverse of release-file finalization). When
    # the finalize commit was reverted above, git already restored the files
    # and this is a no-op; when it wasn't at HEAD (e.g., post-release hooks
    # added commits), the finalized read-only vX.Y.Z.toml and the fresh empty
    # unreleased.toml are still on disk and must be repaired directly.
    # Releasable-aware: the archive lives under the releasable's releases
    # dir in explicit releasable mode.
    try:
        releases_dir = _resolve_undo_releases_dir(
            project_path, ws_root, releasable_name,
        )
        release_file_changed = unfinalize_release_file(releases_dir, bare_version)
        if release_file_changed:
            run("git", ["add", releases_dir])
            run("git", ["commit", "-m", f"chore: restore release file after undo of {tag}"])
            results.append(("Restore release file", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Restore release file", FAILED, f"manually restore unreleased.toml from v{bare_version}.toml in the releases dir"))

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
    if not has_failure:
        # Clear any preserved in-progress release state (e.g. from a
        # fatal-failure release that was just undone) -- leaving it in
        # place would hard-block the next `rlsbl release run`.
        _clear_release_state(project_path, ws_root)
    if has_failure:
        _print_summary(results)
    else:
        print("\nUndo complete.")


def _run_non_latest_undo(version_str, flags, *, ctx):
    """Undo a non-latest release that is provably unpublished.

    Uses the layered evidence gate to determine whether the release was
    published. Only proceeds if the gate clears.

    For non-latest releases:
    - Delete GitHub Release + tag only (no commit revert -- history moved on)
    - Un-finalize changelog (rename x.y.z.jsonl back to unreleased.jsonl)
    - Write undo-audit.json with evidence
    """
    from ..evidence_gate import Verdict, run_evidence_gate, write_undo_audit
    from ..member_context import resolve_member_context
    from ..targets import resolve_releasable_config_dir

    version = version_str.lstrip("v")

    # Monorepo detection (same as latest undo)
    monorepo_name = None
    monorepo_project_path = None
    releasable_name = None
    releasable_config_dir = None
    start_path = str(ctx.project_root)
    ws_root = find_workspace_root(start_path)
    if ws_root:
        project = resolve_project(ws_root, start_path)
        if project is None:
            print("Error: current directory is inside a monorepo but not inside any project.", file=sys.stderr)
            sys.exit(1)
        monorepo_name = project["name"]
        monorepo_project_path = project["path"]
        releasable_config_dir = resolve_releasable_config_dir(project, ws_root)

        from ..workspace import is_explicit_mode, load_releasables, load_workspace as _load_ws, resolve_releasable_for_project
        if is_explicit_mode(ws_root):
            ws_projects = _load_ws(ws_root)
            releasables = load_releasables(ws_root, ws_projects)
            rel = resolve_releasable_for_project(project, releasables)
            if rel:
                releasable_name = rel.name

    # Resolve targets and build tag
    project_dir = start_path
    member = resolve_member_context(
        project_dir, releasable_config_dir=releasable_config_dir,
    )
    entries = member.targets
    if entries:
        target_obj = TARGETS[entries[0].name]
        if releasable_name and ws_root:
            from ..workspace import load_releasables as _lr, load_workspace as _lw, is_explicit_mode as _ie
            if _ie(ws_root):
                _ws_p = _lw(ws_root)
                _rels = _lr(ws_root, _ws_p)
                _rel = next((r for r in _rels if r.name == releasable_name), None)
                if _rel and _rel.tag_format:
                    from .release.validate import _format_releasable_tag
                    tag = _format_releasable_tag(_rel.tag_format, releasable_name, version)
                else:
                    tag = target_obj.monorepo_tag_format(monorepo_name, version, path=monorepo_project_path)
            else:
                tag = target_obj.monorepo_tag_format(monorepo_name, version, path=monorepo_project_path)
        elif monorepo_name:
            tag = target_obj.monorepo_tag_format(monorepo_name, version, path=monorepo_project_path)
        else:
            tag = target_obj.tag_format(version)
    else:
        tag = f"v{version}"

    project_path = os.path.join(ws_root, monorepo_project_path) if monorepo_name else str(ctx.project_root or ".")

    # Run evidence gate
    target_objects = [TARGETS[e.name] for e in entries] if entries else []
    gate_result = run_evidence_gate(target_objects, project_dir, version, ctx)

    if gate_result.verdict == Verdict.BLOCKED:
        print(f"Error: cannot undo {tag} -- {gate_result.reason}", file=sys.stderr)
        for e in gate_result.evidence:
            print(f"  {e.source}/{e.target}: {e.kind.value} -- {e.message}", file=sys.stderr)
        sys.exit(1)

    # Display plan
    print(f"Non-latest undo of {tag} (provably unpublished):")
    print(f"  - Delete git tag {tag} (local + remote)")
    print(f"  - Delete the GitHub Release for {tag}")
    print(f"  - Un-finalize changelog for {version}")
    print(f"\nEvidence:")
    for e in gate_result.evidence:
        print(f"  {e.source}/{e.target}: {e.kind.value} -- {e.message}")

    if not flags.get("yes"):
        try:
            answer = input("\nThis is destructive. Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    results = []

    # Delete GitHub Release
    try:
        run_gh(["release", "view", tag], config=ctx.config)
    except Exception:
        results.append(("Delete GitHub Release", SKIPPED, "no GitHub Release found"))
    else:
        try:
            run_gh(["release", "delete", tag, "--yes"], config=ctx.config)
            results.append(("Delete GitHub Release", OK, "-"))
        except Exception:
            traceback.print_exc()
            results.append(("Delete GitHub Release", FAILED, f"gh release delete {tag} --yes"))

    # Delete remote tag
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

    # Un-finalize changelog
    try:
        changes_dir, regenerate_changelog, changelog_add_paths = (
            _resolve_undo_changelog_paths(project_path, ws_root, releasable_name)
        )
        _cfg = read_project_config(project_path)
        if _cfg.get("coverage_unit") == "changeset-file":
            changed = unfinalize_changeset_version(changes_dir, version)
        else:
            changed = unfinalize_version(changes_dir, version)
        if changed:
            regenerate_changelog()
            run("git", ["add", *changelog_add_paths])
            run("git", ["commit", "-m", f"chore: un-finalize changelog for {tag} (non-latest undo)"])
            results.append(("Un-finalize changelog", OK, "-"))
        else:
            results.append(("Un-finalize changelog", SKIPPED, f"no {version}.jsonl found"))
    except Exception:
        traceback.print_exc()
        results.append(("Un-finalize changelog", FAILED, "manually restore changelog"))

    # Un-finalize release file
    try:
        releases_dir = _resolve_undo_releases_dir(
            project_path, ws_root, releasable_name,
        )
        release_file_changed = unfinalize_release_file(releases_dir, version)
        if release_file_changed:
            run("git", ["add", releases_dir])
            run("git", ["commit", "-m", f"chore: restore release file after non-latest undo of {tag}"])
            results.append(("Restore release file", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Restore release file", FAILED, "manually restore release file"))

    # Write audit record
    try:
        if releasable_name and ws_root:
            from ..workspace import get_releasable_dir
            audit_dir = get_releasable_dir(str(ws_root), releasable_name)
        else:
            audit_dir = os.path.join(project_path, ".rlsbl")
        audit_path = write_undo_audit(audit_dir, version, tag, gate_result)
        run("git", ["add", audit_path])
        run("git", ["commit", "-m", f"chore: audit record for non-latest undo of {tag}"])
        results.append(("Write audit record", OK, "-"))
    except Exception:
        traceback.print_exc()
        results.append(("Write audit record", FAILED, "manually write undo-audit.json"))

    # Push
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

    has_failure = any(status == FAILED for _, status, _ in results)
    if has_failure:
        _print_summary(results)
    else:
        print(f"\nNon-latest undo of {tag} complete.")
