"""Absorb an external repository into a workspace, as a releasable.

``rlsbl monorepo absorb <source_repo> <dest_path>`` is the INBOUND conversion,
and it is the mirror image of ``rlsbl monorepo extract``: the source's history
is rewritten to live under ``dest_path``, merged in, its version tags imported
under the destination's tag scheme, and its whole release state -- changelog,
release archives with their release commits, config and version -- moved into a
releasable's state directory.

The unit is the RELEASABLE, in both directions
----------------------------------------------

An absorbed repository always arrives as a releasable, because that is what it
already was: it owns a version, a changelog, a release-file archive and a tag
scheme, and a member package owns none of those on its own. ``--releasable``
names an existing group to join; without it an AUTO-SINGLETON releasable is
created for the arriving member, with its ``tag_format`` written EXPLICITLY --
derived from the member's primary target's scheme, never inherited by accident.
A source whose targets span both tag schemes (Go's ``{path}/v*`` and everybody
else's ``{name}@v*``) is refused with the remedy: state the format with
``--tag-format``.

The shape: observe, then either render or apply
-----------------------------------------------

The command is a reconciler built on :mod:`rlsbl.preview_apply`. Observation
runs under :func:`~rlsbl.preview_apply.no_writes` and answers everything the
apply acts on -- which tags import, which collide, what the state migration
carries, whether a previous run already did some of it. ``--dry-run`` renders
that plan and stops; otherwise the plan is applied item by item, in the order
it was rendered.

Refusals happen during observation, so they cost nothing and fire identically
under a preview: a source that is missing, not a git repository, or dirty; a
broken target declaration; mixed tag schemes; a destination path already taken
(on disk or in workspace.toml); a member name already used; a releasable named
that does not exist, or one that exists with no release state; a tag whose NAME
or whose VERSION already exists in the destination; a dirty workspace; and a
missing ``git-filter-repo`` or ``saferm``.

What an apply moves
-------------------

* **History**, via ``git-filter-repo --to-subdirectory-filter`` on a working
  clone under the workspace's own ``.git/rlsbl/``, fetched in with
  ``--no-tags`` and merged with ``--allow-unrelated-histories``.
* **Tags**, created by rlsbl at the mapped commits rather than fetched: the
  fetch is deliberately tag-free, so a tag the destination already owns can
  never be overwritten or deleted by the import. One boundary alias is created
  at the current version, keeping the source's own tag name resolvable.
* **The release state**: the arriving ``.rlsbl/changes/`` and
  ``.rlsbl/releases/`` move into the releasable's state directory, their
  changelog hashes remapped through filter-repo's commit map and their release
  release commits remapped and VERIFIED -- a recorded tree hash is content-addressed,
  so a faithful rewrite reproduces it exactly.
* **A transition record** in the releasable's state directory explains all of it.

Re-running a crashed absorb
---------------------------

Every step is detected before it is repeated: the merge by its own trailer plus
the source's root-commit identity AND the releasable it recorded, a tag by
already existing at the mapped commit, the workspace entry by its content, and
a changelog entry by its id -- or, for an entry that carries none, by its
content. A run interrupted anywhere can be re-run to completion without
duplicating what already happened.

A heal re-derives NOTHING. Every value it writes comes from what the first run
recorded: the trailers and the state already migrated. The re-run's source
repository answers one question -- is this the same conversion? -- so a fork
whose manifest moved on cannot overwrite the version this conversion shipped.
A re-run aimed at a DIFFERENT releasable is not that conversion at all and is
refused, because healing skips the version-overlap check on exactly the ground
that the target is unchanged.

What it does NOT do: push anything (the tags it creates are local), touch the
source repository, or administer any external system. Those are next steps.
"""

import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass, field

from ...changelog.files import load_filter_repo_commit_map, remap_jsonl_hashes
from ...changelog.schema import entry_content_key, parse_jsonl, serialize_entry
from ...config import read_json_config
from ...errors import ConfigError
from ...transition_record import (
    ReleaseCommitMapping,
    ReleaseCommitRemapEvent,
    BoundaryAlias,
    BoundaryAliasEvent,
    ConversionEvent,
    TransitionRecordEndpoint,
    TagMapEvent,
    TagMapping,
    append_events,
    get_transition_record_path,
)
from ...lock import rlsbl_lock
from ...ownership import ROOT_MEMBER_NAME, normalize_path
from ...preview_apply import Preview, Reconciler, VerdictItem, reconcile
from ...release_file import read_release_file, write_release_commit
from ...saferm import saferm_delete
from ...snapshot import SNAPSHOT_FILE, generate_snapshot, write_snapshot
from ...tag_glob import TagMode, parse_version_tag, releasable_tag_glob
from ...utils import commit_files, is_clean_tree, working_tree_paths
from ...workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    WorkspaceProject,
    find_workspace_root,
    get_releasable_changes_dir,
    get_releasable_dir,
    load_releasables,
    load_workspace,
    read_releasable_version,
    save_workspace,
    write_releasable_version,
)
from ...workspace_graph import WorkspaceGraph
from ...workspace_types import DEFAULT_TAG_FORMAT, Releasable
from ... import effects
from .extract import (
    ExtractError,
    _ensure_git_identity,
    _prune_dangling_entries,
    _run_filter_repo,
    _run_git,
    require_filter_repo,
)


class AbsorbError(ExtractError):
    """A refusal or failure in the inbound conversion.

    A subclass of the conversion error both directions share, so a caller that
    catches ``ExtractError`` around either conversion keeps working while an
    absorb-specific handler can still name its own.
    """


# ---------------------------------------------------------------------------
# Preview item keys -- also the apply pipeline's order
# ---------------------------------------------------------------------------

ITEM_SOURCE = "source"
ITEM_RELEASABLE = "releasable"
ITEM_HISTORY = "history"
ITEM_TAGS = "tags"
ITEM_STATE = "state"
ITEM_WORKSPACE = "workspace"
ITEM_TRANSITION_RECORD = "transition-record"
ITEM_NEXT_STEPS = "next-steps"

#: The trailer key naming what an absorb merge commit absorbed. Together with
#: :data:`SOURCE_TRAILER` it is how a re-run recognizes its own earlier merge.
ABSORB_TRAILER = "Rlsbl-Absorb"

#: The trailer key carrying the SOURCE's identity -- its root commit, which no
#: rename, move or re-clone of the source repository changes.
SOURCE_TRAILER = "Rlsbl-Absorb-Source"

#: The trailer key naming the RELEASABLE the absorb targeted. It is part of the
#: heal identity, not decoration: healing skips the version-overlap check on the
#: grounds that a re-run is the same conversion, and "the same conversion" is
#: only true if it targets the same releasable.
RELEASABLE_TRAILER = "Rlsbl-Absorb-Releasable"

#: Where the working clone is made: inside the workspace's own git directory,
#: which is neither part of the working tree nor anybody else's scratch.
CLONE_PARENT = os.path.join(".git", "rlsbl")

#: rlsbl's own advisory lock, relative to a workspace root.
LOCK_RELPATH = f"{WORKSPACE_DIR}/lock"


# ---------------------------------------------------------------------------
# Observation record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TagImport:
    """One source tag's translation into the destination's scheme."""

    old_tag: str
    new_tag: str
    version: str
    old_sha: str


@dataclass(frozen=True)
class TagPlan:
    """What the conversion will do to the destination's tags."""

    #: Every version tag that will be created under the destination scheme.
    imports: tuple = ()
    #: Source tags that are not version tags under any scheme: never imported.
    skipped: tuple = ()
    #: ``(alias_tag, aliased_new_tag, version)`` -- the one boundary alias.
    alias: tuple | None = None
    #: Planned tags that ALREADY exist in the destination (a healed re-run).
    already_present: tuple = ()


@dataclass(frozen=True)
class SourceState:
    """The release state the source repository carries in its ``.rlsbl/``."""

    unreleased_entries: int = 0
    versioned: tuple = ()   # version strings with a released JSONL
    archives: tuple = ()    # archived release file names (v<x>.toml)
    has_config: bool = False


@dataclass
class Arrival:
    """Everything observation resolved about one absorption."""

    workspace_root: str
    source_repo: str
    dest_path: str
    name: str
    registry_name: str
    releasable_name: str
    creates_releasable: bool
    tag_format: str
    version: str
    source_root_sha: str
    source_repo_url: str
    source_tag_format: str | None
    projects: list
    releasables: list
    tag_plan: TagPlan
    state: SourceState
    target_names: tuple
    merge_commit: str | None      # set when a previous run already merged
    member_present: bool
    delete_with_rm: bool

    @property
    def healing(self) -> bool:
        """Is this a re-run over a previous absorb's partial work?"""
        return self.merge_commit is not None

    @property
    def clone_path(self) -> str:
        return os.path.join(
            self.workspace_root, CLONE_PARENT, f"absorb-{self.name}",
        )

    @property
    def releasable_dir(self) -> str:
        return get_releasable_dir(self.workspace_root, self.releasable_name)

    @property
    def dest_full(self) -> str:
        return os.path.join(self.workspace_root, self.dest_path)

    @property
    def member_rlsbl_dir(self) -> str:
        return os.path.join(self.dest_full, ".rlsbl")


@dataclass
class Applied:
    """What the apply pipeline learned as it ran, passed between its steps."""

    sha_map: dict = field(default_factory=dict)
    pruned_shas: list = field(default_factory=list)
    tag_mappings: list = field(default_factory=list)
    release_commit_mappings: list = field(default_factory=list)
    unremapped_release_commits: list = field(default_factory=list)
    entries_migrated: int = 0
    entries_already_present: int = 0
    #: Repo-relative paths this run wrote or deleted, for the scoped commit.
    written: list = field(default_factory=list)
    state_commit: str = ""
    alias_commit: str = ""
    stack: ExitStack | None = None

    def record_path(self, workspace_root, *paths):
        """Record repo-relative paths for the commit this run will make.

        Deliberately not spelled ``touch``: that name is a filesystem mutation
        everywhere else in this codebase, and the chokepoint scanner reads it
        as one -- a bookkeeping method has no business borrowing it.
        """
        for path in paths:
            rel = os.path.relpath(path, workspace_root)
            if rel not in self.written:
                self.written.append(rel)


# ---------------------------------------------------------------------------
# Small git / filesystem helpers
# ---------------------------------------------------------------------------


def _tree_hash(repo, path, rev="HEAD"):
    """The git tree object of ``path`` in ``repo`` at ``rev``."""
    spec = f"{rev}^{{tree}}" if path in ("", ".") else f"{rev}:{path}"
    return _run_git(repo, "rev-parse", spec)


def _git_tag_names(repo, pattern=None):
    """The tag names in ``repo``, optionally filtered by a glob."""
    args = ["tag", "-l"]
    if pattern is not None:
        args.append(pattern)
    return [t for t in _run_git(repo, *args).splitlines() if t.strip()]


def _origin_url(repo):
    """``origin``'s URL, or the repository's own path when it has no remote."""
    try:
        url = _run_git(repo, "remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        return os.path.abspath(repo)
    return url or os.path.abspath(repo)


def _root_commits(repo):
    """The parentless commits of ``repo`` -- its durable identity."""
    out = _run_git(repo, "rev-list", "--max-parents=0", "HEAD")
    return [line.strip() for line in out.splitlines() if line.strip()]


def _commit_exists(repo, sha):
    """Does ``sha`` name a commit object that exists in ``repo``?"""
    result = effects.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=str(repo), capture_output=True, text=True, timeout=600,
    )
    return result.returncode == 0


def _dirty_paths(root):
    """Working-tree changes at ``root``, minus rlsbl's own advisory lock."""
    return [
        path for path in working_tree_paths(cwd=root)
        if path.rstrip("/") != LOCK_RELPATH
    ]


def _delete_path(path, *, description, delete_with_rm):
    """Delete ``path`` through saferm, or through ``rm -rf`` when asked to."""
    if not os.path.exists(path):
        return
    if delete_with_rm:
        if os.path.isdir(path):
            effects.rmtree(path)
        else:
            effects.remove(path)
        return
    saferm_delete(
        path,
        recursive=os.path.isdir(path),
        description=description,
        install_hint=(
            "Install saferm, or re-run with --delete-with-rm to use a plain "
            "rm -rf instead."
        ),
    )


def _write_json(path, data, *, exists_ok=True):
    """Write a JSON config file at 644, or preserve the mode it already has.

    A fresh config is an ordinary readable file (644 through the umask); a
    rewrite keeps whatever mode the file already carries. Pinning a mode here
    is how a 644 config silently became owner-only.
    """
    effects.makedirs(os.path.dirname(path), exist_ok=True)
    body = json.dumps(data, indent=2) + "\n"
    if exists_ok and os.path.isfile(path):
        effects.atomic_write_text(path, body, preserve_mode=True)
    else:
        effects.atomic_write_text(path, body)


def _relative(root, path):
    return os.path.relpath(path, root)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def _check_source_repo(source_repo):
    """The source must exist, be a git repository, and be committed."""
    if not os.path.isdir(source_repo):
        raise AbsorbError(f"source repo path does not exist: {source_repo}")
    if not os.path.isdir(os.path.join(source_repo, ".git")):
        raise AbsorbError(f"source path is not a git repository: {source_repo}")
    if not is_clean_tree(cwd=source_repo):
        raise AbsorbError(
            f"source repository has uncommitted changes: {source_repo}. "
            f"Commit or discard them before absorbing -- the history rewrite "
            f"only captures committed state, so uncommitted changes would be "
            f"silently lost."
        )


def _source_targets(source_repo):
    """The source's declared or detected targets, refusing a broken declaration.

    The source's ``.rlsbl/config.json`` becomes the absorbed unit's config and
    its targets decide the tag scheme, so a config file with no ``targets`` key
    is refused HERE -- before any history is rewritten -- rather than surfacing
    later as a mis-schemed tag import. A source with no config file at all is
    the legitimate auto-detect path.
    """
    from ...targets import detect_targets

    try:
        return detect_targets(source_repo)
    except ConfigError as exc:
        raise AbsorbError(
            f"source repository '{source_repo}' has a broken target "
            f"declaration and cannot be absorbed: {exc} A repository with a "
            f".rlsbl/config.json must include a \"targets\" key (a repository "
            f"with no .rlsbl/config.json is fine -- targets are auto-detected)."
        ) from exc


def _derive_tag_format(source_repo, entries, name, dest_path):
    """The tag format the auto-singleton releasable is created with.

    Derivation is shared with ``monorepo add``, the other command that creates
    a releasable from a member (:func:`rlsbl.tag_glob.derive_releasable_tag_format`);
    only the subject of the mixed-scheme refusal differs, since here it is the
    source repository's own declaration that spans both schemes.
    """
    from ...errors import MixedTagSchemeError
    from ...tag_glob import derive_releasable_tag_format

    try:
        return derive_releasable_tag_format(
            entries, name, dest_path,
            subject=f"source repository '{source_repo}'",
        )
    except MixedTagSchemeError as exc:
        raise AbsorbError(str(exc)) from exc


def _version_key(version):
    """Order two version strings, pre-releases before their stable base.

    The release archives' own ordering, so "which of these versions is the
    newest" has one answer in the tool rather than a second ordering written
    here. A source repository's tags are not rlsbl's to name, so a version
    outside that vocabulary sorts lowest rather than refusing: it is a
    candidate for ``max`` here, never a version rlsbl records.
    """
    from ...release_file import archive_sort_key, is_release_version

    if not is_release_version(version):
        return (0, 0, 0, 0, 0, 0)
    return archive_sort_key(version)


def _resolve_version(source_repo, entries, version_tags):
    """The version the absorbed unit arrives at.

    Its own manifest first -- that is what the project says about itself -- and
    its highest version tag second. A source that answers neither is a refusal:
    a releasable's version file is state rlsbl reads for real, and inventing a
    ``0.0.0`` for it would be a lie in a file the release flow bumps from.
    """
    from ...targets import TARGETS

    for entry in entries:
        target = TARGETS.get(entry.name)
        if target is None:
            continue
        try:
            version = target.read_version(entry.path)
        except Exception:
            continue
        if version:
            return str(version).strip()
    if version_tags:
        return max((t.version for t in version_tags), key=_version_key)
    raise AbsorbError(
        f"cannot determine the version of '{source_repo}': its manifest "
        f"declares none that rlsbl can read, and it carries no version tag. "
        f"A releasable's version file is real state -- the release flow bumps "
        f"from it -- so it is never invented. Set the version in the source's "
        f"manifest, or tag its current release, and re-run."
    )


def _arrival_version(workspace_root, source_repo, entries, version_tags, *,
                     releasable_name, healing, creates_releasable):
    """The version the absorbed unit arrives at -- RECORDED before derived.

    A heal re-derives nothing. The first run already wrote this unit's version
    into the releasable it created, and that record is what every later step
    reads; asking the re-run's source again would let a fork with a bumped
    manifest overwrite the recorded version with one this conversion never
    shipped. The re-run's source repository answers the identity question (is
    this the same conversion?) and nothing else.

    Only a releasable this absorb CREATED has a version it recorded. When the
    member joined an existing releasable, that releasable's version is its own
    and the absorb never writes it, so there is nothing recorded to prefer and
    the source is asked as on a first run.
    """
    if healing and creates_releasable:
        try:
            recorded = read_releasable_version(workspace_root, releasable_name)
        except Exception:
            recorded = None
        if recorded:
            return str(recorded).strip()
    return _resolve_version(source_repo, entries, version_tags)


def _trailer_values(body, key):
    """Every value the commit message ``body`` records under trailer ``key``."""
    return [
        line.split(":", 1)[1].strip()
        for line in body.splitlines()
        if line.startswith(key + ":")
    ]


def _find_merge(workspace_root, name, dest_path, source_root_shas, releasable):
    """The commit of a previous absorb of this unit, or None.

    Detection is the trailer this command writes PLUS two identities the
    trailers carry:

    * the SOURCE's root commit -- the same name and destination path absorbed
      from a DIFFERENT repository is not a re-run of this conversion, it is a
      collision, and it is refused rather than healed;
    * the target RELEASABLE -- healing skips :func:`_check_version_overlap` on
      the grounds that a re-run is the same conversion, so a re-run aimed at
      another releasable must not be classified as one. It would skip exactly
      the check that guards the releasable it is newly pointing at.

    A merge that carries no releasable trailer cannot answer the second
    question, so it is refused rather than guessed at. That state only exists
    for a merge written before the trailer did.
    """
    marker = f"{ABSORB_TRAILER}: {name} {dest_path}"
    try:
        out = _run_git(
            workspace_root, "log", "-F", f"--grep={marker}",
            "--format=%H%x00%B%x1e",
        )
    except subprocess.CalledProcessError:
        return None
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, body = record.partition("\x00")
        if marker not in body:
            continue
        recorded = _trailer_values(body, SOURCE_TRAILER)
        if recorded and not set(recorded) & set(source_root_shas):
            raise AbsorbError(
                f"'{dest_path}' was already absorbed as '{name}' by commit "
                f"{sha[:12]}, from a DIFFERENT repository (its root commit was "
                f"{recorded[0][:12]}, this source's is "
                f"{source_root_shas[0][:12]}). Two repositories cannot occupy "
                f"one member path. Absorb this one under another name and "
                f"path, or remove the existing member first."
            )
        targeted = _trailer_values(body, RELEASABLE_TRAILER)
        if not targeted:
            raise AbsorbError(
                f"'{dest_path}' was already absorbed as '{name}' by commit "
                f"{sha[:12]}, but that merge records no "
                f"{RELEASABLE_TRAILER} trailer, so which releasable it targeted "
                f"is not knowable. Completing it would skip the version-overlap "
                f"check against a releasable this run merely assumes -- so it "
                f"is refused. Finish that absorption by hand, or reset this "
                f"repository to before {sha[:12]} and absorb again."
            )
        if releasable not in targeted:
            raise AbsorbError(
                f"'{dest_path}' was already absorbed as '{name}' by commit "
                f"{sha[:12]}, targeting releasable '{targeted[0]}'; this run "
                f"targets '{releasable}'. A re-run completes the SAME "
                f"conversion -- it skips the version-overlap check on exactly "
                f"that ground -- so it may not be re-aimed. Re-run with "
                f"--releasable {targeted[0]} to complete it, or undo that "
                f"absorption before absorbing into '{releasable}'."
            )
        return sha.strip()
    return None


def _released_versions(state_dir):
    """Every version a releasable's own state says it has already released."""
    versions = set()
    changes_dir = os.path.join(state_dir, "changes")
    if os.path.isdir(changes_dir):
        versions |= {
            name[: -len(".jsonl")]
            for name in os.listdir(changes_dir)
            if name.endswith(".jsonl") and name != "unreleased.jsonl"
        }
    releases_dir = os.path.join(state_dir, "releases")
    if os.path.isdir(releases_dir):
        versions |= {
            name[1: -len(".toml")]
            for name in os.listdir(releases_dir)
            if name.startswith("v") and name.endswith(".toml")
        }
    return versions


def _check_version_overlap(workspace_root, releasable_name, source_repo,
                           state, version_tags):
    """Refuse a version the destination releasable has already released.

    A tag-name collision is about SPELLING; this is about the release record,
    which is where the fact really lives: a releasable's ``changes/<v>.jsonl``
    and ``releases/v<v>.toml`` say which versions it shipped, whatever its tags
    happen to be named. Two releases of one version is a state no later command
    can make sense of -- ``changelog generate`` would have two sources for one
    section and the unreleased range would be bounded by a version with two
    release commits -- so it is refused before anything is written.
    """
    incoming = set(state.versioned)
    incoming |= {name[1: -len(".toml")] for name in state.archives}
    incoming |= {tag.version for tag in version_tags}
    present = _released_versions(get_releasable_dir(workspace_root, releasable_name))
    overlap = sorted(incoming & present)
    if not overlap:
        return
    raise AbsorbError(
        f"version collision: releasable '{releasable_name}' has already "
        f"released {', '.join(overlap)}, and '{source_repo}' carries the same "
        f"version(s). One version is one release, and the two records cannot "
        f"both be that version's -- absorb into a releasable that does not "
        f"carry {overlap[0]}, or reconcile the histories first."
    )


def _plan_tags(workspace_root, source_repo, releasable_name, tag_format,
               version, *, glob, healing):
    """Classify every source tag, and refuse both kinds of collision.

    Names are resolvable before anything is written: the destination's tags are
    already there, and the imported names are a pure function of the source's
    tags and the destination's format. The SHAs are not -- filter-repo assigns
    them -- so they are resolved at apply time.

    Two refusals, both here:

    * a **ref-name collision**: the tag rlsbl would create already exists. It
      is never overwritten and never deleted -- a destination tag belongs to
      the destination's own release history.
    * a **same-version collision**: a tag matching this releasable's glob
      already stands at a version the source also carries, under whatever
      spelling. The unit would then have two releases claiming one version.

    A tag that already exists AND was planned is reported as present rather
    than refused when a previous run of THIS absorb created it (healing); its
    commit is verified at apply time.
    """
    present = set(_git_tag_names(workspace_root))
    own_present = {}
    for tag in _git_tag_names(workspace_root, glob):
        parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
        if parsed is not None:
            own_present[parsed.version] = tag

    imports = []
    skipped = []
    already = []
    for tag in sorted(_git_tag_names(source_repo)):
        parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
        if parsed is None:
            skipped.append(tag)
            continue
        sha = _run_git(source_repo, "rev-list", "-n", "1", tag)
        new_tag = tag_format.format(name=releasable_name, version=parsed.version)
        collision = own_present.get(parsed.version)
        if collision is not None and collision != new_tag:
            raise AbsorbError(
                f"version collision: the source's '{tag}' would arrive as "
                f"version {parsed.version}, but '{collision}' already stands "
                f"at that version for releasable '{releasable_name}'. One "
                f"version is one release; resolve the overlap (retag the "
                f"source, or absorb into a releasable that does not carry "
                f"{parsed.version}) before re-running."
            )
        if new_tag in present:
            if not healing:
                raise AbsorbError(
                    f"tag collision: the source's '{tag}' would be imported as "
                    f"'{new_tag}', but a tag named '{new_tag}' already exists "
                    f"in this repository and is not this absorb's. A "
                    f"destination tag is never overwritten or deleted -- "
                    f"resolve the conflicting tag before absorbing."
                )
            already.append(new_tag)
        imports.append(
            TagImport(old_tag=tag, new_tag=new_tag, version=parsed.version,
                      old_sha=sha)
        )

    alias = None
    for entry in imports:
        if entry.version != version or entry.old_tag == entry.new_tag:
            continue
        if entry.old_tag in present:
            if not healing:
                raise AbsorbError(
                    f"boundary alias collision: the source's own '"
                    f"{entry.old_tag}' is kept beside '{entry.new_tag}' so the "
                    f"pre-conversion name still resolves, but a tag named "
                    f"'{entry.old_tag}' already exists in this repository. "
                    f"Resolve the conflicting tag before absorbing."
                )
            already.append(entry.old_tag)
        alias = (entry.old_tag, entry.new_tag, entry.version)
        break

    return TagPlan(
        imports=tuple(imports),
        skipped=tuple(skipped),
        alias=alias,
        already_present=tuple(already),
    )


def _read_source_state(source_repo):
    """What the source's ``.rlsbl/`` carries: changelog, archives, config."""
    changes_dir = os.path.join(source_repo, ".rlsbl", "changes")
    releases_dir = os.path.join(source_repo, ".rlsbl", "releases")
    unreleased = os.path.join(changes_dir, "unreleased.jsonl")
    versioned = []
    if os.path.isdir(changes_dir):
        versioned = sorted(
            name[: -len(".jsonl")]
            for name in os.listdir(changes_dir)
            if name.endswith(".jsonl") and name != "unreleased.jsonl"
        )
    archives = []
    if os.path.isdir(releases_dir):
        archives = sorted(
            name for name in os.listdir(releases_dir)
            if name.startswith("v") and name.endswith(".toml")
        )
    return SourceState(
        unreleased_entries=(
            len(parse_jsonl(unreleased)) if os.path.isfile(unreleased) else 0
        ),
        versioned=tuple(versioned),
        archives=tuple(archives),
        has_config=os.path.isfile(
            os.path.join(source_repo, ".rlsbl", "config.json")
        ),
    )


def resolve_arrival(workspace_root, source_repo, dest_path, *, name,
                    registry_name, releasable_name, tag_format,
                    delete_with_rm):
    """Resolve and validate one absorption. Reads only; refuses loudly."""
    workspace_root = os.path.abspath(workspace_root)
    source_repo = os.path.abspath(source_repo)
    dest_path = normalize_path(dest_path)

    if not dest_path:
        raise AbsorbError(
            "the destination path is the repository root, which already has a "
            "member: every workspace has exactly one root member and it owns "
            "every file no other member claims. Absorb into a subdirectory."
        )
    name = name or os.path.basename(dest_path)
    if name == ROOT_MEMBER_NAME:
        raise AbsorbError(
            f"'{ROOT_MEMBER_NAME}' is reserved for the member that owns the "
            f"repository root (path \".\"). Pass --name to give the absorbed "
            f"member a name of its own."
        )

    _check_source_repo(source_repo)
    require_filter_repo()
    if not delete_with_rm and shutil.which("saferm") is None:
        raise AbsorbError(
            "saferm is not installed, and the arriving package's per-package "
            "release state has to be deleted once it moves to the releasable. "
            "Install saferm (deletions get an audit trail and stay "
            "recoverable), or re-run with --delete-with-rm to use a plain "
            "rm -rf instead."
        )

    projects = load_workspace(workspace_root)
    releasables = load_releasables(workspace_root, projects)

    source_root_shas = _root_commits(source_repo)
    # The releasable this run targets, resolved here rather than below because
    # it is part of the heal identity: without --releasable the created
    # singleton is named after the member, which is the same answer the branch
    # below arrives at.
    target_releasable = releasable_name or name
    merge_commit = _find_merge(
        workspace_root, name, dest_path, source_root_shas, target_releasable,
    )
    healing = merge_commit is not None

    member_present = False
    member_entry = None
    for proj in projects:
        same_path = proj["path"] == dest_path
        same_name = proj["name"] == name
        if same_path and same_name:
            if not healing:
                raise AbsorbError(
                    f"package '{name}' already exists in workspace at "
                    f"'{dest_path}'"
                )
            member_present = True
            member_entry = proj
            continue
        if same_path:
            raise AbsorbError(
                f"path '{dest_path}' already exists in workspace (member "
                f"'{proj['name']}')"
            )
        if same_name:
            raise AbsorbError(
                f"package '{name}' already exists in workspace at "
                f"'{proj['path']}'"
            )

    if not healing and os.path.exists(os.path.join(workspace_root, dest_path)):
        raise AbsorbError(
            f"destination path already exists on disk: {dest_path}. The "
            f"absorbed history is merged in under that prefix, so it must be "
            f"free -- absorb into a path this repository does not use."
        )

    dirty = _dirty_paths(workspace_root)
    if dirty:
        raise AbsorbError(
            f"the workspace has uncommitted changes ({', '.join(dirty)}). The "
            f"absorption merges a rewritten history into this repository and "
            f"commits its own edits, so the tree must be clean. Commit or set "
            f"aside the changes first."
        )

    entries = _source_targets(source_repo)
    target_names = tuple(e.name for e in entries)

    declared = {r.name for r in releasables}
    creates_releasable = releasable_name is None
    if creates_releasable:
        releasable_name = name
        # A releasable of this name that a PREVIOUS run of this same absorb
        # created is this run's own work, recognized by the member entry that
        # names it. Any other one is somebody else's.
        ours = (
            healing
            and member_entry is not None
            and member_entry.get("releasable") == releasable_name
        )
        if releasable_name in declared and not ours:
            raise AbsorbError(
                f"releasable '{releasable_name}' already exists in this "
                f"workspace. Absorbing without --releasable creates a "
                f"releasable named after the member; pass --releasable "
                f"{releasable_name} to join the existing one, or --name to "
                f"give the arriving member a different name."
            )
        if releasable_name in declared:
            declared_format = _named_releasable(
                releasables, releasable_name,
            ).effective_tag_format
            if tag_format and tag_format != declared_format:
                raise AbsorbError(
                    f"releasable '{releasable_name}' was already created by "
                    f"the run this one completes, with tag_format "
                    f"'{declared_format}'; --tag-format says "
                    f"'{tag_format}'. A releasable's tags are already written "
                    f"under the declared format -- change it in workspace.toml "
                    f"if it is wrong, rather than through a re-run."
                )
            resolved_format = declared_format
        elif tag_format:
            resolved_format = tag_format
        else:
            resolved_format = _derive_tag_format(
                source_repo, entries, releasable_name, dest_path,
            )
    else:
        if releasable_name not in declared:
            raise AbsorbError(
                f"releasable '{releasable_name}' is not defined in "
                f"[[releasables]]. Available: {sorted(declared) or '(none)'}"
            )
        if tag_format:
            raise AbsorbError(
                f"--tag-format applies only to the releasable this command "
                f"creates. Releasable '{releasable_name}' already exists and "
                f"declares its own tag format "
                f"('{_named_releasable(releasables, releasable_name).effective_tag_format}'); "
                f"change it in workspace.toml if it is wrong."
            )
        resolved_format = _named_releasable(
            releasables, releasable_name,
        ).effective_tag_format
        try:
            read_releasable_version(workspace_root, releasable_name)
        except Exception as exc:
            raise AbsorbError(
                f"releasable '{releasable_name}' has no release state to "
                f"absorb into: {exc}. Its state directory "
                f"({get_releasable_dir(workspace_root, releasable_name)}) is "
                f"where the arriving changelog and release archives go. Run "
                f"`rlsbl monorepo sync` to create it -- it writes a 0.0.0 "
                f"version file and an empty unreleased.jsonl for every "
                f"declared releasable -- and, if '{releasable_name}' has "
                f"already shipped, put its real version in that version file "
                f"before absorbing."
            ) from exc

    source_version_tags = [
        parsed
        for parsed in (
            parse_version_tag(t, mode=TagMode.PRERELEASE_INCLUSIVE)
            for t in _git_tag_names(source_repo)
        )
        if parsed is not None
    ]
    version = _arrival_version(
        workspace_root, source_repo, entries, source_version_tags,
        releasable_name=releasable_name,
        healing=healing,
        creates_releasable=creates_releasable,
    )

    source_state = _read_source_state(source_repo)
    if not healing:
        # Skipped on a re-run: the state a previous run migrated is already in
        # the releasable, and it would collide with itself. What it might
        # really collide with -- a DIFFERENT file at the same version -- is
        # compared byte for byte when the migration runs.
        _check_version_overlap(
            workspace_root, releasable_name, source_repo, source_state,
            source_version_tags,
        )

    tag_plan = _plan_tags(
        workspace_root, source_repo, releasable_name, resolved_format, version,
        glob=releasable_tag_glob(resolved_format, releasable_name),
        healing=healing,
    )

    schemes = {p.scheme for p in source_version_tags}
    source_tag_format = None
    if schemes == {"standalone"}:
        source_tag_format = "v{version}"
    elif schemes == {"monorepo"}:
        source_tag_format = DEFAULT_TAG_FORMAT

    return Arrival(
        workspace_root=workspace_root,
        source_repo=source_repo,
        dest_path=dest_path,
        name=name,
        registry_name=registry_name or "",
        releasable_name=releasable_name,
        creates_releasable=creates_releasable,
        tag_format=resolved_format,
        version=version,
        source_root_sha=source_root_shas[0] if source_root_shas else "",
        source_repo_url=_origin_url(source_repo),
        source_tag_format=source_tag_format,
        projects=list(projects),
        releasables=list(releasables),
        tag_plan=tag_plan,
        state=source_state,
        target_names=target_names,
        merge_commit=merge_commit,
        member_present=member_present,
        delete_with_rm=delete_with_rm,
    )


def _named_releasable(releasables, name):
    for rel in releasables:
        if rel.name == name:
            return rel
    raise AbsorbError(f"releasable '{name}' not found")


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


def _repository_bound_publishers(arr):
    """The arriving targets whose publisher is authorized per REPOSITORY."""
    from ...targets import TARGETS

    seen = {}
    for target_name in arr.target_names:
        target = TARGETS.get(target_name)
        if target is not None and target.publisher_binds_to_repository:
            seen.setdefault(target_name, target)
    return [seen[key] for key in sorted(seen)]


def _next_steps(arr):
    """The steps rlsbl deliberately does NOT take on the operator's behalf."""
    steps = [
        f"review {arr.dest_path}/ against this workspace's conventions (the "
        f"absorbed repository was scaffolded for a standalone project)",
        f"review the regenerated CI router in {arr.workspace_root} before the "
        f"next release (monorepo sync is re-run for you, but which jobs the "
        f"new member needs is yours to confirm)",
        f"the source repository at {arr.source_repo} is untouched: archive it "
        f"yourself once you are satisfied with what arrived",
    ]
    for target in _repository_bound_publishers(arr):
        steps.append(
            f"{target.registry_display_name} publishing is authorized for a "
            f"REPOSITORY, not for the package, so it did not follow the code: "
            f"register THIS repository at {target.publisher_setup_url} before "
            f"the next release (a publish that fails for want of one is "
            f"recovered with `rlsbl release retry`, not a new version)"
        )
    return steps


def observe(arr) -> Preview:
    """The whole plan, as a keyed verdict list in apply order."""
    items = []
    plan = arr.tag_plan

    items.append(VerdictItem(
        key=ITEM_SOURCE,
        state="rewrite_history",
        summary=(
            f"'{os.path.basename(arr.source_repo)}' (version {arr.version}) "
            f"becomes member '{arr.name}' at {arr.dest_path}/."
        ),
        facts=(
            f"source: {arr.source_repo} (root commit "
            f"{arr.source_root_sha[:12] or 'unknown'})",
            f"targets: {', '.join(arr.target_names) or '(none detected)'}",
            f"working clone: {_relative(arr.workspace_root, arr.clone_path)} "
            f"(inside this repository's git directory, removed afterwards)",
        ),
        actions=(
            f"apply would clone the source there and run git-filter-repo "
            f"--to-subdirectory-filter {arr.dest_path}",
        ),
    ))

    items.append(VerdictItem(
        key=ITEM_RELEASABLE,
        state=(
            "create_releasable" if arr.creates_releasable else "join_releasable"
        ),
        summary=(
            f"a new releasable '{arr.releasable_name}' is created for the "
            f"arriving member."
            if arr.creates_releasable else
            f"the arriving member joins releasable '{arr.releasable_name}'."
        ),
        facts=tuple(
            [f"tag format: {arr.tag_format}"
             + (" (written explicitly)" if arr.creates_releasable else
                " (the releasable's own)")]
            + ([f"version file: {arr.version}"] if arr.creates_releasable
               else ["version: the releasable's own, untouched"])
        ),
    ))

    items.append(VerdictItem(
        key=ITEM_HISTORY,
        state="merge_history" if not arr.healing else "history_already_merged",
        summary=(
            "the rewritten history is fetched without tags and merged with "
            "--allow-unrelated-histories."
            if not arr.healing else
            f"commit {arr.merge_commit[:12]} already merged this source; the "
            f"re-run completes what follows it."
        ),
        facts=(
            f"merge trailer: {ABSORB_TRAILER}: {arr.name} {arr.dest_path}",
            f"source identity: {SOURCE_TRAILER}: {arr.source_root_sha}",
            f"target releasable: {RELEASABLE_TRAILER}: {arr.releasable_name}",
        ),
    ))

    items.append(VerdictItem(
        key=ITEM_TAGS,
        state="import_tags" if plan.imports else "no_tags_to_import",
        summary=(
            f"{len(plan.imports)} version tag(s) are created under "
            f"{arr.tag_format}."
            if plan.imports else
            "the source carries no version tag, so none is imported."
        ),
        facts=tuple(
            [f"{entry.old_tag} -> {entry.new_tag}" for entry in plan.imports]
            + ([f"boundary alias: {plan.alias[0]} is created beside "
                f"{plan.alias[1]} at version {plan.alias[2]}"]
               if plan.alias else [])
            + ([f"not version tags, not imported: {', '.join(plan.skipped)}"]
               if plan.skipped else [])
            + ([f"already present from an earlier run: "
                f"{', '.join(sorted(set(plan.already_present)))}"]
               if plan.already_present else [])
        ),
        actions=(
            "apply would create each tag at the rewritten commit; the fetch "
            "carries no tags, so no existing tag is ever moved or deleted.",
        ),
    ))

    state = arr.state
    items.append(VerdictItem(
        key=ITEM_STATE,
        state="migrate_state",
        summary=(
            f"the arriving release state moves into "
            f"{_relative(arr.workspace_root, arr.releasable_dir)}/."
        ),
        facts=tuple(
            [f"unreleased entries: {state.unreleased_entries}",
             f"released changelogs: {', '.join(state.versioned) or '(none)'}",
             f"release archives: {', '.join(state.archives) or '(none)'}"]
            + (["config.json is copied to the releasable as its base config"]
               if state.has_config and arr.creates_releasable else [])
        ),
        actions=(
            "apply would remap every changelog hash and every release commit "
            "through filter-repo's commit map, verify each recorded tree "
            "against the rewritten history, and name what it could not map.",
            f"apply would then delete the per-package changes/, releases/, "
            f"version and CHANGELOG.md under {arr.dest_path}/ "
            f"({'rm -rf' if arr.delete_with_rm else 'saferm'}); hooks/ and "
            f"config.json stay.",
        ),
    ))

    items.append(VerdictItem(
        key=ITEM_WORKSPACE,
        state="register_member" if not arr.member_present else "member_present",
        summary=(
            f"workspace.toml gains member '{arr.name}' at {arr.dest_path}."
            if not arr.member_present else
            f"workspace.toml already declares '{arr.name}' at "
            f"{arr.dest_path}; the entry is left as it is."
        ),
        facts=tuple(
            [f"releasable = \"{arr.releasable_name}\""]
            + ([f"registry_name = \"{arr.registry_name}\""]
               if arr.registry_name else [])
        ),
        actions=(
            "apply would scaffold the member, re-run monorepo sync, regenerate "
            f"{SNAPSHOT_FILE}, and commit the absorption's own files.",
        ),
    ))

    events = ["conversion (direction=absorb)"]
    if plan.imports:
        events.append("tag-map")
    if state.archives:
        events.append("release-commit-remap")
    if plan.alias:
        events.append("boundary-alias")
    items.append(VerdictItem(
        key=ITEM_TRANSITION_RECORD,
        state="record_transition_record",
        summary=(
            f"a transition record in the releasable explains the conversion: "
            f"{', '.join(events)}."
        ),
        facts=(
            f"record: {_relative(arr.workspace_root, get_transition_record_path(arr.workspace_root, releasable_dir=arr.releasable_dir))}",
        ),
    ))

    items.append(VerdictItem(
        key=ITEM_NEXT_STEPS,
        state="operator_actions",
        summary="rlsbl never administers an external system; these are yours.",
        facts=tuple(_next_steps(arr)),
    ))

    return Preview(tuple(items))


# ---------------------------------------------------------------------------
# Apply: one step per preview item, in preview order
# ---------------------------------------------------------------------------


def _apply_source(arr, item, run):
    """Take the lock, clone the source, and rewrite the clone under the prefix."""
    run.stack.enter_context(
        rlsbl_lock(WORKSPACE_DIR, project_root=arr.workspace_root, wait=False)
    )
    # Re-check under the lock: between observation and here another process may
    # have written, and the merge would carry whatever the tree is now.
    dirty = _dirty_paths(arr.workspace_root)
    if dirty:
        raise AbsorbError(
            f"the workspace became dirty after the plan was made "
            f"({', '.join(dirty)}); nothing was written. Commit or set aside "
            f"the changes and re-run."
        )
    if not is_clean_tree(cwd=arr.source_repo):
        raise AbsorbError(
            f"the source repository became dirty after the plan was made; "
            f"nothing was written. Commit the changes and re-run."
        )

    clone_path = arr.clone_path
    # Tool-owned scratch inside the workspace's git directory: a leftover from
    # an interrupted run is replaced, never merged with.
    if os.path.exists(clone_path):
        effects.rmtree(clone_path)
    effects.makedirs(os.path.dirname(clone_path), exist_ok=True)
    run.stack.callback(lambda: effects.rmtree(clone_path, ignore_errors=True))

    print(f"Cloning {arr.source_repo} -> {_relative(arr.workspace_root, clone_path)} ...")
    _run_git(arr.workspace_root, "clone", "--no-local", arr.source_repo, clone_path)
    _ensure_git_identity(clone_path, arr.source_repo)
    _run_filter_repo(
        clone_path, "--to-subdirectory-filter", arr.dest_path, "--force",
    )

    commit_map = os.path.join(clone_path, ".git", "filter-repo", "commit-map")
    run.sha_map, run.pruned_shas = load_filter_repo_commit_map(commit_map)
    print(
        f"  filter-repo mapped {len(run.sha_map)} commit(s); "
        f"{len(run.pruned_shas)} pruned."
    )


def _apply_releasable(arr, item, run):
    """Nothing to do: the releasable is written with the workspace entry."""
    return


def _apply_history(arr, item, run):
    """Fetch the rewritten history WITHOUT tags and merge it in."""
    if arr.healing:
        print(f"  history: already merged by {arr.merge_commit[:12]}; skipped.")
        return

    # --no-tags is structural, not hygiene: an ordinary fetch auto-follows the
    # source's tags, refuses to move a destination tag of the same name, and
    # leaves the caller to clean up -- which is how the old implementation came
    # to delete the destination's own tags. Bringing in no tag at all means
    # every tag in this repository afterwards was created deliberately, at a
    # commit rlsbl mapped.
    _run_git(arr.workspace_root, "fetch", "--no-tags", arr.clone_path)
    message = (
        f"monorepo: absorb {arr.name} history\n"
        f"\n"
        f"Autogenerated: true\n"
        f"{ABSORB_TRAILER}: {arr.name} {arr.dest_path}\n"
        f"{SOURCE_TRAILER}: {arr.source_root_sha}\n"
        f"{RELEASABLE_TRAILER}: {arr.releasable_name}\n"
    )
    _run_git(
        arr.workspace_root, "merge", "--allow-unrelated-histories",
        "-m", message, "FETCH_HEAD",
    )
    arr.merge_commit = _run_git(arr.workspace_root, "rev-parse", "HEAD")
    print(f"  history: merged as {arr.merge_commit[:12]}.")


def _map_sha(old, sha_map):
    """Map one (possibly abbreviated) SHA through filter-repo's commit map."""
    if old in sha_map:
        return sha_map[old]
    matches = [new for key, new in sha_map.items() if key.startswith(old)]
    if len(matches) == 1:
        return matches[0]
    return None


def _create_tag(arr, run, tag, sha, *, what):
    """Create ``tag`` at ``sha``, healing an identical one and refusing others.

    Never a delete and never a move: a tag that already stands where this
    absorb would put it is the previous run's own work, and one that stands
    anywhere else belongs to somebody and is a hard error.
    """
    existing = _git_tag_names(arr.workspace_root, tag)
    if existing:
        at = _run_git(arr.workspace_root, "rev-list", "-n", "1", tag)
        if at == sha:
            print(f"  tag: '{tag}' already at {sha[:12]}; kept.")
            return False
        raise AbsorbError(
            f"tag '{tag}' already exists at {at[:12]}, but this absorb's "
            f"{what} maps to {sha[:12]}. A tag rlsbl did not just create is "
            f"never moved or deleted -- resolve the conflicting tag by hand "
            f"and re-run."
        )
    _run_git(arr.workspace_root, "tag", tag, sha)
    return True


def _apply_tags(arr, item, run):
    """Create the destination-scheme tags, plus the one boundary alias."""
    mappings = []
    for entry in arr.tag_plan.imports:
        new_sha = _map_sha(entry.old_sha, run.sha_map)
        if new_sha is None or not _commit_exists(arr.workspace_root, new_sha):
            print(
                f"  tag: '{entry.old_tag}' names a commit the rewrite did not "
                f"carry into this repository; not imported.",
                file=sys.stderr,
            )
            continue
        _create_tag(arr, run, entry.new_tag, new_sha, what=entry.old_tag)
        mappings.append(TagMapping(
            old_tag=entry.old_tag, new_tag=entry.new_tag,
            new_commit=new_sha, old_commit=entry.old_sha,
        ))
        if arr.tag_plan.alias and entry.version == arr.tag_plan.alias[2]:
            _create_tag(
                arr, run, arr.tag_plan.alias[0], new_sha,
                what=f"boundary alias for {entry.new_tag}",
            )
            run.alias_commit = new_sha
    run.tag_mappings = mappings
    print(f"  tags: {len(mappings)} imported under {arr.tag_format}.")


def _apply_state(arr, item, run):
    """Remap the arriving state onto the rewritten history, then move it."""
    changes_dir = os.path.join(arr.member_rlsbl_dir, "changes")
    releases_dir = os.path.join(arr.member_rlsbl_dir, "releases")

    _remap_changelog(arr, run, changes_dir)
    _remap_release_commits(arr, run, releases_dir)
    _migrate_changes(arr, run, changes_dir)
    _migrate_releases(arr, run, releases_dir)
    _migrate_identity(arr, run)
    _remove_residue(arr, run, changes_dir, releases_dir)


def _remap_changelog(arr, run, changes_dir):
    """Map the arriving changelog hashes onto the rewritten commits."""
    if not os.path.isdir(changes_dir):
        return
    report = remap_jsonl_hashes(changes_dir, run.sha_map)
    for result in report.results:
        print(
            f"  changelog: remapped {result.hashes_remapped} hash(es) in "
            f"{os.path.basename(result.path)}"
        )
    for filepath, hashes in sorted(report.unmapped.items()):
        print(
            f"  changelog: {len(hashes)} hash(es) in "
            f"{os.path.basename(filepath)} could not be mapped "
            f"({', '.join(h[:12] for h in hashes)})",
            file=sys.stderr,
        )
    for filepath, hashes in sorted(report.ambiguous.items()):
        print(
            f"  changelog: {len(hashes)} abbreviated hash(es) in "
            f"{os.path.basename(filepath)} are ambiguous after the rewrite "
            f"({', '.join(hashes)})",
            file=sys.stderr,
        )
    dropped = _prune_dangling_entries(changes_dir, arr.workspace_root)
    if dropped:
        print(
            f"  changelog: dropped {sum(dropped.values())} entry/entries whose "
            f"commits the rewrite did not carry over.",
            file=sys.stderr,
        )


def _remap_release_commits(arr, run, releases_dir):
    """Remap every arriving release commit onto the rewritten history.

    An archive records the commit a version shipped from and the git tree of
    every path it shipped. The commit is mapped through filter-repo's map; the
    trees are recomputed at the new commit and the path the member now has, and
    CHECKED against what was recorded. A tree hash is content-addressed, so a
    faithful rewrite reproduces it exactly -- a disagreement means the content
    of a historical release changed under the rewrite, and it is a hard error
    while the destination can still be reset.
    """
    if not os.path.isdir(releases_dir):
        return
    for name in sorted(os.listdir(releases_dir)):
        if not (name.startswith("v") and name.endswith(".toml")):
            continue
        path = os.path.join(releases_dir, name)
        config = read_release_file(path)
        if not config.candidate_sha or not config.tree_hashes:
            continue
        new_sha = _map_sha(config.candidate_sha, run.sha_map)
        if new_sha is None or not _commit_exists(arr.workspace_root, new_sha):
            run.unremapped_release_commits.append((name, config.candidate_sha))
            print(
                f"  release commit: {name} still names {config.candidate_sha[:12]}, a "
                f"commit the rewrite did not carry over; left as recorded.",
                file=sys.stderr,
            )
            continue
        trees = {}
        failed = None
        for old_path, recorded in config.tree_hashes.items():
            new_path = _dest_release_commit_path(arr, old_path)
            try:
                recomputed = _tree_hash(
                    arr.workspace_root, new_path, rev=new_sha,
                )
            except subprocess.CalledProcessError:
                failed = old_path
                break
            if recomputed != recorded:
                raise AbsorbError(
                    f"release commit {name} does not survive the rewrite: it "
                    f"records tree {recorded} for '{old_path}', but "
                    f"'{new_path}' at the rewritten commit {new_sha} is "
                    f"{recomputed}. A tree hash is content-addressed, so a "
                    f"faithful rewrite reproduces it exactly -- the content of "
                    f"that released version is not the content that arrived."
                )
            trees[new_path] = recomputed
        if failed is not None:
            run.unremapped_release_commits.append((name, config.candidate_sha))
            print(
                f"  release commit: {name} names path '{failed}', which does not "
                f"resolve at the rewritten commit; left as recorded.",
                file=sys.stderr,
            )
            continue
        effects.chmod(path, 0o644)
        write_release_commit(path, candidate_sha=new_sha, tree_hashes=trees)
        run.release_commit_mappings.append(
            ReleaseCommitMapping(old_sha=config.candidate_sha, new_sha=new_sha)
        )
        print(
            f"  release commit: {name} {config.candidate_sha[:12]} -> {new_sha[:12]} "
            f"({', '.join(sorted(trees))})"
        )


def _dest_release_commit_path(arr, old_path):
    """A recorded path's spelling in the destination.

    A standalone project records its release at ``"."``; that same content now
    sits under the member's path, so the key becomes the member path -- exactly
    the spelling a workspace release writes, and exactly what an extract turns
    back into ``"."``.
    """
    if old_path in ("", "."):
        return arr.dest_path
    return os.path.join(arr.dest_path, old_path)


def _already_migrated(path):
    """How to recognize an entry the file at ``path`` already holds.

    Two indexes, because ``id`` is optional on read: an entry that carries one
    is identified by it, and one that does not falls back to its CONTENT --
    the only identity a line without an id has. Identifying id-less entries by
    id alone made every historical entry unrecognizable, so a re-run appended a
    second copy of what it had already migrated.
    """
    if not os.path.isfile(path):
        return set(), set()
    present = parse_jsonl(path)
    return (
        {entry.id for entry in present if entry.id},
        {entry_content_key(entry) for entry in present if not entry.id},
    )


def _entries_to_migrate(arriving, known_ids, known_content):
    """The arriving entries the target does not already hold.

    Neither index is extended while iterating, deliberately: the arriving file
    is copied as it stands, so two identical lines in it stay two lines, and a
    re-run recognizes both of them at once.
    """
    new_entries = []
    for entry in arriving:
        if entry.id:
            if entry.id in known_ids:
                continue
        elif entry_content_key(entry) in known_content:
            continue
        new_entries.append(entry)
    return new_entries


def _migrate_changes(arr, run, changes_dir):
    """Move the arriving changelog into the releasable's changes directory."""
    if not os.path.isdir(changes_dir):
        return
    rel_changes = get_releasable_changes_dir(
        arr.workspace_root, arr.releasable_name,
    )
    effects.makedirs(rel_changes, exist_ok=True)

    arriving = os.path.join(changes_dir, "unreleased.jsonl")
    if os.path.isfile(arriving):
        target = os.path.join(rel_changes, "unreleased.jsonl")
        arriving_entries = parse_jsonl(arriving)
        new_entries = _entries_to_migrate(
            arriving_entries, *_already_migrated(target),
        )
        run.entries_already_present += (
            len(arriving_entries) - len(new_entries)
        )
        if new_entries:
            body = "".join(serialize_entry(e) + "\n" for e in new_entries)
            effects.append_text(target, body)
            run.entries_migrated += len(new_entries)
        run.record_path(arr.workspace_root, target)

    for name in sorted(os.listdir(changes_dir)):
        if name == "unreleased.jsonl":
            continue
        if not (name.endswith(".jsonl") or name.endswith(".md")):
            continue
        source = os.path.join(changes_dir, name)
        target = os.path.join(rel_changes, name)
        if os.path.isfile(target):
            with open(source, "r", encoding="utf-8") as f:
                arriving_body = f.read()
            with open(target, "r", encoding="utf-8") as f:
                present_body = f.read()
            if arriving_body != present_body:
                raise AbsorbError(
                    f"released changelog collision: releasable "
                    f"'{arr.releasable_name}' already has "
                    f"changes/{name}, and the arriving one differs. A released "
                    f"version's changelog is immutable, so neither copy can be "
                    f"chosen for the other -- resolve the overlap before "
                    f"re-running."
                )
            continue
        effects.copy_file(source, target)
        if name.endswith(".jsonl"):
            # A released changelog is locked, and it arrives locked: the copy
            # inherits whatever the source's mode was, so the lock is stated
            # here rather than assumed.
            effects.chmod(target, 0o444)
        run.record_path(arr.workspace_root, target)


def _migrate_releases(arr, run, releases_dir):
    """Move the arriving release archives into the releasable's releases dir."""
    if not os.path.isdir(releases_dir):
        return
    rel_releases = os.path.join(arr.releasable_dir, "releases")
    effects.makedirs(rel_releases, exist_ok=True)
    for name in sorted(os.listdir(releases_dir)):
        if not (name.startswith("v") and name.endswith(".toml")):
            continue
        source = os.path.join(releases_dir, name)
        target = os.path.join(rel_releases, name)
        if os.path.isfile(target):
            with open(source, "r", encoding="utf-8") as f:
                arriving_body = f.read()
            with open(target, "r", encoding="utf-8") as f:
                present_body = f.read()
            if arriving_body != present_body:
                raise AbsorbError(
                    f"release archive collision: releasable "
                    f"'{arr.releasable_name}' already has releases/{name}, and "
                    f"the arriving one differs. An archived release file is the "
                    f"record of what shipped -- resolve the overlap before "
                    f"re-running."
                )
            continue
        effects.copy_file(source, target)
        effects.chmod(target, 0o444)
        run.record_path(arr.workspace_root, target)


def _migrate_identity(arr, run):
    """Give a newly created releasable the version and config that arrived."""
    if not arr.creates_releasable:
        return
    effects.makedirs(arr.releasable_dir, exist_ok=True)
    write_releasable_version(
        arr.workspace_root, arr.releasable_name, arr.version,
    )
    run.record_path(arr.workspace_root, os.path.join(arr.releasable_dir, "version"))

    source_config = os.path.join(arr.member_rlsbl_dir, "config.json")
    target_config = os.path.join(arr.releasable_dir, "config.json")
    if os.path.isfile(source_config):
        merged = read_json_config(target_config)
        merged.update(read_json_config(source_config))
        _write_json(target_config, merged)
        run.record_path(arr.workspace_root, target_config)


def _remove_residue(arr, run, changes_dir, releases_dir):
    """Delete the per-package release state that has moved to the releasable.

    A releasable's changelog, release archives and version have one home, and
    it is the releasable's state directory. What arrived under the member's own
    ``.rlsbl/`` is the same state under the layout the source used, so once it
    has moved it is residue -- the same set ``rlsbl monorepo cleanup`` removes,
    scoped to this one member.

    Two things it deliberately leaves: per-package ``hooks/`` (a live feature),
    and the member's own ``.rlsbl/config.json``. The config was COPIED to the
    releasable as its base, not moved: a member config is a legal override, it
    is what the scaffold that follows merges into, and deduplicating it against
    the releasable's is ``rlsbl monorepo cleanup``'s decision to make, not this
    conversion's.
    """
    residue = [
        changes_dir,
        releases_dir,
        # rlsbl's scaffolding-version file for a standalone project; under a
        # releasable, the version that means anything is the releasable's.
        os.path.join(arr.member_rlsbl_dir, "version"),
        # The generated changelog is regenerated per releasable now.
        os.path.join(arr.dest_full, "CHANGELOG.md"),
    ]
    for path in residue:
        if not os.path.exists(path):
            continue
        _delete_path(
            path,
            description=(
                f"Per-package release state of '{arr.name}', moved to "
                f"releasable '{arr.releasable_name}' (rlsbl monorepo absorb)"
            ),
            delete_with_rm=arr.delete_with_rm,
        )
        run.record_path(arr.workspace_root, path)


def _apply_workspace(arr, item, run):
    """Register the member (and its releasable), then commit and sync."""
    workspace_file = os.path.join(
        arr.workspace_root, WORKSPACE_DIR, WORKSPACE_FILE,
    )
    if not arr.member_present:
        entry = {
            "path": arr.dest_path,
            "name": arr.name,
            "releasable": arr.releasable_name,
        }
        if arr.registry_name:
            entry["registry_name"] = arr.registry_name
        projects = list(arr.projects) + [WorkspaceProject(entry)]
        releasables = list(arr.releasables)
        if arr.creates_releasable and not any(
            r.name == arr.releasable_name for r in releasables
        ):
            releasables.append(
                Releasable(name=arr.releasable_name, tag_format=arr.tag_format)
            )
        save_workspace(arr.workspace_root, projects, releasables=releasables)
        run.record_path(arr.workspace_root, workspace_file)

    projects = load_workspace(arr.workspace_root)
    graph = WorkspaceGraph(arr.workspace_root, projects)
    write_snapshot(
        arr.workspace_root,
        generate_snapshot(arr.workspace_root, projects, graph),
    )
    run.record_path(
        arr.workspace_root,
        os.path.join(arr.workspace_root, WORKSPACE_DIR, SNAPSHOT_FILE),
    )

    commit_files(
        f"monorepo: absorb {arr.name}",
        sorted(run.written),
        cwd=arr.workspace_root,
    )
    run.state_commit = _run_git(arr.workspace_root, "rev-parse", "HEAD")
    _require_clean(
        arr,
        "the absorption's own commit named every file it wrote, so anything "
        "left here was written by something else",
    )

    _scaffold_member(arr)
    _sweep_member_rlsbl(arr)
    _commit_step_output(arr, f"monorepo: scaffold {arr.name} as a member")
    _sync_workspace(arr)
    _commit_step_output(arr, "monorepo: sync CI workflows")
    _assert_workspace_loads(arr)


def _require_clean(arr, why):
    """Hard-error when the workspace is dirty at a point it must not be."""
    leftover = _dirty_paths(arr.workspace_root)
    if leftover:
        raise AbsorbError(
            f"the workspace has uncommitted changes ({', '.join(leftover)}) "
            f"where it should have none: {why}. Nothing further was written."
        )


def _commit_step_output(arr, message):
    """Commit what the step that just ran wrote, and nothing else.

    The file list is the working tree's own report, taken immediately after a
    step that started from a tree :func:`_require_clean` had just verified --
    so it names exactly that step's output, not a sweep of whatever happened to
    be lying around. The two steps that need this are the ones whose output
    rlsbl does not enumerate: ``rlsbl scaffold`` (which usually commits its own
    files, leaving nothing here) and ``monorepo sync``, whose own auto-commit
    resolves its git repository from the process working directory rather than
    from the workspace it was pointed at.
    """
    paths = _dirty_paths(arr.workspace_root)
    if not paths:
        return
    commit_files(message, paths, cwd=arr.workspace_root)
    _require_clean(arr, f"{message!r} named every file that step wrote")


def _scaffold_member(arr):
    """Scaffold the arriving member, surfacing the command's own failure.

    The absorbed repository was scaffolded for a standalone project; its CI,
    hooks and workflows are regenerated for a member of this workspace. A
    non-zero exit is a hard error rather than a warning: a member that is
    half-scaffolded is a repository nobody can release, and finding that out
    later costs more than stopping here.
    """
    # The releasable model keeps merge bases at the releasable, so a member's
    # own `.rlsbl/bases/` is residue (see _sweep_member_rlsbl) -- and a scaffold
    # run against a member that has `managed-files.json` but no `bases/` refuses
    # with a one-line heal: create the directory and re-run, and it reconstructs
    # each base from the last scaffold commit. Doing that heal here is what
    # makes a re-run of this conversion reach the scaffold at all, since the
    # previous run's sweep is exactly what removed the directory.
    bases = os.path.join(arr.member_rlsbl_dir, "bases")
    managed = os.path.join(arr.member_rlsbl_dir, "managed-files.json")
    if os.path.isfile(managed) and not os.path.isdir(bases):
        effects.makedirs(bases, exist_ok=True)

    # --no-auto-tag: adding a GitHub topic is an act on an external system, and
    # this conversion administers none. The scaffold's own auto-commit is left
    # on, so the files it writes are committed by the step that wrote them.
    cmd = [sys.executable, "-P", "-m", "rlsbl", "scaffold", "--no-auto-tag"]
    result = effects.run(cmd, cwd=arr.dest_full, check=False)
    if result.returncode != 0:
        raise AbsorbError(
            f"`rlsbl scaffold` failed in {arr.dest_path} (exit "
            f"{result.returncode}). The history, the tags and the release "
            f"state are already in this repository; fix the failure and re-run "
            f"the absorb -- it detects what is done and completes the rest."
        )


def _sweep_member_rlsbl(arr):
    """Remove what the scaffold wrote that a releasable member may not keep.

    ``rlsbl scaffold`` writes a standalone project's ``.rlsbl/``: a scaffolding
    ``version`` marker and a ``bases/`` directory of merge bases. Under a
    releasable those belong to the releasable, and the ``releasable-residue``
    check errors on a member that keeps them -- so an absorb that ran the
    scaffold and stopped would hand back a workspace failing its own checks.

    The rule is not restated here: :func:`verify_minimal_rlsbl` is the one
    place that says what a member's ``.rlsbl/`` may hold, and this removes
    exactly what it names, the same set ``rlsbl monorepo cleanup`` removes.
    """
    from ...releasable_cleanup import verify_minimal_rlsbl

    removed = []
    for entry in verify_minimal_rlsbl(arr.dest_full):
        path = os.path.join(arr.member_rlsbl_dir, entry)
        _delete_path(
            path,
            description=(
                f"Per-package release state the scaffold wrote for member "
                f"'{arr.name}', which belongs to releasable "
                f"'{arr.releasable_name}' (rlsbl monorepo absorb)"
            ),
            delete_with_rm=arr.delete_with_rm,
        )
        removed.append(entry)
    if removed:
        print(
            f"  member: removed scaffold residue {', '.join(removed)} from "
            f"{arr.dest_path}/.rlsbl/ (it belongs to the releasable)."
        )


def _sync_workspace(arr):
    """Regenerate the CI router for the member list this absorb changed."""
    from .sync import _cmd_sync

    # auto-commit off: sync's own commit resolves its repository from the
    # process working directory, which is not necessarily the workspace it was
    # handed. Its output is committed by the caller, against the workspace.
    _cmd_sync({"auto-commit": False}, project_root=arr.workspace_root)


def _assert_workspace_loads(arr):
    """The workspace must read back through rlsbl's own loader."""
    try:
        projects = load_workspace(arr.workspace_root)
        releasables = load_releasables(arr.workspace_root, projects)
    except Exception as exc:
        raise AbsorbError(
            f"the workspace at {arr.workspace_root} no longer loads through "
            f"rlsbl's own loader after the absorption: {exc}"
        ) from exc
    if not any(p.name == arr.name for p in projects):
        raise AbsorbError(
            f"the workspace does not declare member '{arr.name}' after the "
            f"absorption"
        )
    if not any(r.name == arr.releasable_name for r in releasables):
        raise AbsorbError(
            f"the workspace does not declare releasable "
            f"'{arr.releasable_name}' after the absorption"
        )


def _apply_transition_record(arr, item, run):
    """Write the releasable's transition record: conversion first, then the rest."""
    path = get_transition_record_path(
        arr.workspace_root, releasable_dir=arr.releasable_dir,
    )
    conversion = ConversionEvent(
        direction="absorb",
        source=TransitionRecordEndpoint(
            repo=arr.source_repo_url,
            project=arr.name,
            tag_format=arr.source_tag_format,
        ),
        destination=TransitionRecordEndpoint(
            repo=".",
            path=arr.dest_path,
            project=arr.name,
            releasable=arr.releasable_name,
            tag_format=arr.tag_format,
        ),
        commit=run.state_commit or _run_git(
            arr.workspace_root, "rev-parse", "HEAD",
        ),
    )
    [stamped] = append_events(path, [conversion])

    followers = []
    if run.tag_mappings:
        followers.append(
            TagMapEvent(mappings=run.tag_mappings, related_to=stamped.id)
        )
    if run.release_commit_mappings:
        followers.append(ReleaseCommitRemapEvent(
            rewrite="git-filter-repo --to-subdirectory-filter (release commits)",
            mappings=run.release_commit_mappings,
            related_to=stamped.id,
        ))
    if arr.tag_plan.alias and run.alias_commit:
        source_tag, destination_tag, _version = arr.tag_plan.alias
        # The schema's two fields are keyed by FORMAT, not by which of the two
        # names this command happened to write: alias_tag is the name in the
        # post-conversion format and aliased_tag the pre-conversion one, which
        # reads the same way in an extract's record and in this one. (An absorb
        # creates both, since the source's own tags are never fetched.)
        followers.append(BoundaryAliasEvent(
            aliases=[BoundaryAlias(
                alias_tag=destination_tag, aliased_tag=source_tag,
                commit=run.alias_commit,
            )],
            related_to=stamped.id,
        ))
    if followers:
        append_events(path, followers)

    commit_files(
        f"monorepo: record the {arr.name} absorb transition record",
        [_relative(arr.workspace_root, path)],
        cwd=arr.workspace_root,
    )
    print(f"  transition record: {len(followers) + 1} event(s) recorded.")


def _apply_next_steps(arr, item, run):
    """Print what the operator still has to do. rlsbl administers nothing."""
    leftover = _dirty_paths(arr.workspace_root)
    if leftover:
        raise AbsorbError(
            f"the workspace still has uncommitted changes after the "
            f"absorption: {', '.join(leftover)}. The history, tags and release "
            f"state are in place; commit or revert the leftovers by hand."
        )
    print(
        f"\nAbsorbed '{arr.name}' from {arr.source_repo} into {arr.dest_path}."
    )
    print(
        f"  Changelog: {run.entries_migrated} unreleased entry/entries "
        f"migrated"
        + (f", {run.entries_already_present} already present"
           if run.entries_already_present else "")
    )
    print(
        f"  Tags: "
        f"{', '.join(m.new_tag for m in run.tag_mappings) or 'none imported'}"
    )
    if run.unremapped_release_commits:
        print(
            "\nRelease release commits left as recorded (their commits did not survive "
            "the rewrite):",
            file=sys.stderr,
        )
        for name, sha in run.unremapped_release_commits:
            print(f"  - {name}: {sha[:12]}", file=sys.stderr)
    print("\nNext steps (rlsbl never administers an external system):")
    for step in _next_steps(arr):
        print(f"  - {step}")


_APPLY_STEPS = {
    ITEM_SOURCE: _apply_source,
    ITEM_RELEASABLE: _apply_releasable,
    ITEM_HISTORY: _apply_history,
    ITEM_TAGS: _apply_tags,
    ITEM_STATE: _apply_state,
    ITEM_WORKSPACE: _apply_workspace,
    ITEM_TRANSITION_RECORD: _apply_transition_record,
    ITEM_NEXT_STEPS: _apply_next_steps,
}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def cmd_absorb(workspace_root, source_repo, dest_path, *, name=None,
               registry_name="", releasable_name=None, tag_format="",
               delete_with_rm=False, dry_run=False):
    """Absorb an external repository into the workspace. Returns the Preview.

    Args:
        workspace_root: path to the monorepo root.
        source_repo: the external repository to absorb.
        dest_path: the member path its history is rewritten under.
        name: the member's workspace name (the destination's basename when
            omitted).
        registry_name: the package registry identity recorded in workspace.toml.
        releasable_name: an existing releasable to join. When omitted, a
            singleton releasable named after the member is created.
        tag_format: the created releasable's tag format, stated explicitly.
            Illegal together with ``releasable_name``, which brings its own.
        delete_with_rm: delete the per-package residue with ``rm -rf`` instead
            of saferm. Without it, an absent saferm is a refusal.
        dry_run: render the plan and stop.
    """
    arr = None

    def _observe():
        nonlocal arr
        arr = resolve_arrival(
            workspace_root, source_repo, dest_path,
            name=name,
            registry_name=registry_name,
            releasable_name=releasable_name,
            tag_format=tag_format,
            delete_with_rm=delete_with_rm,
        )
        return observe(arr)

    run = Applied()

    def _apply(item):
        _APPLY_STEPS[item.key](arr, item, run)

    with ExitStack() as stack:
        run.stack = stack
        return reconcile(
            Reconciler(observe=_observe, apply_item=_apply, show_keys=True),
            dry_run=dry_run,
        )


def _cmd_absorb(flags, project_root):
    """``rlsbl monorepo absorb <source_repo> <dest_path>``."""
    root = find_workspace_root(str(project_root))
    if root is None:
        print(
            "Error: No workspace found. Run 'rlsbl monorepo init' first.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        cmd_absorb(
            root,
            flags["source-repo"],
            flags["dest-path"],
            name=flags.get("name") or None,
            registry_name=flags.get("registry-name") or "",
            releasable_name=flags.get("releasable") or None,
            tag_format=flags.get("tag-format") or "",
            delete_with_rm=bool(flags.get("delete-with-rm", False)),
            dry_run=bool(flags.get("dry-run", False)),
        )
    except Exception as exc:  # noqa: BLE001 -- every failure is a CLI error
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
