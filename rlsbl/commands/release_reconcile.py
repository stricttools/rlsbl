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
* **The release record** -- the archived release files, whose ``candidate_sha``
  is what each version's refs should point at. This is the authority for the
  TARGET, not merely a witness to a move.
* **The transition records** -- ``release-commit-remap`` events (the same commit map, but
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
file IS the preview's output artifact, and it is written even when the plan is
empty, so an apply on it is a clean no-op rather than an instruction to run the
plan that was just run. ``--apply`` reads it, re-observes, and performs exactly
the repairable items the plan named: a subject the fresh observation grew, or a
planned subject whose verdict, lease or target moved, is a hard refusal naming
it (see :func:`check_plan_covers`). ``--dry-run`` renders and writes nothing at
all -- under ``--plan`` the plan file is not written, and under ``--apply`` the
plan is checked and the writes are only described.

``reconcile`` declares itself ``consequential`` as ONE command, so ``--plan``
prompts for consent too. That is deliberate: the two halves are one command,
and the prompt is about running it at all. A per-half classification would make
consent depend on a flag rather than on the command, which is exactly the shape
the effects regime refuses.

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
        # PRERELEASE_INCLUSIVE: the question here is which SCHEME the tag
        # follows, and a pre-release suffix does not change that. It also has
        # to answer the same way _release_record_dir_for_tag does, or a rewritten
        # Release would take its notes from one project and its release commit from
        # another.
        parsed = parse_version_tag(tag_name, mode=TagMode.PRERELEASE_INCLUSIVE)
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


def _release_record_dir_for_tag(tag_name, *, ctx, project_root, tag_prefix_index):
    """The release-archive directory whose release record owns *tag_name*'s version.

    The same resolution :func:`_notes_for_tag` performs for the CHANGELOG, so
    the notes and the release commit a written Release carries describe the same
    project. Returns None when no project owns the tag -- the marker then has
    no release commit to project, which the caller reports rather than guesses at.
    """
    if not ctx.workspace_root:
        return os.path.join(str(project_root), ".rlsbl", "releases")

    from .release.release_state import resolve_releasable_dir

    ws_root = str(ctx.workspace_root)
    matched_proj = None
    for prefix, proj in (tag_prefix_index or {}).items():
        if tag_name.startswith(prefix):
            matched_proj = proj
            break
    if matched_proj is None:
        parsed = parse_version_tag(tag_name, mode=TagMode.PRERELEASE_INCLUSIVE)
        if parsed and parsed.scheme == "standalone":
            return os.path.join(ws_root, ".rlsbl", "releases")
        return None

    proj_path = os.path.join(ws_root, matched_proj.path)
    releasable_dir = resolve_releasable_dir(proj_path, ws_root)
    if releasable_dir:
        return os.path.join(str(releasable_dir), "releases")
    return os.path.join(proj_path, ".rlsbl", "releases")


def update_github_releases(tags, *, ctx, project_root, workspace_projects,
                           tag_prefix_index, gh=None, gh_installed=None,
                           gh_auth=None, extract_entry=None):
    """Rewrite the GitHub Release document for every rewritten tag.

    A GitHub Release follows its tag NAME, and by the time this step runs the
    tags have already been re-pointed -- so every Release is already attached
    to the rewritten commit. What does NOT follow the tag is the document: the
    notes, the ``rlsbl-ci-sha`` marker the publish workflow reads, and the
    pre-release flag. Those are written in place, from the one document
    :mod:`rlsbl.release_publication` decides.

    **Nothing is ever deleted.** An earlier shape deleted each Release and
    created it again, which left a window in which a transient failure stranded
    a tag with no Release at all. There is no such window here: an edit that
    fails leaves the previous Release exactly where it was, and a re-run
    repairs it. A tag carrying NO Release gets one created -- the same
    materialize shape ``rlsbl release reconcile`` performs -- so a rewrite can
    close that gap too rather than only preserve it.

    A tag that is not a version tag under any known scheme is skipped whole,
    without so much as a lookup: there is no version, therefore no document to
    write. Pre-release tags are first-class (``PRERELEASE_INCLUSIVE``).

    The release commit comes from the release record, which the scrub's ``RELEASE_COMMITS_REMAPPED``
    step has already moved through the same rewrite by the time this runs, so
    the marker names the rewritten commit. A version whose archive holds no
    release commit -- released before the release record existed, or recorded unrecoverable --
    gets its document WITHOUT a marker, and the omission is stated on stderr
    rather than hidden.

    Individual failures are warnings: a partially reconciled forge is better
    than an aborted reconcile that leaves the rest untouched, and re-running
    the command is idempotent.

    Returns the number of Releases written (edited or created).
    """
    from ..release_publication import (
        release_commit_from_record,
        create_release,
        edit_all_args,
        is_prerelease,
        notes_file,
        publication,
        update_release,
        view_body_args,
    )

    gh = gh or run_gh
    gh_installed = gh_installed or check_gh_installed
    gh_auth = gh_auth or check_gh_auth
    if not (gh_installed() and gh_auth()):
        return 0

    written = 0
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

        parsed_tag = parse_version_tag(
            tag_name, mode=TagMode.PRERELEASE_INCLUSIVE,
        )
        if not parsed_tag:
            print(
                f"Warning: {tag_name} is not a version tag under any known "
                f"scheme, so there is no version whose document could be "
                f"written. Leaving its GitHub Release exactly as it is -- its "
                f"notes now describe the pre-rewrite commit.",
                file=sys.stderr,
            )
            continue
        version = parsed_tag.version

        try:
            gh(view_body_args(tag_name), config=ctx.config)
            exists = True
        except Exception:
            exists = False

        notes = _notes_for_tag(
            tag_name, version, ctx=ctx, project_root=project_root,
            workspace_projects=workspace_projects,
            tag_prefix_index=tag_prefix_index, extract_entry=extract_entry,
        ) or ""

        release_record_dir = _release_record_dir_for_tag(
            tag_name, ctx=ctx, project_root=project_root,
            tag_prefix_index=tag_prefix_index,
        )
        release_commit = None
        if release_record_dir and os.path.isdir(release_record_dir):
            try:
                release_commit = release_commit_from_record(release_record_dir, version)
            except Exception as exc:
                print(
                    f"Warning: the release archive for {version} could not be "
                    f"read ({exc}); its Release is written without an "
                    f"rlsbl-ci-sha marker.",
                    file=sys.stderr,
                )
        pub = (
            publication(tag=tag_name, version=version, candidate_sha=release_commit,
                        notes=notes)
            if release_commit else None
        )
        if pub is None:
            print(
                f"Warning: the release record holds no release commit for {version}, "
                f"so its Release carries no rlsbl-ci-sha marker. "
                f"The publish workflow reads that marker to learn which commit "
                f"CI proved green; record the version's release commit and re-run "
                f"`rlsbl release reconcile --plan` to put it back.",
                file=sys.stderr,
            )

        try:
            if pub is not None:
                if exists:
                    update_release(pub, gh=gh, config=ctx.config,
                                   directory=str(project_root))
                else:
                    create_release(pub, gh=gh, config=ctx.config,
                                   directory=str(project_root))
            else:
                # Markerless: the same document minus the release commit it does not
                # have, through the same two argv builders.
                body = (notes or f"Release {version}").rstrip("\n") + "\n"
                with notes_file(body, directory=str(project_root)) as path:
                    if exists:
                        args = edit_all_args(
                            tag_name, path, title=tag_name,
                            prerelease=is_prerelease(version),
                        )
                    else:
                        args = ["release", "create", tag_name, "--title",
                                tag_name, "--notes-file", path]
                        if is_prerelease(version):
                            args.append("--prerelease")
                    gh(args, config=ctx.config)
            written += 1
        except Exception as e:
            if exists:
                print(
                    f"Warning: failed to update release {tag_name}: {e}\n"
                    f"  Nothing was deleted, so {tag_name} still carries its "
                    f"previous Release -- with the pre-rewrite notes and "
                    f"marker. Re-run this command (it is idempotent) to write "
                    f"the current document.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Warning: failed to create release {tag_name}: {e}\n"
                    f"  The tag carries no GitHub Release, exactly as before. "
                    f"Re-run this command (it is idempotent) or create it from "
                    f"the version's CHANGELOG.md section.",
                    file=sys.stderr,
                )
    return written


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

# How many Releases one listing asks for. ``gh release list`` takes a --limit
# and nothing else -- there is no pagination flag and no total to compare
# against -- so a full answer and a truncated one are indistinguishable in the
# output. A listing that comes back AT the cap is therefore a hard error naming
# it, never a set of absent Releases to materialize.
_RELEASE_LIST_LIMIT = 1000

_LIST_TIMEOUT = 60


def plan_path(releases_dir):
    """The reconcile plan file for a project whose release record is *releases_dir*."""
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


def _transition_record_facts(transition_record_paths):
    """Release commit remaps, boundary aliases and identity transitions, merged."""
    from ..transition_record import (
        KIND_RELEASE_COMMIT_REMAP,
        KIND_BOUNDARY_ALIAS,
        KIND_IDENTITY_TRANSITION,
        read_events,
    )

    commit_map = {}
    origins = {}
    aliases = set()
    transitions = []
    seen_any = False
    for path in transition_record_paths:
        events = read_events(path, kinds=[
            KIND_RELEASE_COMMIT_REMAP, KIND_BOUNDARY_ALIAS, KIND_IDENTITY_TRANSITION,
        ])
        if events:
            seen_any = True
        for event in events:
            if event.KIND == KIND_RELEASE_COMMIT_REMAP:
                for mapping in event.mappings:
                    commit_map[mapping.old_sha] = mapping.new_sha
                    origins[mapping.old_sha] = (
                        f"transition record release-commit-remap {event.rewrite}"
                    )
            elif event.KIND == KIND_BOUNDARY_ALIAS:
                for alias in event.aliases:
                    aliases.add(alias.alias_tag)
            else:
                transitions.append(event)
    return (
        commit_map, origins, frozenset(aliases), tuple(transitions),
        "transition records" if seen_any else None,
    )


def collect_explanations(releases_dirs, transition_record_paths):
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

    (transition_record_map, transition_record_origins, aliases, transitions,
     transition_record_label) = _transition_record_facts(transition_record_paths)
    commit_map.update(transition_record_map)
    origins.update(transition_record_origins)
    if transition_record_label:
        present.append(transition_record_label)

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
# The release record heal: detect-and-heal, before any verdict is computed
# ---------------------------------------------------------------------------


def dangling_release_commits(releases_dir, *, git=None, cwd=None):
    """Archived versions whose release commit names a commit this repository lacks.

    Returns ``{version: release commit}``, empty when every release commit resolves.

    This is the state an out-of-band rewrite leaves behind: the local tags
    followed the rewrite, the archives did not, and the commits they name were
    pruned. The release record is the authority for where a released ref belongs, so
    until this is repaired every released ref reads as disagreeing with it.

    Only a RECORDED archive can be dangling. The other two fates name no
    commit at all and so can never name a missing one: an ``unrecoverable``
    version shipped from a commit nothing can name, and a ``never_released``
    one is a version number no release ever used.

    One ``git rev-list --no-walk --ignore-missing`` answers for the whole
    release record, so a repository with a hundred versions pays one git call rather
    than a hundred. An archive that cannot be read is skipped rather than
    guessed at: :func:`build_preview` reads the same file and raises its own
    error naming it.
    """
    from ..errors import RlsblError
    from ..release_file import (
        archived_release_path,
        list_archived_versions,
        read_release_file,
    )

    git = git or run
    release_commits = {}
    for version in list_archived_versions(releases_dir):
        try:
            archive = read_release_file(
                archived_release_path(releases_dir, version),
            )
        except (RlsblError, OSError):
            continue
        if archive.unrecoverable or archive.never_released:
            continue
        release_commit = (archive.candidate_sha or "").strip()
        if release_commit:
            release_commits[version] = release_commit
    if not release_commits:
        return {}

    out = git(
        "git",
        ["rev-list", "--no-walk", "--ignore-missing", *sorted(set(release_commits.values()))],
        cwd=cwd,
    )
    if not isinstance(out, str):
        # A recorded run: nothing was asked, so nothing is known, and a heal
        # decided from an unanswered question would be a guess.
        return {}
    present = [line.strip() for line in out.splitlines() if line.strip()]
    return {
        version: release_commit for version, release_commit in release_commits.items()
        if not any(_same_commit(release_commit, found) for found in present)
    }


def heal_dangling_release_commits(*, releases_dir, explanations, repo_root,
                          dry_run=False, log=print):
    """Move the release record's stale release commits through the recorded rewrite.

    The release commit half of detect-and-heal, and the counterpart to what the
    changelog side has done since scrubbing existed. It runs BEFORE the
    verdicts are computed, because the verdicts are computed AGAINST the
    release record: with the archives naming pruned commits, every released ref is
    classified ``refuse-foreign`` and the tripwire aborts the reconcile --
    refusing precisely the repair the command exists to perform.

    Returns ``{version: healed release commit}``, which the caller passes to
    :func:`build_preview` as ``release_commit_overrides``. Outside a dry run the
    archives on disk are rewritten (and committed) as well, so the two agree;
    under ``--dry-run`` nothing is written and the mapping is what keeps the
    preview truthful about a world that WOULD be healed first.

    Three rules, none of them inferred:

    * a dangling release commit no record explains is a hard error naming the version
      -- the heal is driven by the journal, a transition record release-commit-remap event or a
      committed scrub archive, never by resemblance;
    * the content check is ``refuse``: this command did not perform the
      rewrite, so it cannot state that a released tree changing is intended.
      ``rlsbl release scrub`` is the caller that can, and it declares so;
    * the rewritten archives and the transition record events beside them are committed,
      because a rewritten read-only archive left in the working tree is
      breakage for every other command and every other session.

    The heal is scoped to the release record being reconciled, not to every release record in
    the repository: a reconcile answers for one project's published metadata,
    and healing a sibling's archives (or being blocked by a content mismatch in
    one) would be a wider write than the command was asked for.
    """
    from ..release_commit_remap import (
        ON_CONTENT_CHANGE_REFUSE,
        transition_record_path_for_releases_dir,
        plan_release_commit_remap,
        record_release_commit_remap,
        remap_release_commits,
    )
    from ..errors import RlsblError

    dangling = dangling_release_commits(releases_dir, cwd=repo_root)
    if not dangling:
        return {}

    try:
        planned = plan_release_commit_remap(
            releases_dir, explanations.commit_map, cwd=repo_root,
            on_content_change=ON_CONTENT_CHANGE_REFUSE,
        )
    except RlsblError as exc:
        raise ReconcileError(
            f"the release record cannot be moved through the recorded "
            f"rewrite:\n{exc}"
        ) from exc

    healed = {remap.version: remap.new_sha for remap in planned}
    unexplained = {
        version: release_commit for version, release_commit in dangling.items()
        if version not in healed
    }
    if unexplained:
        listed = "".join(
            f"  {version}: released from {release_commit}\n"
            for version, release_commit in sorted(unexplained.items())
        )
        raise ReconcileError(
            f"the release record names commits this repository no longer has, "
            f"and no record explains where they went:\n{listed}"
            f"  An archive's candidate_sha is the commit that version shipped "
            f"from, and the release record is the authority for where every released "
            f"ref belongs -- so\n"
            f"  nothing can be judged against it while it names a pruned "
            f"commit. safegit's rewrite journal, a transition record release-commit-remap event "
            f"or a committed\n"
            f"  scrub archive would explain the move; none of them does. "
            f"Restore the commits, or repair the archives, and re-run."
        )

    log(
        f"The release record names {len(dangling)} commit(s) this repository "
        f"no longer has, and the recorded rewrite explains them:"
    )
    for remap in planned:
        origin = explanations.origins.get(
            remap.old_sha, "a recorded rewrite",
        )
        log(
            f"  {remap.version}: {remap.old_sha[:12]} -> "
            f"{remap.new_sha[:12]} ({origin})"
        )
    if dry_run:
        log(
            "  Dry run: the archives were NOT rewritten. The verdicts below "
            "are the ones a real run would compute, after healing them."
        )
        return healed

    rewrite_id = "; ".join(sorted({
        explanations.origins.get(remap.old_sha, "an out-of-band rewrite")
        for remap in planned
    })) or "an out-of-band rewrite"
    try:
        remaps = remap_release_commits(
            releases_dir, explanations.commit_map, cwd=repo_root,
            on_content_change=ON_CONTENT_CHANGE_REFUSE,
        )
    except RlsblError as exc:
        raise ReconcileError(
            f"the release record cannot be moved through the recorded "
            f"rewrite:\n{exc}"
        ) from exc
    touched = [remap.path for remap in remaps]
    transition_record_path = record_release_commit_remap(
        transition_record_path_for_releases_dir(releases_dir), rewrite_id, remaps,
    )
    if transition_record_path:
        touched.append(transition_record_path)
    if touched:
        try:
            run("safegit", [
                "commit", "-m",
                "reconcile: move the release record's release commits through the "
                "recorded rewrite",
                "--",
            ] + sorted(set(touched)))
        except Exception as exc:
            raise ReconcileError(
                f"the release record's release commits were moved, but the rewritten "
                f"archives could not be committed ({exc}). They are read-only "
                f"files every other command reads; commit or restore them "
                f"before re-running."
            ) from exc
        log(f"  Committed {len(set(touched))} rewritten release record file(s).")
    return healed


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

    A listing that comes back holding exactly :data:`_RELEASE_LIST_LIMIT`
    entries is refused: ``gh release list`` reports no total and offers no
    pagination, so a repository with more Releases than the cap would have the
    unlisted ones judged absent and proposed for creation.
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
    tags = [t.strip() for t in out.splitlines() if t.strip()]
    if len(tags) >= _RELEASE_LIST_LIMIT:
        raise ReconcileError(
            f"the GitHub Release listing came back at its {_RELEASE_LIST_LIMIT}"
            f"-entry limit, so it may be truncated. `gh release list` reports "
            f"no total and offers no pagination, so a truncated listing is "
            f"indistinguishable from a complete one -- and every unlisted "
            f"Release would be judged absent and proposed for creation. Raise "
            f"_RELEASE_LIST_LIMIT in rlsbl/commands/release_reconcile.py above "
            f"this repository's Release count and re-run."
        )
    return frozenset(tags), True


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


def _ref_verdict(*, refname, tag, version, release_commit, observation, explanations,
                 target, archived):
    """Classify one git ref. Returns a :class:`VerdictItem`."""
    remote = observation.remote_refs.get(refname)
    remote_peeled = observation.remote_refs.get(f"{refname}^{{}}", remote)
    local = observation.local_refs.get(refname)
    local_peeled = observation.local_refs.get(f"{refname}^{{}}", local)

    version_fact = f"version {version}" if version else "no archived version"

    # The release record is the authority for where a released ref belongs. A local ref
    # that disagrees with it is not a repair the reconcile may publish.
    if archived and local_peeled and release_commit and not _same_commit(
        local_peeled, release_commit,
    ):
        return VerdictItem(
            key=refname, state=STATE_REFUSE_FOREIGN,
            summary="the local ref does not match the release record",
            facts=(
                version_fact,
                f"local:  {local_peeled}",
                f"release record: {release_commit}",
            ),
            detail=(
                "  Pushing this ref would publish a commit the release record does "
                "not record as released.\n"
                "  Re-point the tag at the release commit, or repair the archive, "
                "before reconciling."
            ),
        )

    target_sha = local or (release_commit if archived else "")

    if remote is None:
        if not target_sha:
            return VerdictItem(
                key=refname, state=STATE_REFUSE_FOREIGN,
                summary="recorded as released but present nowhere",
                facts=(version_fact, "absent locally and on origin"),
                detail=(
                    "  There is no commit to create this ref at. Fetch the "
                    "ref, or record the version as unrecoverable."
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
            "  No rewrite journal entry, transition record release-commit-remap or committed "
            "scrub archive maps the origin value to this one.\n"
            "  Force-pushing over it could destroy work that is not part of "
            "any recorded rewrite."
        ),
    )


def _release_verdict(*, tag, version, release_commit, observation):
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
        facts=(f"version {version}", f"released from {release_commit}"),
        actions=(
            f"create the GitHub Release {tag} with the version's changelog "
            f"section and its rlsbl-ci-sha marker",
        ),
        data=RefAction(
            kind="release", tag=tag, version=version, target=release_commit,
        ),
    )


def build_preview(*, observation, explanations, target, ref_ctx, releases_dir,
                  release_commit_overrides=None):
    """One merged preview over every subject this repository owns.

    Subjects come from two places and are judged in one pass:

    * **the release record** -- every archived version's full ref set (its
      primary tag, its ecosystem companions and its recorded aliases, all from
      ``expected_refs``, the single authority), plus that version's GitHub
      Release;
    * **the local tag namespace** -- any tag the release record does not name that
      nonetheless diverges from origin. Those are outside the release record's account
      of what was released, so they are never materialized; they are only
      classified, which is what makes the publication tripwire fire for a
      repository that has no archives at all.

    A tag the transition record declares a ``non-version-tag`` is claimed too,
    for the same reason and by the same mechanism: :mod:`rlsbl.tag_explanation`
    is the one consultation over "is this tag explained?", and a tag an operator
    deliberately put OUTSIDE the version model is not a release this reconcile
    has an account of.  Judging it would make the tripwire fire forever on a
    nightly marker or an imported vendor tag.

    Two archive fates are skipped entirely, for different reasons:

    * an ``unrecoverable`` version has no commit, so there is nothing to
      compare a ref against and nothing to create one at;
    * a ``never_released`` version was never released at all, so it owns no ref
      origin could be wrong about and no GitHub Release that could be missing.
      Its would-be refs are CLAIMED even though no verdict is produced for
      them, so a tag carrying its name -- the phantom tag that is usually why
      such an archive exists -- never reaches the unarchived-tag pass below and
      can never fire the tripwire. rlsbl never recorded where that tag belongs,
      so it has nothing to say about where origin holds it; refusing would
      abort every reconcile on the repository forever.

    *release_commit_overrides* is :func:`heal_dangling_release_commits`' answer: the release commits
    the release record WOULD carry once healed, keyed by version. Outside a dry run the
    archives already say the same thing (they were rewritten before this ran),
    so it changes nothing; under ``--dry-run``, where nothing may be written,
    it is what keeps the preview from judging every released ref against a
    commit that no longer exists.
    """
    from ..errors import RlsblError
    from ..release_file import (
        archived_release_path,
        list_archived_versions,
        read_release_file,
    )
    from ..tag_explanation import build as build_tag_explanations

    items = []
    claimed = set()
    unrecoverable = []
    never_released = []

    # The one consultation over "is this tag explained?". Only the
    # non-version-tag answer changes anything here: the two archive-backed
    # sources name a version, which the release-record pass below judges on its
    # own terms.
    #
    # The records read are the TAG-NAMESPACE set, which is wider than the
    # version-keyed alias set: it carries the repository-scoped record too,
    # because that is where `rlsbl transition record --non-version-tag` writes
    # and a tag name is unique across a repository.
    outside_the_model = build_tag_explanations(
        transition_record_paths=ref_ctx.tag_explanation_record_paths,
    ).non_version_tags
    for tag in outside_the_model:
        claimed.add(f"refs/tags/{tag}")

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
        if archive.never_released:
            never_released.append(version)
            # Claim the refs it WOULD have owned, without judging any of them:
            # the version was never released, so there is nothing to compare
            # origin against, and leaving the refname unclaimed would hand a
            # phantom tag to the unarchived-tag pass, where a divergence is
            # refuse-foreign and aborts everything.
            try:
                for tag in target.expected_refs(version, ref_ctx).tags:
                    claimed.add(f"refs/tags/{tag}")
            except RlsblError:
                # The ref names could not be derived. Nothing is owed for this
                # version either way, so this costs only the claim -- a tag of
                # its name falls to the unarchived pass, which is where an
                # unrecognized tag belongs.
                pass
            continue
        if archive.unrecoverable:
            unrecoverable.append(version)
            continue
        release_commit = (
            (release_commit_overrides or {}).get(version)
            or (archive.candidate_sha or "").strip()
        )
        if not release_commit:
            raise ReconcileError(
                f"the release archive for {version} records no fate ({path}): "
                f"no release commit, no unrecoverable marker and no "
                f"never_released marker, so there is no commit its refs should "
                f"point at. Backfill it before reconciling."
            )

        try:
            expected = target.expected_refs(version, ref_ctx)
        except RlsblError as exc:
            # A ref set that cannot be derived -- two records disagreeing about
            # the spelling this version shipped under, a member whose config
            # does not resolve -- is a stated refusal, and the reconcile's own
            # error type is what its command surface catches. The reason is
            # carried verbatim: expected_refs already names both records.
            raise ReconcileError(
                f"the refs of {version} cannot be derived, so the reconcile "
                f"cannot say whether origin is right about them: {exc}"
            ) from exc
        for tag in expected.tags:
            refname = f"refs/tags/{tag}"
            claimed.add(refname)
            items.append(_ref_verdict(
                refname=refname, tag=tag, version=version, release_commit=release_commit,
                observation=observation, explanations=explanations,
                target=target, archived=True,
            ))
        if observation.releases_known:
            items.append(_release_verdict(
                tag=expected.primary, version=version, release_commit=release_commit,
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
            release_commit="", observation=observation, explanations=explanations,
            target=target, archived=False,
        )
        if verdict.state == STATE_ALREADY_CORRECT:
            continue
        items.append(verdict)

    preview = Preview(tuple(items))
    if outside_the_model:
        print(
            f"Skipping {len(outside_the_model)} tag(s) recorded outside the "
            f"version model (not releases, so no verdict is owed): "
            f"{', '.join(outside_the_model)}"
        )
    if never_released:
        print(
            f"Skipping {len(never_released)} version(s) recorded never released "
            f"(no release, so no ref or Release is owed): "
            f"{', '.join(never_released)}"
        )
    if unrecoverable:
        print(
            f"Skipping {len(unrecoverable)} version(s) recorded unrecoverable "
            f"(no commit to reconcile against): {', '.join(unrecoverable)}"
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

    from ..transition_record import now_timestamp

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
    # An empty array-of-tables serializes to nothing at all, and `items` is a
    # required member -- so a plan that found nothing writes an empty array
    # rather than a document the validator rejects.
    doc.add("items", items if preview.items else tomlkit.array())
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


# The verdicts a plan item can carry that an apply would ACT on, in the
# hyphenated spelling the plan file records.
_REPAIRABLE_LABELS = frozenset({
    STATE_MATERIALIZE.replace("_", "-"),
    STATE_RE_POINT.replace("_", "-"),
})


def check_plan_covers(plan, preview, path):
    """Refuse an apply whose fresh observation names work the plan does not.

    The plan file IS the consent, so the apply performs exactly the repairable
    items the operator read -- never a freshly derived set that happens to be
    larger. :func:`check_plan_matches` cannot answer this on its own:
    ``world_digest`` covers the REMOTE by design (it is the force-push lease
    material), so a purely LOCAL change between plan and apply -- a tag
    fetched, a tag created, a tag moved -- leaves the digest valid while the
    fresh preview grows a subject, or re-points an existing one at a different
    commit. Both are writes nobody previewed.

    Three refusals, each naming what it saw:

    * a fresh actionable subject the plan does not name at all;
    * a planned subject whose verdict changed (both verdicts are named);
    * a planned subject whose lease or target commit moved.

    A planned repairable item the fresh observation no longer names became
    correct on its own; those keys are RETURNED, so the caller can report them
    as no-ops rather than treat their absence as a mismatch.
    """
    planned = {entry.key: entry for entry in plan.items}
    filename = os.path.basename(path)
    re_plan = (
        f"  Re-run `rlsbl release reconcile --plan`, read the new plan, and "
        f"apply that."
    )

    for item in preview.items:
        action = item.data
        if not isinstance(action, RefAction):
            continue
        entry = planned.get(item.key)
        if entry is None:
            raise ReconcileError(
                f"the world grew a subject {filename} does not cover: "
                f"{item.key}.\n"
                f"  now: {item.state_label} -- {item.summary}\n"
                f"  The plan's items are the consent, and its world_digest "
                f"covers the remote only -- a tag brought local since the plan "
                f"was written changes\n"
                f"  nothing the digest can see while enlarging what an apply "
                f"would touch.\n{re_plan}"
            )
        if entry.state != item.state_label:
            raise ReconcileError(
                f"the verdict for {item.key} changed since {filename} was "
                f"written.\n"
                f"  planned: {entry.state}\n"
                f"  now:     {item.state_label} -- {item.summary}\n"
                f"  Applying it would perform an action the plan does not "
                f"describe.\n{re_plan}"
            )
        if (entry.observed or "") != (action.observed or ""):
            raise ReconcileError(
                f"the force-push lease for {item.key} changed since "
                f"{filename} was written.\n"
                f"  planned: {entry.observed or '<absent>'}\n"
                f"  now:     {action.observed or '<absent>'}\n{re_plan}"
            )
        if (entry.target or "") != (action.target or ""):
            raise ReconcileError(
                f"the commit {item.key} would be pushed to changed since "
                f"{filename} was written.\n"
                f"  planned: {entry.target or '<absent>'}\n"
                f"  now:     {action.target or '<absent>'}\n"
                f"  The verdict is unchanged, so the digest still matches: "
                f"this moved LOCALLY. Applying it would publish a commit the "
                f"plan never named.\n{re_plan}"
            )

    fresh_keys = {
        item.key for item in preview.items if isinstance(item.data, RefAction)
    }
    return [
        entry.key for entry in plan.items
        if entry.state in _REPAIRABLE_LABELS and entry.key not in fresh_keys
    ]


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
    """The Release document for one version, from the changelog and the release record."""
    from ..release_publication import release_commit_from_record, publication

    notes = ""
    if changelog_path and os.path.exists(changelog_path):
        notes = extract_changelog_entry(changelog_path, action.version) or ""
    release_commit = (
        release_commit_from_record(releases_dir, action.version) or action.target
    )
    return publication(
        tag=action.tag, version=action.version, candidate_sha=release_commit,
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
            # workflow reads. Re-pointed here, from the release record's release commit.
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
    release record-driven half is empty there anyway. What remains is the local-tag
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
    """Reconcile this project's published refs and Releases with its records.

    Both halves run through here. ``--plan`` writes the plan file (empty plan
    included) and performs no per-item apply; ``--apply`` reads that plan back,
    refuses when the remote or the plan's own subjects moved under it, and then
    performs what it named.

    The command is ``consequential`` as a whole, so ``--plan`` prompts as well.
    That is deliberate rather than an oversight: consent is for running the
    command, and making it depend on which half was elected would put a flag in
    charge of whether a human is asked.
    """
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

    if mode == "apply":
        # Managed-repo hygiene: the apply force-pushes refs and rewrites
        # published Release documents. A stash is work with no branch of its
        # own, and nothing here can carry it through that.
        from ..git_util import refuse_present_stash

        try:
            refuse_present_stash(
                str(ctx.workspace_root or ctx.project_root),
                operation="reconcile",
                detail=(
                    "The apply pushes refs, force-pushes the ones a recorded "
                    "rewrite moved, and rewrites published Release documents."
                ),
                error=ReconcileError,
            )
        except ReconcileError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    try:
        target, ref_ctx, releases_dir = _resolve_identity(ctx)
    except ReconcileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    transition_record_paths = ref_ctx.transition_record_paths
    path_of_plan = plan_path(releases_dir)
    changelog_path = _changelog_path(ctx)
    push_timeout = get_push_timeout(
        ctx.config, override=flags.get("push-timeout"),
    )

    state = {}
    repo_root = str(ctx.workspace_root or ctx.project_root)

    # Detect-and-heal, before anything is judged: the verdicts are computed
    # AGAINST the release record, so a release record naming pruned commits has to be moved
    # through the same records that explain the divergence first. Outside the
    # observation, because it writes.
    try:
        explanations = collect_explanations([releases_dir], transition_record_paths)
        release_commit_overrides = heal_dangling_release_commits(
            releases_dir=releases_dir, explanations=explanations,
            repo_root=repo_root, dry_run=dry_run,
        )
    except ReconcileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    def _observe():
        observation = observe_world(ctx=ctx)
        state["observation"] = observation
        state["explanations"] = explanations
        preview = build_preview(
            observation=observation, explanations=explanations,
            target=target, ref_ctx=ref_ctx, releases_dir=releases_dir,
            release_commit_overrides=release_commit_overrides,
        )
        if mode == "apply":
            plan = read_plan(path_of_plan)
            check_plan_matches(plan, observation, path_of_plan)
            state["plan"] = plan
            if refusals(preview):
                raise ReconcileError(tripwire_error(preview))
            # The plan's items are the consent: the fresh preview may not name
            # a repairable subject the plan does not, and a planned subject may
            # not have changed under it.
            state["noops"] = check_plan_covers(plan, preview, path_of_plan)
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
            "no transition record release-commit-remap record and no committed scrub archive. "
            "Only refs origin already agrees with, and refs it is missing "
            "entirely, can be reconciled."
        )

    if not preview.items:
        print("Nothing to reconcile: origin matches this repository's records.")

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
        # An EMPTY plan is still written. The two halves are one flow, and a
        # plan half that writes nothing when it found nothing would leave the
        # apply half telling the operator to run the plan first -- which they
        # just did.
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
    for key in state.get("noops") or []:
        print(f"  {key}: already correct by the time the plan was applied.")
    if os.path.exists(path_of_plan):
        effects.remove(path_of_plan)
