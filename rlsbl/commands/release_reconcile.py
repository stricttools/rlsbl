"""Reconcile release metadata with rewritten history: re-push moved tags and recreate their GitHub Releases from the safegit rewrite journal.

A history rewrite moves every commit it touches, which silently invalidates
two pieces of release metadata that live OUTSIDE the commit graph:

- **Tags** still point at the rewritten commits locally, but the remote holds
  the pre-rewrite ones, so ``git ls-remote --tags`` and the local repository
  disagree about what every released version is.
- **GitHub Releases** are attached to the remote tag, so they keep pointing at
  commits that no longer exist in history.

``rlsbl release scrub`` fixes both as part of its own flow. This module holds
that logic so it is also available *standalone*, after ANY out-of-band rewrite
-- a raw ``safegit scrub``, a ``git filter-repo`` run, someone else's rewrite
pulled into the repo. The standalone entry point is ``rlsbl release reconcile``.

The reconcile is **journal-driven and fail-closed**. safegit writes every
rewrite's old-to-new commit map to ``.git/safegit/rewrite-maps.jsonl``; a tag
is only re-pushed when the journal explains its divergence (the remote's
commit maps to the local one). A divergence the journal does NOT explain is a
hard error: force-pushing over it could destroy work.

Every function that shells out takes its git/gh runners from the caller
(``git=``, ``gh=``, ...). Both entry points -- the scrub flow and the
standalone command -- pass their own module-level bindings, so one set of test
doubles covers whichever path a test drives, and neither module has to reach
into the other's namespace.
"""

import os
import sys

from ..tag_glob import TagMode, parse_version_tag
from ..utils import (
    check_gh_auth,
    check_gh_installed,
    extract_changelog_entry,
    get_push_timeout,
    run,
    run_gh,
)
from ..workspace import load_workspace


def tag_name_from_refname(refname):
    """Return the tag name for a ``refs/tags/...`` refname, else None."""
    prefix = "refs/tags/"
    if not isinstance(refname, str) or not refname.startswith(prefix):
        return None
    name = refname[len(prefix):]
    return name or None


def snapshot_remote_refs(timeout=120, *, git=None):
    """Snapshot the remote's refs. Returns ``{refname: sha}``.

    Includes the peeled ``refs/tags/<name>^{}`` entries, so an annotated tag's
    COMMIT is available alongside its tag-object sha.

    These are the only trustworthy lease expectations for a post-rewrite
    force-push: a bare ``--force-with-lease`` is useless once the rewrite has
    moved the remote-tracking refs, and tags carry no tracking information at
    all.
    """
    git = git or run
    out = git("git", ["ls-remote", "origin"], timeout=timeout)
    refs = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            sha, ref = parts
            refs[ref.strip()] = sha.strip()
    return refs


def push_ref_with_lease(refname, expected_sha, target_sha, *, timeout, git=None):
    """Force-push one ref with an explicit lease expectation.

    ``expected_sha`` is the remote value captured before the rewrite's effects
    were published (None when the ref did not exist remotely). ``target_sha``
    is the value the remote should end up with; if the push is rejected but
    the remote already equals ``target_sha`` (a resumed run), the push is
    treated as done. Any other rejection is a hard error: the remote changed
    under us and force-pushing would destroy someone's work.

    The push runs with ``--no-verify``: these are tool-internal pushes and the
    pre-push hook exists to catch MANUAL pushes to release branches.
    """
    git = git or run
    lease = f"--force-with-lease={refname}:{expected_sha or ''}"
    try:
        git("git", ["push", "--no-verify", lease, "origin", f"{refname}:{refname}"],
            timeout=timeout)
        return
    except Exception as push_exc:
        # Idempotence: a previous (partially completed) run may have
        # already pushed this ref.
        try:
            out = git("git", ["ls-remote", "origin", refname], timeout=120)
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
            f"The remote changed since the rewrite started; refusing to "
            f"force-push over it.",
            file=sys.stderr,
        )
        sys.exit(1)


def push_rewritten_tags(tags, remote_refs, *, push_timeout, git=None):
    """Force-push every rewritten tag with an explicit lease.

    ``tags`` is a list of dicts with ``refname`` and ``new_sha`` (safegit's
    tag list shape, which the standalone reconcile also produces).
    """
    for tag_info in tags:
        refname = tag_info.get("refname", "")
        if tag_name_from_refname(refname) is None:
            print(
                f"Warning: skipping non-tag refname in the tag list: "
                f"{refname!r}",
                file=sys.stderr,
            )
            continue
        push_ref_with_lease(
            refname, remote_refs.get(refname), tag_info.get("new_sha", ""),
            timeout=push_timeout, git=git,
        )


def _notes_for_tag(tag_name, version, *, ctx, project_root, workspace_projects,
                   tag_prefix_index, extract_entry=None):
    """Resolve a tag's release notes from the owning project's CHANGELOG.md."""
    extract_entry = extract_entry or extract_changelog_entry
    if ctx.workspace_root:
        matched_proj = None
        if tag_prefix_index:
            for prefix, proj in tag_prefix_index.items():
                if tag_name.startswith(prefix):
                    matched_proj = proj
                    break
        if matched_proj is not None:
            proj_path = os.path.join(str(ctx.workspace_root), matched_proj.path)
            changelog_path = os.path.join(proj_path, "CHANGELOG.md")
            if os.path.exists(changelog_path):
                return extract_entry(changelog_path, version)
            return None
        parsed = parse_version_tag(tag_name, mode=TagMode.FINAL_ONLY)
        if parsed and parsed.scheme == "standalone":
            changelog_path = os.path.join(str(ctx.workspace_root), "CHANGELOG.md")
            if os.path.exists(changelog_path):
                return extract_entry(changelog_path, version)
            return None
        print(
            f"Warning: no prefix match for tag {tag_name}, scanning all projects",
            file=sys.stderr,
        )
        for proj in workspace_projects or []:
            proj_path = os.path.join(str(ctx.workspace_root), proj.path)
            changelog_path = os.path.join(proj_path, "CHANGELOG.md")
            if os.path.exists(changelog_path):
                entry = extract_entry(changelog_path, version)
                if entry:
                    return entry
        return None

    changelog_path = os.path.join(str(project_root), "CHANGELOG.md")
    if os.path.exists(changelog_path):
        return extract_entry(changelog_path, version)
    return None


def recreate_github_releases(tags, *, ctx, project_root, workspace_projects,
                             tag_prefix_index, gh=None, gh_installed=None,
                             gh_auth=None, extract_entry=None):
    """Recreate the GitHub Release for every rewritten tag that had one.

    A Release is attached to the remote tag, so once the tag moves the Release
    still points at a commit that no longer exists. Each existing Release is
    deleted and recreated with notes taken from the owning project's
    CHANGELOG.md. Tags without a Release are left alone -- this reconciles,
    it does not publish.

    Individual failures are warnings: a partially reconciled forge is better
    than an aborted reconcile that leaves the rest untouched, and re-running
    the command is idempotent.
    """
    gh = gh or run_gh
    gh_installed = gh_installed or check_gh_installed
    gh_auth = gh_auth or check_gh_auth
    if not (gh_installed() and gh_auth()):
        return 0

    recreated = 0
    for tag_info in tags:
        refname = tag_info.get("refname", "")
        tag_name = tag_name_from_refname(refname)
        if tag_name is None:
            print(
                f"Warning: skipping non-tag refname in the tag list: "
                f"{refname!r}",
                file=sys.stderr,
            )
            continue

        try:
            gh(["release", "view", tag_name, "--json", "body"], config=ctx.config)
        except Exception:
            continue  # No Release for this tag -- nothing to reconcile.

        try:
            gh(["release", "delete", tag_name, "--yes"], config=ctx.config)
        except Exception as e:
            print(f"Warning: failed to delete release {tag_name}: {e}",
                  file=sys.stderr)
            continue

        # Extract the version from the tag name (final releases only -- a
        # prerelease suffix disqualifies the tag). Handles standalone
        # "v1.2.3", monorepo "project@v1.2.3", and path "project/v1.2.3".
        parsed_tag = parse_version_tag(tag_name, mode=TagMode.FINAL_ONLY)
        if not parsed_tag:
            print(f"Warning: cannot extract version from tag {tag_name}",
                  file=sys.stderr)
            continue
        version = parsed_tag.version

        notes = _notes_for_tag(
            tag_name, version, ctx=ctx, project_root=project_root,
            workspace_projects=workspace_projects,
            tag_prefix_index=tag_prefix_index, extract_entry=extract_entry,
        ) or f"Release {version}"

        try:
            gh(["release", "create", tag_name, "--title", tag_name,
                "--notes", notes], config=ctx.config)
            recreated += 1
        except Exception as e:
            print(f"Warning: failed to recreate release {tag_name}: {e}",
                  file=sys.stderr)
    return recreated


# ---------------------------------------------------------------------------
# Standalone command
# ---------------------------------------------------------------------------


class ReconcileError(Exception):
    """Raised when the reconcile cannot proceed safely."""


def _local_tags(git=None):
    git = git or run
    out = git("git", ["tag", "-l"])
    return [t.strip() for t in out.splitlines() if t.strip()]


def plan_reconcile(commit_map, remote_refs, *, git=None):
    """Decide which tags the rewrite moved, and refuse anything unexplained.

    Returns ``(tags, skipped)`` where ``tags`` is the safegit-shaped list of
    ``{"refname", "new_sha"}`` records to re-push and ``skipped`` maps tag
    names to the reason they need no action.

    Raises :class:`ReconcileError` when a tag diverges from the remote in a
    way the rewrite journal does NOT explain: that divergence was created by
    something other than this rewrite, and force-pushing over it could destroy
    work.
    """
    git = git or run
    tags = []
    skipped = {}
    unexplained = []

    for name in _local_tags(git):
        refname = f"refs/tags/{name}"
        try:
            local_ref = git("git", ["rev-parse", refname]).strip()
            local_peeled = git("git", ["rev-parse", f"{refname}^{{}}"]).strip()
        except Exception:
            skipped[name] = "unresolvable locally"
            continue

        remote_ref = remote_refs.get(refname)
        if remote_ref is None:
            skipped[name] = "not on the remote"
            continue
        remote_peeled = remote_refs.get(f"{refname}^{{}}", remote_ref)

        if remote_ref == local_ref:
            skipped[name] = "already reconciled"
            continue

        if commit_map.get(remote_peeled) == local_peeled:
            tags.append({"refname": refname, "new_sha": local_ref})
        else:
            unexplained.append((name, remote_peeled, local_peeled))

    if unexplained:
        lines = [
            "Refusing to reconcile: some tags diverge from the remote in a way "
            "the rewrite journal does not explain.",
            "",
        ]
        for name, remote_sha, local_sha in unexplained:
            lines.append(
                f"  {name}: remote {remote_sha[:12]} -> local {local_sha[:12]} "
                f"(not a mapping recorded by the rewrite)"
            )
        lines.extend([
            "",
            "Force-pushing these could destroy work that is not part of the "
            "rewrite. Investigate the divergence, or move the affected tags "
            "yourself, then re-run.",
        ])
        raise ReconcileError("\n".join(lines))

    return tags, skipped


def run_cmd(flags, *, ctx):
    """Reconcile tags and GitHub Releases with a rewritten history.

    Reads the last rewrite group from safegit's persisted journal, determines
    which tags that rewrite moved, force-pushes them with explicit leases, and
    recreates the GitHub Releases attached to them.
    """
    from .release_scrub import _load_rewrite_journal

    project_root = ctx.project_root
    dry_run = flags.get("dry-run", False)

    journal = _load_rewrite_journal()
    if journal is None:
        print(
            "Error: no safegit rewrite journal found "
            "(.git/safegit/rewrite-maps.jsonl).\n"
            "`rlsbl release reconcile` repairs release metadata after a "
            "history rewrite, and it derives what moved from that journal. "
            "Without it there is nothing to reconcile against.",
            file=sys.stderr,
        )
        sys.exit(1)

    commit_map = journal.get("commit_map") or {}
    if not commit_map:
        print(
            f"Error: the rewrite journal record {journal['id']} carries no "
            f"commit map, so nothing can be reconciled from it.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Rewrite {journal['id']} ({journal.get('op') or 'unknown op'}"
        f"{', ' + journal['reason'] if journal.get('reason') else ''}): "
        f"{len(commit_map)} commit(s) rewritten."
    )

    remote_refs = snapshot_remote_refs()
    try:
        tags, skipped = plan_reconcile(commit_map, remote_refs)
    except ReconcileError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not tags:
        print("Nothing to reconcile: no tag on the remote was moved by this "
              "rewrite.")
        for name, reason in sorted(skipped.items()):
            print(f"  {name}: {reason}")
        return

    print(f"Tags to reconcile ({len(tags)}):")
    for t in tags:
        print(f"  {tag_name_from_refname(t['refname'])} -> {t['new_sha'][:12]}")

    if dry_run:
        print(
            "\nDry run: would force-push the tags above with explicit leases "
            "and recreate any GitHub Release attached to them."
        )
        return

    # Announcement, not a gate: `release reconcile` declares itself
    # `consequential`, so strictcli confirmed once before dispatch.
    print(
        f"Force-pushing {len(tags)} tag(s) and recreating their GitHub "
        f"Releases."
    )

    push_timeout = get_push_timeout(
        ctx.config, override=flags.get("push-timeout"),
    )
    push_rewritten_tags(tags, remote_refs, push_timeout=push_timeout)
    print(f"Pushed {len(tags)} rewritten tag(s).")

    workspace_projects = None
    tag_prefix_index = None
    if ctx.workspace_root:
        workspace_projects = load_workspace(str(ctx.workspace_root))
        tag_prefix_index = {f"{p.name}@": p for p in workspace_projects}

    recreated = recreate_github_releases(
        tags, ctx=ctx, project_root=project_root,
        workspace_projects=workspace_projects,
        tag_prefix_index=tag_prefix_index,
    )
    print(f"Recreated {recreated} GitHub Release(s).")
