"""Reconcile a repository's published release metadata with what its own records say it released.

Two pieces of release metadata live OUTSIDE the commit graph and outside the
working tree, so nothing about a checkout makes them true: the **git refs on
the remote** (a version's tag, its ecosystem companions, the aliases a rename
recorded) and the **GitHub Release** attached to each of them. A history
rewrite moves the commits under them. A release interrupted after its candidate
push never created them. An out-of-band ``git push --delete`` removes them. In
every case the repository's own records say one thing and the forge says
another.

``rlsbl release reconcile`` observes both sides, judges every subject, and --
only when told to -- writes the difference.

The four explanation sources
----------------------------

A divergence is repaired only when something EXPLAINS it. Four records can, and
the reconcile merges all four into one answer rather than resting on any single
one:

* **safegit's rewrite journal** (``.git/safegit/rewrite-maps.jsonl``) -- the
  old-to-new commit map of the last rewrite. It was the whole spine of this
  command and is now one source among four. It lives under ``.git``, so it does
  NOT survive a fresh clone.
* **The release ledger** -- the archived release files, whose ``candidate_sha``
  is what each version's refs should point at. This is the authority for the
  TARGET, not merely a witness to a move.
* **The lineage records** -- ``anchor-remap`` events (the same commit map, but
  COMMITTED, so a fresh clone has it), ``boundary-alias`` events (a tag that
  legitimately duplicates another), and ``identity-transition`` events (a
  published identity that changed, and from which version).
* **The committed scrub archives** (``.rlsbl/scrubs/scrub-*.json``) -- each
  past scrub's own old-to-new map, committed to the repository and therefore
  present in a clone that never saw the journal.

The five verdicts
-----------------

Every subject -- one git ref, or one version's GitHub Release -- falls into
exactly one class:

* ``materialize`` -- the record says it exists; the forge does not have it.
* ``already-correct`` -- both sides agree. Nothing is done.
* ``re-point-with-lease`` -- the forge holds a different commit AND a source
  explains the difference. The force-push carries an explicit
  ``--force-with-lease`` captured from the value actually read off the remote,
  never a bare lease (a rewrite has already invalidated the remote-tracking
  refs a bare lease would consult, and tags carry no tracking information at
  all).
* ``refuse-foreign`` -- **the publication tripwire.** The forge holds something
  no source explains. One such subject aborts the entire reconcile: nothing is
  repaired anywhere, because a force-push over an unexplained divergence could
  destroy work, and a partial repair around it would be a reconcile that
  silently decided which half of an inconsistent world to trust.
* ``refuse-identity-mismatch`` -- the target's
  ``release_materialization_policy`` refuses. Go declares it: a Go tag IS the
  published artifact, so recreating one for a version released under a module
  path the repository has since changed would publish that version under the
  new identity for the first time, permanently.

Consent is file-driven
----------------------

``--plan`` observes and writes ``.rlsbl/releases/reconcile-plan.toml``; that
file IS the preview's output artifact. ``--apply`` reads it, re-observes, and
refuses when the world moved under the plan. ``--dry-run`` renders and writes
nothing at all -- under ``--plan`` the plan file is not written, and under
``--apply`` the plan is checked and the writes are only described.

Every function that shells out takes its git/gh runners from the caller
(``git=``, ``gh=``, ...). Both entry points -- the scrub flow and the
standalone command -- pass their own module-level bindings, so one set of test
doubles covers whichever path a test drives, and neither module has to reach
into the other's namespace.
"""

import glob
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field

from ..preview_apply import (
    Preview,
    Reconciler,
    VerdictItem,
    reconcile,
)
from ..tag_glob import TagMode, parse_version_tag
from ..utils import (
    check_gh_auth,
    check_gh_installed,
    extract_changelog_entry,
    get_push_timeout,
    run,
    run_gh,
)


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
# The standalone command: one merged preview over four explanation sources
# ---------------------------------------------------------------------------


class ReconcileError(Exception):
    """Raised when the reconcile cannot proceed safely."""


# The five verdict classes. ``VerdictItem.state`` carries these verbatim and
# the shared renderer prints them with hyphens, so the vocabulary a reader sees
# is the vocabulary the code branches on.
STATE_MATERIALIZE = "materialize"
STATE_ALREADY_CORRECT = "already_correct"
STATE_RE_POINT = "re_point_with_lease"
STATE_REFUSE_FOREIGN = "refuse_foreign"
STATE_REFUSE_IDENTITY = "refuse_identity_mismatch"

REFUSAL_STATES = (STATE_REFUSE_FOREIGN, STATE_REFUSE_IDENTITY)

# Where the plan file sits: beside the release state it describes, so a
# releasable's plan lives under its own state directory rather than at the
# repository root.
PLAN_FILENAME = "reconcile-plan.toml"

# How many Releases one listing asks for. A repository with more released
# versions than this is not silently truncated: the listing count is compared
# against the ledger and a short answer is an error, never a set of absent
# Releases to materialize.
_RELEASE_LIST_LIMIT = 1000

_LIST_TIMEOUT = 60


def plan_path(releases_dir):
    """The reconcile plan file for a project whose ledger is *releases_dir*."""
    return os.path.join(str(releases_dir), PLAN_FILENAME)


def _local_tags(git=None):
    git = git or run
    out = git("git", ["tag", "-l"])
    return [t.strip() for t in out.splitlines() if t.strip()]


def _same_commit(a, b):
    """Do two object names denote the same commit, allowing abbreviation?"""
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]


# ---------------------------------------------------------------------------
# The explanation sources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Explanations:
    """Everything the four sources say about how the world got this way.

    ``commit_map`` is the merged old-to-new map; ``origins`` names which source
    contributed each entry, so a plan can say WHY a divergence is explained
    rather than merely that it is.
    """

    commit_map: dict = field(default_factory=dict)
    origins: dict = field(default_factory=dict)
    alias_tags: frozenset = frozenset()
    identity_transitions: tuple = ()
    sources_present: tuple = ()

    def resolve(self, sha):
        """Follow *sha* through every recorded rewrite to its final commit.

        Successive rewrites chain: a commit rewritten twice appears as the old
        side of one map entry and the new side of another. The walk is bounded
        by the map's own size and stops on a cycle, so a malformed record
        cannot spin here.
        """
        seen = {sha}
        current = sha
        chain = []
        while current in self.commit_map:
            nxt = self.commit_map[current]
            if nxt in seen:
                break
            chain.append(self.origins.get(current, "a recorded rewrite"))
            seen.add(nxt)
            current = nxt
        return current, tuple(chain)


def _journal_map():
    """The last rewrite's commit map from safegit's journal, plus its label."""
    from .release_scrub import _load_rewrite_journal

    journal = _load_rewrite_journal()
    if journal is None or not journal.get("commit_map"):
        return {}, None
    return dict(journal["commit_map"]), f"safegit rewrite journal ({journal['id']})"


def _scrub_archive_maps(releases_dirs):
    """Every committed scrub archive's own old-to-new map.

    These are the source that survives a fresh clone: the archives are
    committed to the repository, while safegit's journal lives under ``.git``.
    """
    merged = {}
    origins = {}
    found = []
    for releases_dir in releases_dirs:
        scrubs = os.path.join(
            os.path.dirname(os.path.normpath(releases_dir)), "scrubs",
        )
        for path in sorted(glob.glob(os.path.join(scrubs, "scrub-*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    archive = json.load(f)
            except (OSError, ValueError) as exc:
                raise ReconcileError(
                    f"the committed scrub archive {path} could not be read "
                    f"({exc}). It is one of the records that explains a "
                    f"divergence, and a reconcile that skipped an unreadable "
                    f"one would refuse repairs it has the evidence for."
                ) from exc
            rewrites = archive.get("rewrites") or {}
            if not rewrites:
                continue
            found.append(os.path.basename(path))
            for old, new in rewrites.items():
                merged[old] = new
                origins[old] = f"scrub archive {os.path.basename(path)}"
    label = (
        f"committed scrub archives ({', '.join(found)})" if found else None
    )
    return merged, origins, label


def _lineage_facts(lineage_paths):
    """Anchor remaps, boundary aliases and identity transitions, merged."""
    from ..lineage import (
        KIND_ANCHOR_REMAP,
        KIND_BOUNDARY_ALIAS,
        KIND_IDENTITY_TRANSITION,
        read_events,
    )

    commit_map = {}
    origins = {}
    aliases = set()
    transitions = []
    seen_any = False
    for path in lineage_paths:
        events = read_events(path, kinds=[
            KIND_ANCHOR_REMAP, KIND_BOUNDARY_ALIAS, KIND_IDENTITY_TRANSITION,
        ])
        if events:
            seen_any = True
        for event in events:
            if event.KIND == KIND_ANCHOR_REMAP:
                for mapping in event.mappings:
                    commit_map[mapping.old_sha] = mapping.new_sha
                    origins[mapping.old_sha] = (
                        f"lineage anchor-remap {event.rewrite}"
                    )
            elif event.KIND == KIND_BOUNDARY_ALIAS:
                for alias in event.aliases:
                    aliases.add(alias.alias_tag)
            else:
                transitions.append(event)
    return (
        commit_map, origins, frozenset(aliases), tuple(transitions),
        "lineage records" if seen_any else None,
    )


def collect_explanations(releases_dirs, lineage_paths):
    """Merge all four sources into one :class:`Explanations`.

    Precedence is deliberate and narrow: an entry contributed by more than one
    source names the same pair of commits in both, so the maps agree wherever
    they overlap and the merge order only decides which label is reported.
    The committed records go on first and the journal last, so a divergence the
    journal also explains is attributed to it -- it is the most recent event.
    """
    commit_map = {}
    origins = {}
    present = []

    scrub_map, scrub_origins, scrub_label = _scrub_archive_maps(releases_dirs)
    commit_map.update(scrub_map)
    origins.update(scrub_origins)
    if scrub_label:
        present.append(scrub_label)

    (lineage_map, lineage_origins, aliases, transitions,
     lineage_label) = _lineage_facts(lineage_paths)
    commit_map.update(lineage_map)
    origins.update(lineage_origins)
    if lineage_label:
        present.append(lineage_label)

    journal_map, journal_label = _journal_map()
    commit_map.update(journal_map)
    for old in journal_map:
        origins[old] = journal_label
    if journal_label:
        present.append(journal_label)

    return Explanations(
        commit_map=commit_map, origins=origins, alias_tags=aliases,
        identity_transitions=transitions, sources_present=tuple(present),
    )


# ---------------------------------------------------------------------------
# The observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """The world, read once.

    ``releases`` is the set of tag names carrying a GitHub Release, and
    ``releases_known`` says whether it could be read at all -- an unread
    listing is never treated as an empty one, because that would turn every
    released version into a Release to materialize.
    """

    remote_refs: dict
    local_refs: dict
    releases: frozenset = frozenset()
    releases_known: bool = False

    @property
    def digest(self):
        """A digest of everything the verdicts were computed from.

        Stamped into the plan file; the apply step re-observes and compares, so
        a plan can never be applied against a remote that moved under it.
        """
        h = hashlib.sha256()
        for name in sorted(self.remote_refs):
            h.update(f"{name} {self.remote_refs[name]}\n".encode())
        h.update(b"--releases--\n")
        h.update(f"known={self.releases_known}\n".encode())
        for tag in sorted(self.releases):
            h.update(f"{tag}\n".encode())
        return h.hexdigest()


def _local_tag_refs(git=None):
    """Local tag refs as ``{refname: sha}``, including the peeled entries."""
    git = git or run
    out = git("git", [
        "for-each-ref", "--format=%(refname) %(objectname) %(*objectname)",
        "refs/tags/",
    ])
    refs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        refs[parts[0]] = parts[1]
        if len(parts) >= 3 and parts[2]:
            refs[f"{parts[0]}^{{}}"] = parts[2]
        else:
            refs[f"{parts[0]}^{{}}"] = parts[1]
    return refs


def list_releases(*, ctx, gh=None, gh_installed=None, gh_auth=None):
    """The tag names carrying a GitHub Release, in ONE listing.

    Returns ``(tags, known)``. ``known`` is False when gh is unavailable or
    unauthenticated -- the Release half of the reconcile then reports itself as
    unanswerable instead of proposing to create every Release the repository
    has ever published.
    """
    gh = gh or run_gh
    gh_installed = gh_installed or check_gh_installed
    gh_auth = gh_auth or check_gh_auth
    if not (gh_installed() and gh_auth()):
        return frozenset(), False
    try:
        out = gh(
            ["release", "list", "--limit", str(_RELEASE_LIST_LIMIT),
             "--json", "tagName", "-q", ".[].tagName"],
            config=ctx.config, timeout=_LIST_TIMEOUT,
        )
    except Exception as exc:
        raise ReconcileError(
            f"the repository's GitHub Releases could not be listed ({exc}). "
            f"An unread listing is not an empty one: continuing would report "
            f"every released version's Release as absent and propose to "
            f"create it."
        ) from exc
    if not isinstance(out, str):
        # A preview carrier standing in for output that was never produced.
        return frozenset(), False
    return frozenset(t.strip() for t in out.splitlines() if t.strip()), True


def observe_world(*, ctx, git=None, gh=None, gh_installed=None, gh_auth=None,
                  remote_timeout=120):
    """Read the remote refs, the local refs and the Release listing, once each."""
    git = git or run
    remote = snapshot_remote_refs(timeout=remote_timeout, git=git)
    local = _local_tag_refs(git=git)
    releases, known = list_releases(
        ctx=ctx, gh=gh, gh_installed=gh_installed, gh_auth=gh_auth,
    )
    return Observation(
        remote_refs=remote, local_refs=local, releases=releases,
        releases_known=known,
    )


# ---------------------------------------------------------------------------
# The verdict engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefAction:
    """What an apply would do to one subject, carried from observe to apply."""

    kind: str                 # "ref" | "release"
    refname: str = ""
    tag: str = ""
    version: str = ""
    target: str = ""          # the commit the subject should end up at
    observed: str = ""        # the remote's value, the force-with-lease
    create_local_tag: bool = False
    repoint_release_marker: bool = False


def _identity_refusal(version, target, transitions):
    """The recorded identity transition that forbids materializing *version*.

    A transition states that a published identity changed and FROM WHICH
    VERSION. Anything earlier than that version was published under the OLD
    identity, so recreating its refs now would publish it under the current
    one. Returns the offending event, or None.
    """
    from ..release_file import archive_sort_key
    from ..targets.base import MATERIALIZE_UNLESS_IDENTITY_CHANGED

    if target.release_materialization_policy != MATERIALIZE_UNLESS_IDENTITY_CHANGED:
        return None
    for event in transitions:
        try:
            if archive_sort_key(version) < archive_sort_key(
                event.effective_version
            ):
                return event
        except Exception:
            # An unparseable version on either side is not evidence that the
            # materialization is safe, so it is treated as covered by the
            # transition rather than waved through.
            return event
    return None


def _ref_verdict(*, refname, tag, version, anchor, observation, explanations,
                 target, archived):
    """Classify one git ref. Returns a :class:`VerdictItem`."""
    remote = observation.remote_refs.get(refname)
    remote_peeled = observation.remote_refs.get(f"{refname}^{{}}", remote)
    local = observation.local_refs.get(refname)
    local_peeled = observation.local_refs.get(f"{refname}^{{}}", local)

    version_fact = f"version {version}" if version else "no archived version"

    # The ledger is the authority for where a released ref belongs. A local ref
    # that disagrees with it is not a repair the reconcile may publish.
    if archived and local_peeled and anchor and not _same_commit(
        local_peeled, anchor,
    ):
        return VerdictItem(
            key=refname, state=STATE_REFUSE_FOREIGN,
            summary="the local ref does not match the release ledger",
            facts=(
                version_fact,
                f"local:  {local_peeled}",
                f"ledger: {anchor}",
            ),
            detail=(
                "  Pushing this ref would publish a commit the ledger does "
                "not record as released.\n"
                "  Re-point the tag at the anchor, or repair the archive, "
                "before reconciling."
            ),
        )

    target_sha = local or (anchor if archived else "")

    if remote is None:
        if not target_sha:
            return VerdictItem(
                key=refname, state=STATE_REFUSE_FOREIGN,
                summary="recorded as released but present nowhere",
                facts=(version_fact, "absent locally and on origin"),
                detail=(
                    "  There is no commit to create this ref at. Fetch the "
                    "ref, or record the version as unanchorable."
                ),
            )
        refusal = _identity_refusal(version, target, explanations.identity_transitions)
        if refusal is not None:
            return VerdictItem(
                key=refname, state=STATE_REFUSE_IDENTITY,
                summary="a recorded identity transition forbids recreating it",
                facts=(
                    version_fact,
                    f"{refusal.facet}: {refusal.old} -> {refusal.new}",
                    f"effective from {refusal.effective_version}",
                ),
                detail=(
                    "  This target's release_materialization_policy is "
                    "refuse-identity-transition: its tags ARE the published\n"
                    "  artifact, so pushing this one would publish a version "
                    "released under the old identity under the NEW one, for\n"
                    "  the first time and permanently. Create the ref yourself "
                    "if that is genuinely what you want."
                ),
            )
        return VerdictItem(
            key=refname, state=STATE_MATERIALIZE,
            summary="recorded as released, absent on origin",
            facts=(version_fact, f"would push {target_sha}"),
            actions=(f"push {refname} -> {target_sha[:12]}",),
            data=RefAction(
                kind="ref", refname=refname, tag=tag, version=version,
                target=target_sha, observed="",
                create_local_tag=local is None,
            ),
        )

    if _same_commit(remote_peeled or remote, local_peeled or target_sha):
        return VerdictItem(
            key=refname, state=STATE_ALREADY_CORRECT,
            summary="origin already holds this ref",
            facts=(version_fact, f"origin: {remote_peeled or remote}"),
        )

    resolved, chain = explanations.resolve(remote_peeled or remote)
    if _same_commit(resolved, local_peeled or target_sha):
        return VerdictItem(
            key=refname, state=STATE_RE_POINT,
            summary="origin holds a commit a recorded rewrite moved",
            facts=(
                version_fact,
                f"origin: {remote_peeled or remote}",
                f"here:   {local_peeled or target_sha}",
                f"explained by: {', '.join(chain) or 'a recorded rewrite'}",
            ),
            actions=(
                f"force-push {refname} -> {(local or target_sha)[:12]} "
                f"(lease {remote[:12]})",
            ),
            data=RefAction(
                kind="ref", refname=refname, tag=tag, version=version,
                target=local or target_sha, observed=remote,
                repoint_release_marker=bool(version),
            ),
        )

    return VerdictItem(
        key=refname, state=STATE_REFUSE_FOREIGN,
        summary="origin holds a commit no record explains",
        facts=(
            version_fact,
            f"origin: {remote_peeled or remote}",
            f"here:   {local_peeled or target_sha}",
        ),
        detail=(
            "  No rewrite journal entry, lineage anchor-remap or committed "
            "scrub archive maps the origin value to this one.\n"
            "  Force-pushing over it could destroy work that is not part of "
            "any recorded rewrite."
        ),
    )


def _release_verdict(*, tag, version, anchor, observation):
    """Classify one version's GitHub Release. Presence only, from the listing."""
    key = f"release:{tag}"
    if tag in observation.releases:
        return VerdictItem(
            key=key, state=STATE_ALREADY_CORRECT,
            summary="the GitHub Release exists",
            facts=(f"version {version}",),
        )
    return VerdictItem(
        key=key, state=STATE_MATERIALIZE,
        summary="released, but no GitHub Release exists for its tag",
        facts=(f"version {version}", f"anchored at {anchor}"),
        actions=(
            f"create the GitHub Release {tag} with the version's changelog "
            f"section and its rlsbl-ci-sha marker",
        ),
        data=RefAction(
            kind="release", tag=tag, version=version, target=anchor,
        ),
    )


def build_preview(*, observation, explanations, target, ref_ctx, releases_dir):
    """One merged preview over every subject this repository owns.

    Subjects come from two places and are judged in one pass:

    * **the release ledger** -- every archived version's full ref set (its
      primary tag, its ecosystem companions and its recorded aliases, all from
      ``expected_refs``, the single authority), plus that version's GitHub
      Release;
    * **the local tag namespace** -- any tag the ledger does not name that
      nonetheless diverges from origin. Those are outside the ledger's account
      of what was released, so they are never materialized; they are only
      classified, which is what makes the publication tripwire fire for a
      repository that has no archives at all.

    An ``unanchorable`` version is skipped entirely: it has no commit, so there
    is nothing to compare a ref against and nothing to create one at.
    """
    from ..errors import RlsblError
    from ..release_file import (
        archived_release_path,
        list_archived_versions,
        read_release_file,
    )

    items = []
    claimed = set()
    unanchorable = []

    for version in list_archived_versions(releases_dir):
        path = archived_release_path(releases_dir, version)
        try:
            archive = read_release_file(path)
        except (RlsblError, OSError) as exc:
            raise ReconcileError(
                f"the release archive for {version} could not be read "
                f"({exc}), so the refs it owns are unknown and the reconcile "
                f"cannot say whether origin is right about them."
            ) from exc
        if archive.unanchorable:
            unanchorable.append(version)
            continue
        anchor = (archive.candidate_sha or "").strip()
        if not anchor:
            raise ReconcileError(
                f"the release archive for {version} carries neither an anchor "
                f"nor the unanchorable marker ({path}), so there is no commit "
                f"its refs should point at. Backfill it before reconciling."
            )

        expected = target.expected_refs(version, ref_ctx)
        for tag in expected.tags:
            refname = f"refs/tags/{tag}"
            claimed.add(refname)
            items.append(_ref_verdict(
                refname=refname, tag=tag, version=version, anchor=anchor,
                observation=observation, explanations=explanations,
                target=target, archived=True,
            ))
        if observation.releases_known:
            items.append(_release_verdict(
                tag=expected.primary, version=version, anchor=anchor,
                observation=observation,
            ))

    for refname in sorted(observation.local_refs):
        if refname.endswith("^{}") or refname in claimed:
            continue
        if refname not in observation.remote_refs:
            continue
        tag = tag_name_from_refname(refname) or ""
        parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
        verdict = _ref_verdict(
            refname=refname, tag=tag,
            version=parsed.version if parsed else "",
            anchor="", observation=observation, explanations=explanations,
            target=target, archived=False,
        )
        if verdict.state == STATE_ALREADY_CORRECT:
            continue
        items.append(verdict)

    preview = Preview(tuple(items))
    if unanchorable:
        print(
            f"Skipping {len(unanchorable)} version(s) recorded unanchorable "
            f"(no commit to reconcile against): {', '.join(unanchorable)}"
        )
    return preview


def refusals(preview):
    """Every item whose verdict forbids the reconcile from writing anything."""
    return [i for i in preview.items if i.state in REFUSAL_STATES]


def tripwire_error(preview):
    """The message a refusal aborts the whole reconcile with."""
    lines = [
        "Refusing to reconcile: some published refs are in a state no record "
        "explains.",
        "",
    ]
    for item in refusals(preview):
        lines.append(f"  {item.key}: {item.state_label} -- {item.summary}")
        for fact in item.facts:
            lines.append(f"    {fact}")
    lines.extend([
        "",
        "NOTHING has been changed -- not the refused subjects and not the ",
        "repairable ones. A reconcile that repaired around an unexplained ",
        "divergence would be choosing which half of an inconsistent world to ",
        "trust. Investigate the divergence, resolve it yourself, then re-run.",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The plan file: the preview's output artifact and the apply step's input
# ---------------------------------------------------------------------------

PLAN_FORMAT_VERSION = 1


def render_plan(preview, digest, *, generated_by):
    """Serialize *preview* as the plan document."""
    import tomlkit

    from ..lineage import now_timestamp

    doc = tomlkit.document()
    doc.add(tomlkit.comment(
        "Written by `rlsbl release reconcile --plan` and consumed by"
    ))
    doc.add(tomlkit.comment(
        "`rlsbl release reconcile --apply`. Do not edit: the apply step "
        "re-observes"
    ))
    doc.add(tomlkit.comment(
        "the world and refuses when it no longer matches world_digest."
    ))
    doc.add(tomlkit.comment("strictspec document version gate (do not remove)"))
    doc.add("format_version", PLAN_FORMAT_VERSION)
    doc.add("generated_at", now_timestamp())
    doc.add("generated_by", generated_by)
    doc.add("world_digest", digest)

    items = tomlkit.aot()
    for item in preview.items:
        action = item.data if isinstance(item.data, RefAction) else None
        entry = tomlkit.table()
        entry.add("key", item.key)
        entry.add("kind", action.kind if action else (
            "release" if item.key.startswith("release:") else "ref"
        ))
        entry.add("state", item.state_label)
        version = action.version if action else ""
        if version:
            entry.add("version", version)
        if action and action.target:
            entry.add("target", action.target)
        if action and action.observed:
            entry.add("observed", action.observed)
        if item.summary:
            entry.add("summary", item.summary)
        items.append(entry)
    doc.add("items", items)
    return tomlkit.dumps(doc)


def read_plan(path):
    """Read and validate the plan document at *path*.

    The shape is strictspec's to decide (``.strictspec/reconcile-plan.schema.toml``);
    this raises :class:`ReconcileError` naming the file for anything the
    validator rejects, and for an absent file.
    """
    from ..strictspec_gen import reconcile_plan_validator as validator

    if not os.path.isfile(path):
        raise ReconcileError(
            f"no reconcile plan at {path}.\n"
            f"  `rlsbl release reconcile --apply` consumes a plan written by "
            f"`rlsbl release reconcile --plan`.\n"
            f"  Run the plan first, read what it proposes, then apply it."
        )
    with open(path, "rb") as f:
        raw = f.read()
    root, diags = validator.validate_bytes(raw, "toml")
    if diags:
        raise ReconcileError(
            f"the reconcile plan {path} is not a valid plan document: "
            + "; ".join(d.message for d in diags)
        )
    return root


def check_plan_matches(plan, observation, path):
    """Refuse an apply whose plan was written against a different world."""
    if plan.world_digest == observation.digest:
        return
    raise ReconcileError(
        f"the world changed since {os.path.basename(path)} was written.\n"
        f"  plan observed:  {plan.world_digest[:16]}\n"
        f"  now observed:   {observation.digest[:16]}\n"
        f"  The plan names force-push leases captured from the remote values "
        f"it read. Applying it now would push against\n"
        f"  expectations that no longer hold. Re-run "
        f"`rlsbl release reconcile --plan`, read the new plan, and apply that."
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _release_publication_for(action, *, changelog_path, releases_dir):
    """The Release document for one version, from the changelog and the ledger."""
    from ..release_publication import anchor_from_ledger, publication

    notes = ""
    if changelog_path and os.path.exists(changelog_path):
        notes = extract_changelog_entry(changelog_path, action.version) or ""
    anchor = (
        anchor_from_ledger(releases_dir, action.version) or action.target
    )
    return publication(
        tag=action.tag, version=action.version, candidate_sha=anchor,
        notes=notes,
    )


def apply_item(item, *, ctx, releases_dir, changelog_path, push_timeout,
               git=None, gh=None, log=print):
    """Perform one verdict's actions. Called only outside a dry run."""
    from ..release_publication import create_release, ensure_marker

    git = git or run
    gh = gh or run_gh
    action = item.data
    if not isinstance(action, RefAction):
        return

    if action.kind == "ref":
        if action.create_local_tag:
            git("git", ["tag", "-f", action.tag, action.target])
            log(f"Created local tag {action.tag} at {action.target[:12]}")
        push_ref_with_lease(
            action.refname, action.observed or None, action.target,
            timeout=push_timeout, git=git,
        )
        log(f"Pushed {action.refname} -> {action.target[:12]}")
        if action.repoint_release_marker and action.version:
            # The Release follows the tag NAME, so a moved tag drags its
            # Release onto the new commit -- but the body still names the old
            # one in its rlsbl-ci-sha marker, which is what the publish
            # workflow reads. Re-pointed here, from the ledger's anchor.
            pub = _release_publication_for(
                action, changelog_path=changelog_path,
                releases_dir=releases_dir,
            )
            try:
                if ensure_marker(pub, gh=gh, config=ctx.config):
                    log(f"Re-pointed the ci-sha marker on {action.tag}")
            except Exception as exc:
                raise ReconcileError(
                    f"the tag {action.tag} was re-pointed, but the ci-sha "
                    f"marker on its GitHub Release could not be updated "
                    f"({exc}). The marker is what the publish workflow reads "
                    f"to learn which commit CI proved green, so it is a hard "
                    f"error rather than a warning. Fix the cause and re-run "
                    f"the reconcile."
                ) from exc
        return

    pub = _release_publication_for(
        action, changelog_path=changelog_path, releases_dir=releases_dir,
    )
    create_release(pub, gh=gh, config=ctx.config)
    log(f"Created GitHub Release {action.tag}")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _resolve_identity(ctx):
    """``(target, ref_context, releases_dir)`` for the project being reconciled.

    The same resolution the ref checks work from, so a reconcile names exactly
    the refs those checks look for.

    A repository with no detectable target still gets an answer rather than a
    refusal, and the answer is not a guess: ref naming has a defined default
    (``v{version}``, :class:`~rlsbl.targets.base.BaseTarget`'s own
    ``tag_format``) that holds independently of any ecosystem, and the
    ledger-driven half is empty there anyway. What remains is the local-tag
    half -- the divergences the publication tripwire judges -- and those need no
    target at all.
    """
    from ..checks._common import _resolve_release_identity
    from ..targets.base import BaseTarget
    from ..targets.refs import ref_context

    resolved = _resolve_release_identity(ctx)
    if resolved is not None:
        return resolved
    root = str(ctx.workspace_root or ctx.project_root)
    return (
        BaseTarget(),
        ref_context(repo_root=root),
        os.path.join(str(ctx.project_root), ".rlsbl", "releases"),
    )


def _changelog_path(ctx):
    """The CHANGELOG.md whose sections become recreated Release notes."""
    root = str(ctx.workspace_root or ctx.project_root)
    project = os.path.join(str(ctx.project_root), "CHANGELOG.md")
    if os.path.exists(project):
        return project
    candidate = os.path.join(root, "CHANGELOG.md")
    return candidate if os.path.exists(candidate) else None


def run_cmd(flags, *, ctx):
    """Reconcile this project's published refs and Releases with its records."""
    from .. import __version__ as _rlsbl_version
    from .. import effects

    dry_run = flags.get("dry-run", False)
    mode = flags.get("mode") or "plan"
    if mode not in ("plan", "apply"):
        print(
            f"Error: unknown reconcile mode {mode!r}; expected 'plan' or "
            f"'apply'.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        target, ref_ctx, releases_dir = _resolve_identity(ctx)
    except ReconcileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    lineage_paths = ref_ctx.lineage_paths
    path_of_plan = plan_path(releases_dir)
    changelog_path = _changelog_path(ctx)
    push_timeout = get_push_timeout(
        ctx.config, override=flags.get("push-timeout"),
    )

    state = {}

    def _observe():
        explanations = collect_explanations([releases_dir], lineage_paths)
        observation = observe_world(ctx=ctx)
        state["observation"] = observation
        state["explanations"] = explanations
        preview = build_preview(
            observation=observation, explanations=explanations,
            target=target, ref_ctx=ref_ctx, releases_dir=releases_dir,
        )
        if mode == "apply":
            plan = read_plan(path_of_plan)
            check_plan_matches(plan, observation, path_of_plan)
            state["plan"] = plan
            if refusals(preview):
                raise ReconcileError(tripwire_error(preview))
        return preview

    def _apply(item):
        apply_item(
            item, ctx=ctx, releases_dir=releases_dir,
            changelog_path=changelog_path, push_timeout=push_timeout,
        )

    def _never_apply(item):  # pragma: no cover - defensive
        raise AssertionError(
            "plan mode performs no per-item apply: the plan FILE is its whole "
            "output"
        )

    try:
        if mode == "plan":
            # Always rendered, never applied: the plan file is the artifact,
            # and --dry-run is what suppresses writing it.
            preview = reconcile(
                Reconciler(observe=_observe, apply_item=_never_apply,
                           show_keys=True),
                dry_run=True,
            )
        else:
            preview = reconcile(
                Reconciler(observe=_observe, apply_item=_apply,
                           show_keys=True),
                dry_run=dry_run,
            )
    except ReconcileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    explanations = state["explanations"]
    if explanations.sources_present:
        print(f"\nExplained from: {'; '.join(explanations.sources_present)}")
    else:
        print(
            "\nNo explanation source is present: no safegit rewrite journal, "
            "no lineage anchor-remap record and no committed scrub archive. "
            "Only refs origin already agrees with, and refs it is missing "
            "entirely, can be reconciled."
        )

    if not preview.items:
        print("Nothing to reconcile: origin matches this repository's records.")
        return

    if mode == "plan":
        blocked = refusals(preview)
        if blocked:
            print(f"\nError: {tripwire_error(preview)}", file=sys.stderr)
            sys.exit(1)
        if dry_run:
            print(
                f"\nDry run: the plan above was NOT written to "
                f"{path_of_plan}. Re-run without --dry-run to write it, then "
                f"`rlsbl release reconcile --apply` to perform it."
            )
            return
        effects.makedirs(os.path.dirname(path_of_plan), exist_ok=True)
        effects.atomic_write_text(
            path_of_plan,
            render_plan(
                preview, state["observation"].digest,
                generated_by=_rlsbl_version,
            ),
        )
        print(
            f"\nWrote {path_of_plan}.\n"
            f"Read it, then perform it with:\n"
            f"  rlsbl release reconcile --apply --approve-consequential"
        )
        return

    if dry_run:
        print(
            f"\nDry run: {path_of_plan} matches the world and would be "
            f"applied. Nothing was pushed or created."
        )
        return

    actionable = [
        i for i in preview.items if isinstance(i.data, RefAction)
    ]
    print(f"\nApplied {len(actionable)} change(s).")
    if os.path.exists(path_of_plan):
        effects.remove(path_of_plan)
