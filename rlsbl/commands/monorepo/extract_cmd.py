"""Extract a releasable out of a workspace into its own repository.

``rlsbl monorepo extract <releasable> <target-path>`` is the ONE conversion
command in the outbound direction, and the unit it operates on is the
RELEASABLE -- the portable unit. A releasable owns a version, a changelog, a
release-file archive and a tag scheme; a single member package owns none of
those on its own, so "extract this package" was never a complete question. The
two commands that used to ask it (``monorepo extract <package>`` and ``monorepo
extract-releasable``) are gone.

The shape: observe, then either render or apply
-----------------------------------------------

The command is a reconciler built on :mod:`rlsbl.preview_apply`. Observation
runs under :func:`~rlsbl.preview_apply.no_writes` and answers every question the
apply will act on -- which tags translate, which trees must match, what the
destination will look like, what the source loses. ``--dry-run`` renders that
plan and stops. Otherwise the plan is applied, item by item, in the order it was
rendered: the preview IS the pipeline.

Refusals happen during observation, so they cost nothing and fire identically
under a preview:

* the releasable is **mirrored** (a member declares ``subtree_remote``). A
  mirror is a tool-owned derived artifact of THIS repository; converting the
  releasable out from under it would leave the mirror pointing at a subtree that
  no longer exists. Promoting a mirror to the real repository is its own
  operation; until it exists, remove the mirror binding first.
* the releasable contains the **root member**. The root member owns every path
  no other member claims, and a workspace has exactly one -- extracting it would
  leave the source with no root. Restructure first.
* a **remaining member depends on an extracted member**. The edge would dangle
  the moment the members leave, so the conversion refuses and names the exact
  ``rlsbl rewrite`` invocation that severs it. Extract never rewrites a manifest
  itself: the rewrite commands own that, and composing them is the design.
* the usual preconditions: the target path exists, git-filter-repo is missing,
  the source tree is dirty, a release is in flight, a translated tag would
  collide, or ``saferm`` is absent and ``--delete-with-rm`` was not passed.

What an apply actually moves
----------------------------

* **History**, via ``git-filter-repo`` on a fresh clone: the union of the member
  paths, hoisted to the repository root when the releasable has a single member.
* **Tree-object identity is then VERIFIED per member**: the source's
  ``HEAD:<member>`` tree must equal the corresponding tree in the filtered
  result. A mismatch is a hard error naming both hashes, and nothing further is
  written. This is the one check that says the code that arrived is the code
  that left.
* **The whole release state**: the releasable's state directory -- version,
  ``changes/`` (locked JSONL and generated markdown), ``releases/`` (the
  archives, anchors included), ``config.json``, ``lint/``, ``hooks/`` and its own
  lineage record -- moves to wherever the destination keeps it: ``.rlsbl/`` for a
  standalone successor, ``.rlsbl-monorepo/releasables/<name>/`` for a workspace.
* **The anchors and the changelog hashes are remapped** through filter-repo's
  commit map, and the tree hashes recomputed at the new commits and paths. What
  could not be mapped is NAMED in the output rather than silently left stale.
* **Tags** translate to the destination's scheme, with one boundary alias at the
  current version so the pre-conversion name still resolves in the new
  repository. Another live member's tags are pruned; a tag matching no current
  member is KEPT (it is most likely this releasable's own history under an older
  prefix, and release history is never destroyed on a guess).
* **A lineage record** in the destination explains all of it, and the source
  records the departure of the releasable's tag globs.

What it does NOT do: push anything (the tags it creates are local -- the
boundary alias reaches a remote through the release flow, which owns that
namespace), create a remote, scaffold the new repository, or touch any external
system. Those are printed as next steps.
"""

import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass, field

from ...changelog.files import load_filter_repo_commit_map
from ...config import read_json_config
from ...errors import ConfigError, WorkspaceError
from ...lineage import (
    AnchorMapping,
    AnchorRemapEvent,
    BoundaryAlias,
    BoundaryAliasEvent,
    ConversionEvent,
    DepartedGlobsEvent,
    LineageEndpoint,
    TagMapEvent,
    TagMapping,
    append_events,
    get_lineage_path,
)
from ...lock import rlsbl_lock
from ...ownership import find_root_member
from ...preview_apply import Preview, Reconciler, VerdictItem, reconcile
from ...release_file import read_release_file, write_release_anchor
from ...saferm import saferm_delete
from ...snapshot import SNAPSHOT_FILE, generate_snapshot, write_snapshot
from ...tag_glob import (
    TagMode,
    parse_version_tag,
    releasable_tag_glob,
    resolve_monorepo_tag_glob,
)
from ...utils import commit_files, working_tree_paths
from ...workspace import (
    STANDALONE_TAG_FORMAT,
    WORKSPACE_DIR,
    WorkspaceProject,
    find_workspace_root,
    get_releasable_dir,
    load_releasables,
    load_standalone_releasable,
    load_workspace,
    members_of,
    read_releasable_version,
    resolve_releasable_for_project,
    save_workspace,
)
from ...workspace_graph import WorkspaceGraph
from ... import effects
from .extract import (
    ExtractError,
    _ensure_git_identity,
    _prune_dangling_entries,
    _run_filter_repo,
    _run_git,
    require_filter_repo,
)


# ---------------------------------------------------------------------------
# Preview item keys -- also the apply pipeline's order
# ---------------------------------------------------------------------------

ITEM_RELEASABLE = "releasable"
ITEM_DEPENDENCIES = "dependencies"
ITEM_TREES = "trees"
ITEM_STATE = "state"
ITEM_TAGS = "tags"
ITEM_DESTINATION = "destination"
ITEM_LINEAGE = "lineage"
ITEM_SOURCE = "source"
ITEM_NEXT_STEPS = "next-steps"

#: The state directory entries a standalone destination does NOT receive.
#:
#: ``version`` is the releasable's version file, which only a workspace has: a
#: standalone project's version is the one in its own manifest, and
#: ``.rlsbl/version`` means something else entirely (the scaffolding version of
#: the rlsbl that generated the tree). Writing it there would be a lie in a file
#: another part of rlsbl reads for a different purpose.
STANDALONE_SKIPPED_STATE = ("version",)


# ---------------------------------------------------------------------------
# Observation record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TagPlan:
    """What the conversion will do to the destination clone's tags."""

    #: (old_tag, new_tag) for every own-scheme tag that changes name.
    translations: tuple = ()
    #: Own-scheme tags whose old name is DELETED after translating.
    deletions: tuple = ()
    #: Foreign live-member tags to prune.
    pruned: tuple = ()
    #: (new_tag, old_tag) kept side by side at the current version.
    alias: tuple | None = None

    @property
    def changes_names(self) -> bool:
        return bool(self.translations)


@dataclass
class Departure:
    """Everything observation resolved about one extraction."""

    workspace_root: str
    target_path: str
    releasable: object
    members: list
    projects: list
    releasables: list
    delete_with_rm: bool
    source_state_dir: str
    dest_tag_format: str
    version: str
    tag_plan: TagPlan
    source_trees: dict          # member name -> source HEAD tree hash
    outbound: list              # (member name, dep name, dep type, scope)
    departed_globs: list
    root_state_dir: str | None  # the source root's state home (releasable dir)
    source_repo_url: str

    @property
    def member_paths(self) -> list:
        return [m.path for m in self.members]

    @property
    def member_names(self) -> list:
        return [m.name for m in self.members]

    @property
    def is_multi(self) -> bool:
        return len(self.members) > 1

    @property
    def dest_state_dir(self) -> str:
        """Where the releasable's state directory lands in the destination."""
        if self.is_multi:
            return get_releasable_dir(self.target_path, self.releasable.name)
        return os.path.join(self.target_path, ".rlsbl")

    def dest_member_path(self, member) -> str:
        """A member's path in the destination (``""`` means the repo root)."""
        return member.path if self.is_multi else ""


@dataclass
class Applied:
    """What the apply pipeline learned as it ran, passed between its steps."""

    sha_map: dict = field(default_factory=dict)
    pruned_shas: list = field(default_factory=list)
    tag_mappings: list = field(default_factory=list)
    anchor_mappings: list = field(default_factory=list)
    unremapped_anchors: list = field(default_factory=list)
    state_commit: str = ""
    stack: ExitStack | None = None


# ---------------------------------------------------------------------------
# Small git / filesystem helpers
# ---------------------------------------------------------------------------


def _tree_hash(repo, path, rev="HEAD"):
    """The git tree object of ``path`` in ``repo`` at ``rev``.

    ``path`` of ``""`` or ``"."`` means the repository root, which is the
    ``rev^{tree}`` spelling rather than ``rev:``. The whole tree-identity
    verification funnels through here, so a test can make one side lie.
    """
    spec = f"{rev}^{{tree}}" if path in ("", ".") else f"{rev}:{path}"
    return _run_git(repo, "rev-parse", spec)


#: The git file mode of a gitlink -- the entry a submodule occupies in a tree.
GITLINK_MODE = "160000"


def _tracked_entries(repo, path):
    """``(mode, relative path)`` for everything tracked under ``path`` at HEAD.

    ``-z`` because the paths are read, not displayed: git's default output
    C-quotes any path outside plain ASCII, and a member with a non-ASCII file
    under it would be judged on an escaped spelling of its own contents.
    """
    out = _run_git(repo, "ls-tree", "-r", "-z", "HEAD", "--", path)
    entries = []
    for record in out.split("\0"):
        if not record:
            continue
        meta, _, entry_path = record.partition("\t")
        fields = meta.split()
        if not fields or not entry_path:
            continue
        entries.append((fields[0], entry_path))
    return entries


def _check_member_contents(workspace_root, members):
    """Refuse a member the conversion could not carry, before anything is done.

    Two shapes, both fatal at observation:

    * **a gitlink** (a submodule) under a member. The source-side edit is
      committed by naming the paths the working tree reports, and the commit
      tool refuses a gitlink path -- so an extract that reached that commit
      would already have deleted the member and would leave the source
      half-mutated with nothing recorded. Refusing here costs nothing.
    * **nothing tracked at all** at the member's path. Its tree object is what
      the conversion verifies identity with, and a path with no tree raises a
      raw ``git rev-parse`` failure deep in observation instead of saying which
      member is empty.
    """
    for member in members:
        entries = _tracked_entries(workspace_root, member.path)
        if not entries:
            raise ExtractError(
                f"member '{member.name}' has nothing tracked at {member.path}/ "
                f"in HEAD, so there is no tree for the conversion to carry or "
                f"verify. Commit the member's files, or remove it from the "
                f"releasable, before extracting."
            )
        gitlinks = [p for mode, p in entries if mode == GITLINK_MODE]
        if gitlinks:
            raise ExtractError(
                f"member '{member.name}' contains a submodule: "
                f"{', '.join(sorted(gitlinks))}. The conversion commits the "
                f"source-side removal by naming the paths the working tree "
                f"reports, and the commit tool refuses a gitlink path -- so "
                f"this extract would delete the member and then fail to record "
                f"it, leaving the source half-mutated. Remove the submodule, or "
                f"absorb its content into this repository, and re-run."
            )


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


def _delete_path(path, *, description, delete_with_rm):
    """Delete ``path`` through saferm, or through ``rm -rf`` when asked to.

    saferm is the default because a conversion's deletions are exactly the kind
    that want an audit trail and an undo. ``--delete-with-rm`` is the operator
    stating that this machine has no saferm and they accept a plain removal --
    which is why an absent saferm WITHOUT the flag is a refusal (raised during
    observation) rather than a silent downgrade.
    """
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


def _write_config(config_path, config):
    """Write a JSON config file, creating its directory when absent.

    ``config.write_project_config`` writes ``<root>/.rlsbl/config.json``, and the
    config this touches is the SOURCE ROOT's state home -- which is the root
    member's releasable directory when the root member belongs to one. The path
    is therefore passed in rather than derived from a project root.
    """
    import json

    effects.makedirs(os.path.dirname(config_path), exist_ok=True)
    effects.atomic_write_text(
        config_path, json.dumps(config, indent=2) + "\n", file_mode=0o600,
    )


def _root_state_dir(workspace_root, projects, releasables):
    """The source ROOT member's state home, or None when it has none.

    A releasable's own records live in its state directory; the root member's
    live in the state directory of the releasable it belongs to. A root member
    outside every releasable (the common dev-node root) has no releasable
    directory, and its state home is the repository's own ``.rlsbl/``. Both
    answers feed :func:`rlsbl.lineage.get_lineage_path` and the config writer,
    which is why one function resolves them.
    """
    root_member = find_root_member(projects)
    if root_member is None:
        return None
    rel = resolve_releasable_for_project(root_member, releasables)
    if rel is None:
        return None
    return get_releasable_dir(workspace_root, rel.name)


def _relative(workspace_root, path):
    """``path`` as a repo-relative path, for naming in a commit."""
    return os.path.relpath(path, workspace_root)


#: rlsbl's own advisory lock, relative to a workspace root.
LOCK_RELPATH = f"{WORKSPACE_DIR}/lock"


def _dirty_paths(root):
    """Working-tree changes at ``root``, minus rlsbl's own advisory lock.

    The lock file is this process's own infrastructure: it exists only while
    the conversion runs and is deleted on the way out. A scaffolded repository
    gitignores it, so it usually never shows up here at all -- the exemption is
    STRUCTURAL (a fixed path, never gitignore-derived) so that a repository
    whose ``.gitignore`` predates the entry cannot make the conversion refuse
    over its own lock, or worse, commit it.

    This is also the commit list for both repositories: naming what the tree
    actually reports cannot miss a path a step forgot to declare, and cannot
    name a directory whose untracked contents would be swept in with it.
    """
    return [
        path for path in working_tree_paths(cwd=root)
        if path.rstrip("/") != LOCK_RELPATH
    ]


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def _find_releasable(releasables, name):
    for rel in releasables:
        if rel.name == name:
            return rel
    available = ", ".join(sorted(r.name for r in releasables)) or "(none)"
    raise ExtractError(
        f"releasable '{name}' not found in this workspace. Available: {available}"
    )


def _other_member_globs(workspace_root, projects, releasables, *,
                        exclude_project_names, exclude_releasable_names):
    """The tag globs of every member/releasable that is NOT being extracted.

    These identify which scheme-parsing tags in the extracted clone are
    genuinely foreign -- another live member's release history, safe to prune.
    A scheme tag matching none of them is an ORPHAN and is kept: it is most
    likely the extracted releasable's own history under an older prefix.

    Resolved against the SOURCE workspace before anything is removed, because
    target detection reads the still-present member directories. A member whose
    target declaration is broken (a ``.rlsbl/config.json`` with no ``targets``
    key) makes that resolution impossible, and that is a hard error HERE --
    before any history is rewritten -- rather than a silent fallback to a
    default scheme that would prune the wrong tags.
    """
    globs = set()
    for proj in projects:
        if proj.name in exclude_project_names:
            continue
        rel = resolve_releasable_for_project(proj, releasables)
        try:
            globs.add(resolve_monorepo_tag_glob(proj, workspace_root, releasable=rel))
        except ConfigError as exc:
            cfg = os.path.join(proj.path, ".rlsbl", "config.json")
            raise ExtractError(
                f"member '{proj.name}' has a broken target declaration, so the "
                f"tags belonging to it cannot be told apart from the extracted "
                f"releasable's: {exc} Add a \"targets\" key to {cfg} (a member "
                f"with no .rlsbl/config.json is fine -- targets are "
                f"auto-detected)."
            ) from exc
    for rel in releasables:
        if rel.name in exclude_releasable_names:
            continue
        globs.add(releasable_tag_glob(rel.effective_tag_format, rel.name))
    return globs


def _plan_tags(workspace_root, own_glob, foreign_globs,
               own_format, dest_format, releasable_name, version):
    """Classify every tag in the source repository. Never touches a tag.

    Tag NAMES are the same in the clone as in the source (a clone carries every
    tag), so the whole classification -- including the collision pre-check -- is
    answerable before anything is cloned. The SHAs are not: filter-repo rewrites
    them, so they are resolved at apply time in the destination.
    """
    all_tags = set(_git_tag_names(workspace_root))
    own_tags = [t for t in _git_tag_names(workspace_root, own_glob)]
    own_set = set(own_tags)

    foreign_tags = set()
    for glob in foreign_globs:
        foreign_tags.update(_git_tag_names(workspace_root, glob))

    current_old_tag = own_format.format(name=releasable_name, version=version)
    translations = []
    deletions = []
    alias = None
    if own_format != dest_format:
        for tag in sorted(own_tags):
            parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
            if parsed is None:
                continue  # the glob matched a non-version tag; leave it alone
            new_tag = dest_format.format(
                name=releasable_name, version=parsed.version,
            )
            if new_tag == tag:
                continue
            if new_tag in all_tags and new_tag not in own_set:
                raise ExtractError(
                    f"tag translation collision: '{tag}' would be renamed to "
                    f"'{new_tag}', but a tag named '{new_tag}' already exists "
                    f"and is not this releasable's. Resolve the conflicting tag "
                    f"before extracting."
                )
            translations.append((tag, new_tag))
            if tag == current_old_tag:
                # The one boundary alias: the current version keeps its
                # pre-conversion name alongside the new one, so a consumer that
                # knows the old tag still resolves it in the new repository.
                # Every OTHER historical tag is renamed outright.
                alias = (new_tag, tag)
            else:
                deletions.append(tag)

    pruned = sorted(t for t in all_tags if t in foreign_tags and t not in own_set)
    return TagPlan(
        translations=tuple(translations),
        deletions=tuple(deletions),
        pruned=tuple(pruned),
        alias=alias,
    )


def _read_toml_doc(path):
    """Parse a TOML file, or None when it is absent or unreadable.

    Unreadable is None rather than an error on purpose: this reads manifests to
    decide which REMEDY to print for an edge that is already a refusal. A
    manifest nobody can parse costs its evidence, not the refusal.
    """
    if not os.path.isfile(path):
        return None
    try:
        import tomlkit

        with open(path, "r", encoding="utf-8") as f:
            return tomlkit.parse(f.read())
    except Exception:
        return None


def _read_json_doc(path):
    """Parse a JSON file, or None when it is absent or unreadable."""
    if not os.path.isfile(path):
        return None
    try:
        import json

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _member_spellings(workspace_root, member, *, manifest, key):
    """Every name the departing member can be spelled with in one ecosystem.

    Its workspace name, the ``registry_name`` the workspace declares for it,
    and the name its own manifest declares -- a member registered as ``pkgA``
    can perfectly well publish as ``acme-pkg-a`` and be depended on under that
    name.
    """
    names = {member.name}
    if member.registry_name:
        names.add(member.registry_name)
    path = os.path.join(workspace_root, member.path, manifest)
    doc = _read_toml_doc(path) if manifest.endswith(".toml") else _read_json_doc(path)
    if doc is not None:
        declared = key(doc)
        if isinstance(declared, str) and declared:
            names.add(declared)
    return names


def _go_module_path(workspace_root, path):
    """The module path a ``go.mod`` under *path* declares, or None."""
    gomod = os.path.join(workspace_root, path, "go.mod")
    if not os.path.isfile(gomod):
        return None
    try:
        with open(gomod, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            return stripped.split(None, 1)[1].strip().strip('"')
    return None


def _python_inbound_remedy(workspace_root, dependent, member):
    """The Python edit, when the dependent's pyproject really names the member."""
    from ...dep_floors import normalize_pypi_name
    from ...dep_rewrite import SECTIONS_ALL, detect_uv_path_sources, find_dep_entries

    dep_dir = os.path.join(workspace_root, dependent.path)
    doc = _read_toml_doc(os.path.join(dep_dir, "pyproject.toml"))
    if doc is None:
        return None
    spellings = _member_spellings(
        workspace_root, member,
        manifest="pyproject.toml",
        key=lambda doc: (doc.get("project") or {}).get("name"),
    )
    names = {normalize_pypi_name(n): n for n in spellings}
    entries = find_dep_entries(doc, names, SECTIONS_ALL)
    sources = [
        name for name in detect_uv_path_sources(doc)
        if normalize_pypi_name(name) in names
    ]
    if not entries and not sources:
        return None

    declared = sources[0] if sources else entries[0]["normalized"]
    edits = []
    if sources:
        edits.append(
            "delete " + ", ".join(f"[tool.uv.sources].{name}" for name in sources)
        )
    if entries:
        edits.append(
            "floor " + ", ".join(
                f"[{entry['section']}] entry {entry['original']!r}"
                for entry in entries
            )
        )
    lines = [
        f"    in {dependent.path}/pyproject.toml: {' and '.join(edits)}, so "
        f"'{declared}' resolves from the registry "
        f"('{declared}>=<the version the lock resolves>')."
    ]
    if os.path.isfile(os.path.join(dep_dir, "uv.lock")):
        lines.append(
            f"      `rlsbl rewrite uv-path-sources` writes exactly that edit: "
            f"(cd {dependent.path} && rlsbl rewrite uv-path-sources --dry-run)"
            f"  then re-run without --dry-run."
        )
    else:
        lines.append(
            f"      `rlsbl rewrite uv-path-sources` reads the floor from a "
            f"uv.lock beside the manifest it rewrites, and {dependent.path} "
            f"has no uv.lock of its own (a uv workspace resolves into one lock "
            f"at the repository root), so make this edit by hand."
        )
    return "\n".join(lines)


def _npm_inbound_remedy(workspace_root, dependent, member):
    """The npm edit. Stated in full: no rewrite command owns package.json."""
    doc = _read_json_doc(
        os.path.join(workspace_root, dependent.path, "package.json")
    )
    if not isinstance(doc, dict):
        return None
    spellings = _member_spellings(
        workspace_root, member,
        manifest="package.json",
        key=lambda doc: doc.get("name") if isinstance(doc, dict) else None,
    )
    for section in (
        "dependencies", "devDependencies", "peerDependencies",
        "optionalDependencies",
    ):
        entries = doc.get(section)
        if not isinstance(entries, dict):
            continue
        for name, spec in entries.items():
            if name not in spellings:
                continue
            return (
                f"    in {dependent.path}/package.json: replace "
                f'"{name}": "{spec}" in "{section}" with the published range '
                f'("{name}": "^<the version it is developed against>"), and '
                f'drop {member.path} from any "workspaces" array that lists '
                f"it. No rewrite command owns package.json -- this one is a "
                f"hand edit."
            )
    return None


def _go_inbound_remedy(workspace_root, dependent, member):
    """The Go edit, when the dependent's go.mod requires the member's module."""
    module = _go_module_path(workspace_root, member.path)
    if module is None:
        return None
    gomod = os.path.join(workspace_root, dependent.path, "go.mod")
    if not os.path.isfile(gomod):
        return None
    try:
        with open(gomod, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    if module not in text:
        return None
    return (
        f"    {dependent.path}/go.mod requires {module}, which moves with the "
        f"extraction:\n"
        f"      rlsbl rewrite go-module-path --from-module {module} "
        f"--to-module <its module path in the new repository>   (run at the "
        f"repository root, before extracting)"
    )


def _inbound_remedies(workspace_root, dependent, member, dep):
    """Every edit that severs one inbound dependency edge.

    Decided from the DEPENDING member's manifests on disk, never from the
    scanner's ``dep_type``: the Python scanner marks a ``[tool.uv.sources]``
    path/workspace edge ``"versioned"`` (only a direct ``name @ file://``
    reference is ``"path"``), so branching on that string routes the commonest
    Python edge to a Go remedy naming a go.mod that does not exist.

    Every remedy that applies is returned, because an edge can be declared in
    more than one place -- a ``depends_on`` in workspace.toml AND the manifest
    that really carries it -- and severing one of them leaves the other.
    """
    remedies = []
    if dep.dep_type == "explicit":
        remedies.append(
            f"    remove '{member.name}' from depends_on in "
            f"{WORKSPACE_DIR}/workspace.toml for member '{dependent.name}'"
        )
    for probe in (
        _python_inbound_remedy, _npm_inbound_remedy, _go_inbound_remedy,
    ):
        remedy = probe(workspace_root, dependent, member)
        if remedy is not None:
            remedies.append(remedy)
    if not remedies:
        remedies.append(
            f"    no manifest in {dependent.path} names '{member.name}' (the "
            f"workspace graph read this edge as '{dep.dep_type}'), so sever it "
            f"wherever it is declared before re-running."
        )
    return remedies


def _check_inbound(workspace_root, graph, projects, members, member_names):
    """Refuse when a REMAINING member depends on one that is leaving.

    The edge cannot survive the conversion: the depended-on package will not be
    in this repository any more. Severing it is a manifest rewrite, and rlsbl
    has commands that do exactly that -- so extract refuses and names them
    rather than editing somebody's manifest as a side effect of a conversion.
    """
    by_name = {p.name: p for p in projects}
    problems = []
    for member in members:
        # ``_rdeps`` rather than ``dependents()`` because the scope is half the
        # finding, and only the private mapping carries it (monorepo graph reads
        # it the same way).
        for dependent_name, scope in graph._rdeps.get(member.name, []):
            if dependent_name in member_names:
                continue  # an edge between two departing members travels along
            dependent = by_name.get(dependent_name)
            dep = next(
                (d for d in graph.dependencies(dependent_name)
                 if d.name == member.name),
                None,
            )
            if dependent is None or dep is None:
                continue
            problems.append((dependent, member, dep, scope))
    if not problems:
        return
    lines = []
    for dependent, member, dep, scope in problems:
        lines.append(
            f"  - '{dependent.name}' depends on '{member.name}' "
            f"({dep.dep_type}, scope {scope})"
        )
        lines.extend(
            _inbound_remedies(workspace_root, dependent, member, dep)
        )
    raise ExtractError(
        "members that stay behind depend on members that would leave:\n"
        + "\n".join(lines)
        + "\nSever each edge first (extract never rewrites a manifest itself), "
        "then re-run."
    )


def _outbound_edges(graph, members, member_names):
    """Edges FROM a departing member TO one that stays -- reported, not refused.

    The departing manifest keeps a reference to a package that is no longer a
    sibling. That is a normal, resolvable situation (the dependency is published,
    or becomes published), unlike the inbound direction where a repository that
    stays behind is left pointing at nothing. It is stated in the plan so the
    operator sees it before the conversion, not after.
    """
    edges = []
    for member in members:
        for dep in graph.dependencies(member.name):
            if dep.name in member_names:
                continue
            edges.append((member.name, dep.name, dep.dep_type, dep.scope))
    return edges


def _check_no_inflight(workspace_root, releasables):
    """Hard-error when a release is in flight anywhere in the workspace."""
    from ...release_file import get_batch_release_file_path

    batch_file = get_batch_release_file_path(workspace_root)
    if os.path.isfile(batch_file):
        raise ExtractError(
            f"a workspace release file is in flight: {batch_file}. Finish or "
            f"remove the release before extracting."
        )
    for rel in releasables:
        in_progress = os.path.join(
            get_releasable_dir(workspace_root, rel.name),
            "releases", "in-progress.json",
        )
        if os.path.isfile(in_progress):
            raise ExtractError(
                f"releasable '{rel.name}' has a release in progress: "
                f"{in_progress}. Resume or abort it before extracting."
            )


def resolve_departure(workspace_root, releasable_name, target_path, *,
                      delete_with_rm):
    """Resolve and validate one extraction. Reads only; refuses loudly.

    Every refusal in the command lives here or in the helpers it calls, so a
    ``--dry-run`` refuses exactly what an apply would, and neither has written
    anything by the time it does.
    """
    workspace_root = os.path.abspath(workspace_root)
    target_path = os.path.abspath(target_path)

    projects = load_workspace(workspace_root)
    releasables = load_releasables(workspace_root, projects)
    releasable = _find_releasable(releasables, releasable_name)

    members = members_of(releasable_name, projects)
    if not members:
        raise ExtractError(
            f"releasable '{releasable_name}' has no member packages, so there "
            f"is nothing to extract."
        )

    root_member = find_root_member(projects)
    if root_member is not None and root_member.name in {m.name for m in members}:
        raise ExtractError(
            f"releasable '{releasable_name}' owns the repository root (member "
            f"'{root_member.name}', path '.'). Every workspace has exactly one "
            f"root member and it owns every file no other member claims, so "
            f"extracting this releasable would leave the source with no root. "
            f"Move the root member into a releasable that stays, or give the "
            f"repository root a member of its own, before extracting."
        )

    _check_member_contents(workspace_root, members)

    mirrored = [m.name for m in members if m.subtree_remote]
    if mirrored:
        raise ExtractError(
            f"releasable '{releasable_name}' is mirrored: "
            f"{', '.join(mirrored)} declare a subtree_remote. The mirror is a "
            f"tool-owned artifact derived from THIS repository, and extracting "
            f"the releasable would leave it deriving from a subtree that no "
            f"longer exists. Promoting a mirror into the real repository is its "
            f"own operation; until then, remove the subtree_remote binding (and "
            f"the mirror remote) first."
        )

    if os.path.exists(target_path):
        raise ExtractError(f"target path already exists: {target_path}")

    require_filter_repo()

    if not delete_with_rm and shutil.which("saferm") is None:
        raise ExtractError(
            "saferm is not installed, and the extracted members' directories "
            "have to be deleted from the source. Install saferm (deletions get "
            "an audit trail and stay recoverable), or re-run with "
            "--delete-with-rm to use a plain rm -rf instead."
        )

    dirty = _dirty_paths(workspace_root)
    if dirty:
        raise ExtractError(
            f"the source working tree has uncommitted changes "
            f"({', '.join(dirty)}). The conversion filters COMMITTED history, "
            f"so uncommitted work would be silently dropped. Commit it first."
        )

    _check_no_inflight(workspace_root, releasables)

    graph = WorkspaceGraph(workspace_root, projects)
    if graph.scan_errors:
        named = "; ".join(f"{e.project}: {e.path}" for e in graph.scan_errors)
        raise ExtractError(
            f"some member manifests could not be read, so the dependency edges "
            f"into the extracted releasable cannot be established: {named}. Fix "
            f"the manifests and re-run -- a conversion that cannot see an edge "
            f"would leave it dangling."
        )
    member_names = {m.name for m in members}
    _check_inbound(workspace_root, graph, projects, members, member_names)
    outbound = _outbound_edges(graph, members, member_names)

    is_multi = len(members) > 1
    own_format = releasable.effective_tag_format
    dest_format = own_format if is_multi else STANDALONE_TAG_FORMAT
    # The version is read from the releasable's state directory, which is also
    # the directory the conversion transplants. A workspace that declares
    # [[releasables]] but keeps its release state per package has not finished
    # migrating to the releasable model, and there is nothing to carry over --
    # so this is where that is said, before any history is rewritten.
    try:
        version = read_releasable_version(workspace_root, releasable_name)
    except WorkspaceError as exc:
        raise ExtractError(
            f"releasable '{releasable_name}' has no release state to carry "
            f"over: {exc}. Its state directory "
            f"({get_releasable_dir(workspace_root, releasable_name)}) holds the "
            f"version, changelog and release archives the conversion moves. Run "
            f"`rlsbl monorepo migrate-releasable {releasable_name}` first if "
            f"this workspace still keeps that state per package."
        ) from exc

    own_glob = releasable_tag_glob(own_format, releasable_name)
    foreign_globs = _other_member_globs(
        workspace_root, projects, releasables,
        exclude_project_names=member_names,
        exclude_releasable_names={releasable_name},
    )
    tag_plan = _plan_tags(
        workspace_root, own_glob, foreign_globs,
        own_format, dest_format, releasable_name, version,
    )

    source_trees = {m.name: _tree_hash(workspace_root, m.path) for m in members}

    return Departure(
        workspace_root=workspace_root,
        target_path=target_path,
        releasable=releasable,
        members=list(members),
        projects=list(projects),
        releasables=list(releasables),
        delete_with_rm=delete_with_rm,
        source_state_dir=get_releasable_dir(workspace_root, releasable_name),
        dest_tag_format=dest_format,
        version=version,
        tag_plan=tag_plan,
        source_trees=source_trees,
        outbound=outbound,
        departed_globs=[own_glob],
        root_state_dir=_root_state_dir(workspace_root, projects, releasables),
        source_repo_url=_origin_url(workspace_root),
    )


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


def _next_steps(dep):
    """The steps rlsbl deliberately does NOT take on the operator's behalf."""
    steps = [
        f"create the remote repository and add it as origin in "
        f"{dep.target_path}",
        f"cd {dep.target_path} && rlsbl scaffold  (CI, hooks, workflows)",
        f"review the regenerated CI router in {dep.workspace_root} before the "
        f"next release (monorepo sync is re-run for you, but which jobs the "
        f"remaining members need is yours to confirm)",
    ]
    for target in _repository_bound_publishers(dep):
        steps.append(
            f"{target.registry_display_name} publishing is authorized for a "
            f"REPOSITORY, not for the package, so it does not follow the code: "
            f"register the new repository at {target.publisher_setup_url} "
            f"before its first release there (a publish that fails for want of "
            f"one is recovered with `rlsbl release retry`, not a new version)"
        )
    return steps


def _repository_bound_publishers(dep):
    """The departing members' targets whose publisher names the repository.

    Asked of the target rather than derived from its name: which registries
    bind publishing to a repository is the registry's fact, and the target
    registry is where rlsbl keeps those.

    A member whose targets cannot be detected contributes no hint. That is
    deliberate: the hint is guidance, and a broken declaration on a DEPARTING
    member is not otherwise this conversion's business (its tag scheme comes
    from the releasable).
    """
    from ...targets import TARGETS, detect_targets, resolve_releasable_config_dir

    seen = {}
    for member in dep.members:
        try:
            entries = detect_targets(
                os.path.join(dep.workspace_root, member.path),
                releasable_config_dir=resolve_releasable_config_dir(
                    member, dep.workspace_root,
                ),
            )
        except ConfigError:
            continue
        for entry in entries:
            target = TARGETS.get(entry.name)
            if target is not None and target.publisher_binds_to_repository:
                seen.setdefault(entry.name, target)
    return [seen[name] for name in sorted(seen)]


def _state_entries(dep):
    """The state-directory entries that will be transplanted, in order."""
    if not os.path.isdir(dep.source_state_dir):
        return []
    entries = sorted(os.listdir(dep.source_state_dir))
    if not dep.is_multi:
        entries = [e for e in entries if e not in STANDALONE_SKIPPED_STATE]
    return entries


def _archived_versions(dep):
    """The archived release files (``v*.toml``) the transplant carries."""
    releases_dir = os.path.join(dep.source_state_dir, "releases")
    if not os.path.isdir(releases_dir):
        return []
    return sorted(
        name for name in os.listdir(releases_dir)
        if name.startswith("v") and name.endswith(".toml")
    )


def observe(dep) -> Preview:
    """The whole plan, as a keyed verdict list in apply order."""
    items = []

    shape = "workspace" if dep.is_multi else "standalone repository"
    items.append(VerdictItem(
        key=ITEM_RELEASABLE,
        state="extract_to_workspace" if dep.is_multi else "extract_to_standalone",
        summary=(
            f"releasable '{dep.releasable.name}' (version {dep.version}) "
            f"becomes a {shape} at {dep.target_path}."
        ),
        facts=tuple(
            [f"member: {m.name} at {m.path}/" for m in dep.members]
            + [f"tag format: {dep.releasable.effective_tag_format} -> "
               f"{dep.dest_tag_format}"]
        ),
        actions=(
            f"apply would clone the source and run git-filter-repo keeping "
            f"{', '.join(dep.member_paths)}"
            + ("" if dep.is_multi else f", hoisting {dep.member_paths[0]}/ to "
                                       f"the repository root"),
        ),
    ))

    items.append(VerdictItem(
        key=ITEM_DEPENDENCIES,
        state="outbound_edges" if dep.outbound else "no_edges_to_sever",
        summary=(
            "no member that stays behind depends on a departing member."
            if not dep.outbound else
            f"{len(dep.outbound)} dependency edge(s) leave the workspace with "
            f"the extracted members."
        ),
        facts=tuple(
            f"{src} depends on {name} ({dep_type}, scope {scope}) -- it stays "
            f"behind and must be resolved from a registry in the new repository"
            for src, name, dep_type, scope in dep.outbound
        ),
    ))

    items.append(VerdictItem(
        key=ITEM_TREES,
        state="verify_member_trees",
        summary="every member's tree object must survive the filter unchanged.",
        facts=tuple(
            f"{m.name}: {dep.source_trees[m.name][:12]} at "
            f"{m.path}/ -> {dep.dest_member_path(m) or '<repo root>'}"
            for m in dep.members
        ),
        actions=(
            "apply would compare each hash against the filtered result and "
            "hard-error on any mismatch, naming both hashes.",
        ),
    ))

    entries = _state_entries(dep)
    archives = _archived_versions(dep)
    items.append(VerdictItem(
        key=ITEM_STATE,
        state="transplant_state",
        summary=(
            f"the releasable's state directory moves to "
            f"{_relative(dep.target_path, dep.dest_state_dir)}/."
        ),
        facts=tuple(
            [f"carries: {', '.join(entries) or '(nothing)'}"]
            + ([f"anchors to remap: {', '.join(archives)}"] if archives else [])
            + ([f"not carried (a standalone project's version is its "
                f"manifest's): {', '.join(STANDALONE_SKIPPED_STATE)}"]
               if not dep.is_multi else [])
        ),
        actions=(
            "apply would remap every changelog hash and every release anchor "
            "through filter-repo's commit map, and name what it could not map.",
        ),
    ))

    plan = dep.tag_plan
    items.append(VerdictItem(
        key=ITEM_TAGS,
        state="translate_tags" if plan.changes_names else "tags_unchanged",
        summary=(
            f"{len(plan.translations)} tag(s) translate to "
            f"{dep.dest_tag_format}."
            if plan.changes_names else
            f"the destination keeps this releasable's tag format "
            f"({dep.dest_tag_format}), so no tag changes name."
        ),
        facts=tuple(
            [f"{old} -> {new}" for old, new in plan.translations]
            + ([f"boundary alias: {plan.alias[1]} is KEPT beside "
                f"{plan.alias[0]} at version {dep.version}"]
               if plan.alias else [])
            + ([f"pruned (another live member's): {', '.join(plan.pruned)}"]
               if plan.pruned else [])
        ),
    ))

    items.append(VerdictItem(
        key=ITEM_DESTINATION,
        state="synthesize_workspace" if dep.is_multi else "synthesize_standalone",
        summary=(
            f"the destination gets its own {WORKSPACE_DIR}/workspace.toml "
            f"(root member + {len(dep.members)} member(s), releasable "
            f"'{dep.releasable.name}' with an explicit tag_format)."
            if dep.is_multi else
            f"the destination gets .rlsbl/releasable.toml naming "
            f"'{dep.releasable.name}' with tag_format "
            f"{STANDALONE_TAG_FORMAT}."
        ),
        actions=(
            "apply would commit the transplanted state and then load the new "
            "repository through rlsbl's own loader, hard-erroring if it does "
            "not read back.",
        ),
    ))

    events = ["conversion (direction=extract)"]
    if plan.translations:
        events.append("tag-map")
    if archives:
        events.append("anchor-remap")
    if plan.alias:
        events.append("boundary-alias")
    items.append(VerdictItem(
        key=ITEM_LINEAGE,
        state="record_lineage",
        summary=(
            f"a lineage record in the destination explains the conversion: "
            f"{', '.join(events)}."
        ),
        facts=(
            f"destination record: "
            f"{_relative(dep.target_path, get_lineage_path(dep.target_path, releasable_dir=dep.dest_state_dir if dep.is_multi else None))}",
            f"source record: "
            f"{_relative(dep.workspace_root, _source_lineage_path(dep))} "
            f"(departed-globs: {', '.join(dep.departed_globs)})",
        ),
    ))

    floors = _departing_registry_names(dep)
    items.append(VerdictItem(
        key=ITEM_SOURCE,
        state="remove_members",
        summary=(
            f"the source loses {len(dep.members)} member(s) and the releasable "
            f"'{dep.releasable.name}'."
        ),
        facts=(
            f"deleted ({'rm -rf' if dep.delete_with_rm else 'saferm'}): "
            f"{', '.join(dep.member_paths)}, "
            f"{_relative(dep.workspace_root, dep.source_state_dir)}",
            f"workspace.toml loses the member entries and the [[releasables]] "
            f"entry",
            f"internal_dep_floors gains {', '.join(floors)} in "
            f"{', '.join(_relative(dep.workspace_root, p) for p in _floor_config_paths(dep)) or '(no releasable stays behind)'}",
            "the tags themselves STAY in the source; the departed-globs record "
            "is what explains them",
        ),
        actions=(
            "apply would re-run monorepo sync, regenerate the snapshot, and "
            "commit all of it as one commit.",
        ),
    ))

    items.append(VerdictItem(
        key=ITEM_NEXT_STEPS,
        # Not "next_steps": the renderer prints the key and then the state, and
        # a state spelled like its key reads as a stutter.
        state="operator_actions",
        summary="rlsbl never administers an external system; these are yours.",
        facts=tuple(_next_steps(dep)),
    ))

    return Preview(tuple(items))


def _departing_registry_names(dep):
    """The names the departing members publish under."""
    return sorted({m.registry_name or m.name for m in dep.members})


def _source_lineage_path(dep):
    """Where the SOURCE records the departure: the WORKSPACE-scoped record.

    ``<root>/.rlsbl-monorepo/lineage.jsonl``. A departure is a fact about this
    repository's tag namespace -- these globs stopped belonging here -- not
    about any releasable in it, and the departing releasable's own record leaves
    with the conversion. The other two candidates are both wrong:

    * ``<root>/.rlsbl/lineage.jsonl`` (the standalone home) cannot exist in a
      workspace at all: rlsbl's ``root-rlsbl-conflict`` check refuses a root
      ``.rlsbl/`` beside ``.rlsbl-monorepo/``, so writing there would make the
      source fail its own workspace checks;
    * a surviving releasable's record would file a repository-wide fact under
      whichever releasable happened to be picked.
    """
    return get_lineage_path(dep.workspace_root, workspace=True)


# ---------------------------------------------------------------------------
# Apply: one step per preview item, in preview order
# ---------------------------------------------------------------------------


def _apply_filter(dep, item, run):
    """Clone the source and rewrite the clone down to the member paths."""
    run.stack.enter_context(
        rlsbl_lock(WORKSPACE_DIR, project_root=dep.workspace_root, wait=False)
    )
    # Re-check under the lock: between observation and here another process may
    # have written, and the clone captures whatever the tree is now.
    dirty = _dirty_paths(dep.workspace_root)
    if dirty:
        raise ExtractError(
            f"the source working tree became dirty after the plan was made "
            f"({', '.join(dirty)}); nothing was written. Commit or set aside "
            f"the changes and re-run."
        )

    print(f"Cloning {dep.workspace_root} -> {dep.target_path} ...")
    _run_git(dep.workspace_root, "clone", "--no-local", ".", dep.target_path)
    _ensure_git_identity(dep.target_path, dep.workspace_root)

    args = []
    for path in dep.member_paths:
        args += ["--path", path]
    _run_filter_repo(dep.target_path, *args, "--force")
    if not dep.is_multi:
        _run_filter_repo(
            dep.target_path, "--path-rename", f"{dep.member_paths[0]}/:", "--force",
        )

    commit_map = os.path.join(
        dep.target_path, ".git", "filter-repo", "commit-map",
    )
    run.sha_map, run.pruned_shas = load_filter_repo_commit_map(commit_map)
    print(
        f"  filter-repo mapped {len(run.sha_map)} commit(s); "
        f"{len(run.pruned_shas)} pruned."
    )


def _apply_dependencies(dep, item, run):
    """Nothing to do: the edges were judged during observation."""
    return


def _apply_trees(dep, item, run):
    """Verify tree-object identity per member. Any mismatch is fatal."""
    for member in dep.members:
        expected = dep.source_trees[member.name]
        dest_path = dep.dest_member_path(member)
        actual = _tree_hash(dep.target_path, dest_path)
        if actual != expected:
            raise ExtractError(
                f"tree verification failed for member '{member.name}': the "
                f"source tree at {member.path}/ is {expected}, but the "
                f"filtered result at "
                f"{dest_path or '<repo root>'} is {actual}. The extracted "
                f"history is not the history that left, so nothing further was "
                f"written; the source is untouched and "
                f"{dep.target_path} can be deleted."
            )
        print(f"  {member.name}: tree {expected[:12]} verified.")


def _apply_state(dep, item, run):
    """Transplant the state directory, then remap its hashes and anchors."""
    if not os.path.isdir(dep.source_state_dir):
        raise ExtractError(
            f"releasable '{dep.releasable.name}' has no state directory at "
            f"{dep.source_state_dir}; there is no release state to carry over."
        )

    effects.makedirs(dep.dest_state_dir, exist_ok=True)
    for name in _state_entries(dep):
        src = os.path.join(dep.source_state_dir, name)
        dst = os.path.join(dep.dest_state_dir, name)
        if os.path.isdir(src):
            effects.copytree(src, dst, dirs_exist_ok=True)
        elif name == "config.json" and not dep.is_multi:
            _merge_standalone_config(dep, src, dst)
        else:
            effects.copy_file(src, dst)

    _remap_changelog(dep, run)
    _remap_anchors(dep, run)


def _merge_standalone_config(dep, releasable_config, dest_config):
    """Merge the releasable config under the member's own, for a flat repo.

    In a workspace the releasable-level config is the base and a member's own
    ``.rlsbl/config.json`` overrides individual keys. A standalone successor has
    one config file where both used to live, so the same precedence is applied
    once, here, rather than left to whichever file happened to be copied last.
    """
    import json

    merged = read_json_config(releasable_config)
    merged.update(read_json_config(dest_config))
    effects.atomic_write_text(
        dest_config, json.dumps(merged, indent=2) + "\n", file_mode=0o600,
    )


def _remap_changelog(dep, run):
    """Map the transplanted changelog hashes onto the rewritten commits."""
    changes_dir = os.path.join(dep.dest_state_dir, "changes")
    if not os.path.isdir(changes_dir):
        return
    from ...changelog.files import remap_jsonl_hashes

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
    dropped = _prune_dangling_entries(changes_dir, dep.target_path)
    if dropped:
        print(
            f"  changelog: dropped {dropped} entry/entries whose commits the "
            f"filter pruned.",
            file=sys.stderr,
        )


def _map_sha(old, sha_map):
    """Map one (possibly abbreviated) SHA through filter-repo's commit map."""
    if old in sha_map:
        return sha_map[old]
    matches = [new for key, new in sha_map.items() if key.startswith(old)]
    if len(matches) == 1:
        return matches[0]
    return None


def _remap_anchors(dep, run):
    """Remap every archived release anchor onto the rewritten history.

    An archive records which commit a version shipped from and the tree of every
    path it shipped. Both are stated in the SOURCE's object graph, which the
    filter has just replaced, so both are rewritten here: the commit through
    filter-repo's map, the trees recomputed at the new commit and the path the
    member now has.

    An anchor whose commit the filter pruned is left exactly as it was and NAMED
    on stderr. Rewriting it to nothing would be worse (the fields are the record
    of what shipped), and aborting mid-conversion would leave a half-converted
    pair of repositories -- the lineage record is what explains the stale value.
    """
    releases_dir = os.path.join(dep.dest_state_dir, "releases")
    if not os.path.isdir(releases_dir):
        return
    for name in _archived_versions(dep):
        path = os.path.join(releases_dir, name)
        config = read_release_file(path)
        if not config.candidate_sha or not config.tree_hashes:
            continue
        new_sha = _map_sha(config.candidate_sha, run.sha_map)
        if new_sha is None:
            run.unremapped_anchors.append((name, config.candidate_sha))
            print(
                f"  anchor: {name} still names {config.candidate_sha[:12]}, a "
                f"commit the filter pruned; left as recorded.",
                file=sys.stderr,
            )
            continue
        trees = {}
        failed = None
        for old_path in config.tree_hashes:
            new_path = _dest_anchor_path(dep, old_path)
            try:
                trees[new_path] = _tree_hash(
                    dep.target_path, new_path, rev=new_sha,
                )
            except subprocess.CalledProcessError:
                failed = old_path
                break
        if failed is not None:
            run.unremapped_anchors.append((name, config.candidate_sha))
            print(
                f"  anchor: {name} names path '{failed}', which does not "
                f"resolve at the rewritten commit; left as recorded.",
                file=sys.stderr,
            )
            continue
        effects.chmod(path, 0o644)
        write_release_anchor(path, candidate_sha=new_sha, tree_hashes=trees)
        effects.chmod(path, 0o444)
        run.anchor_mappings.append(
            AnchorMapping(old_sha=config.candidate_sha, new_sha=new_sha)
        )
        print(
            f"  anchor: {name} {config.candidate_sha[:12]} -> {new_sha[:12]} "
            f"({', '.join(sorted(trees))})"
        )


def _dest_anchor_path(dep, old_path):
    """An anchored path's spelling in the destination.

    A workspace releasable's anchor is keyed by member path and those paths are
    unchanged; a standalone successor hoisted its one member to the root, so
    that member's key becomes ``"."`` -- the same spelling a standalone release
    writes.
    """
    if dep.is_multi:
        return old_path
    if old_path in (dep.member_paths[0], "."):
        return "."
    return old_path


def _apply_tags(dep, item, run):
    """Retag the clone: translate own tags, keep one alias, prune foreign ones."""
    present = set(_git_tag_names(dep.target_path))
    mappings = []
    for old, new in dep.tag_plan.translations:
        if old not in present:
            print(
                f"  tag: '{old}' did not survive the filter; not translated.",
                file=sys.stderr,
            )
            continue
        sha = _run_git(dep.target_path, "rev-list", "-n", "1", old)
        _run_git(dep.target_path, "tag", new, sha)
        mappings.append(TagMapping(old_tag=old, new_tag=new, new_commit=sha))
    for old in dep.tag_plan.deletions:
        if old in present:
            _run_git(dep.target_path, "tag", "-d", old)
    for tag in dep.tag_plan.pruned:
        if tag in present:
            _run_git(dep.target_path, "tag", "-d", tag)
    run.tag_mappings = mappings

    # What is left over: a tag that parses as a version tag under SOME scheme
    # but is neither this releasable's own (in either the source or the
    # destination spelling) nor another live member's. The conservative rule
    # keeps it -- it is most likely this releasable's own history under a prefix
    # it used before a rename -- and says so, because a kept foreign-looking tag
    # in a fresh repository is otherwise a mystery.
    own = set(_git_tag_names(dep.target_path, dep.departed_globs[0]))
    own |= {new for _old, new in dep.tag_plan.translations}
    if dep.tag_plan.alias:
        own.add(dep.tag_plan.alias[1])
    for tag in _git_tag_names(dep.target_path):
        if tag in own:
            continue
        if parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE) is None:
            continue
        print(
            f"  tag: keeping '{tag}' -- it matches no current member's scheme, "
            f"so it is most likely this releasable's own history under an "
            f"older prefix.",
            file=sys.stderr,
        )


def _apply_destination(dep, item, run):
    """Give the destination the identity its own loader needs, then commit."""
    if dep.is_multi:
        _write_destination_workspace(dep)
    else:
        _write_standalone_releasable(dep)

    paths = _dirty_paths(dep.target_path)
    if paths:
        commit_files(
            f"chore: carry {dep.releasable.name} state over from the monorepo",
            paths,
            cwd=dep.target_path,
        )
    leftover = _dirty_paths(dep.target_path)
    if leftover:
        raise ExtractError(
            f"the extracted repository still has uncommitted changes after its "
            f"migration commit: {', '.join(leftover)}"
        )
    run.state_commit = _run_git(dep.target_path, "rev-parse", "HEAD")
    _assert_destination_loads(dep)


def _write_destination_workspace(dep):
    """Write the new repository's workspace.toml.

    It carries a dev-node ROOT member (the extracted members keep their own
    subdirectories, so nothing owns the repository root otherwise, and a
    workspace without a root member does not load), and the releasable with an
    EXPLICIT tag_format -- the tags travelled unchanged, and a format left to
    the default would be a different question than the one they answer.
    """
    from ...workspace_types import Releasable

    carried = ("library", "registry_name", "import_name")
    member_names = set(dep.member_names)
    projects = [WorkspaceProject({
        "path": ".", "name": "root", "dev_only": True, "releasable": False,
    })]
    for member in dep.members:
        data = {
            "path": member.path,
            "name": member.name,
            "releasable": dep.releasable.name,
        }
        for key in carried:
            value = member.get(key)
            if value:
                data[key] = value
        # Only edges that travelled with the conversion survive; an edge to a
        # member that stayed behind is a registry dependency now, not a
        # workspace one, and declaring it would name a project that does not
        # exist in this workspace.
        depends = [d for d in member.depends_on if d in member_names]
        if depends:
            data["depends_on"] = depends
        projects.append(WorkspaceProject(data))
    save_workspace(
        dep.target_path,
        projects,
        releasables=[Releasable(
            name=dep.releasable.name, tag_format=dep.dest_tag_format,
        )],
    )


def _write_standalone_releasable(dep):
    """Write ``.rlsbl/releasable.toml`` for a single-member successor.

    ``create_standalone_releasable`` would otherwise derive the name from the
    manifest, which is not necessarily the releasable's name -- and the name is
    what its tags, its changelog home and its release state were written under.
    Stating it explicitly is what makes the successor read back as the same
    releasable that left.
    """
    from ...workspace import STANDALONE_RELEASABLE_FILE

    path = os.path.join(dep.target_path, ".rlsbl", STANDALONE_RELEASABLE_FILE)
    effects.makedirs(os.path.dirname(path), exist_ok=True)
    effects.write_text(
        path,
        f'name = "{dep.releasable.name}"\n'
        f'tag_format = "{STANDALONE_TAG_FORMAT}"\n',
    )


def _assert_destination_loads(dep):
    """The new repository must read back through rlsbl's own loader."""
    try:
        if dep.is_multi:
            projects = load_workspace(dep.target_path)
            names = {p.name for p in load_releasables(dep.target_path, projects)}
            if dep.releasable.name not in names:
                raise ExtractError(
                    f"the extracted workspace does not declare releasable "
                    f"'{dep.releasable.name}' (declares: {sorted(names)})"
                )
        else:
            releasable = load_standalone_releasable(dep.target_path)
            if releasable is None or releasable.name != dep.releasable.name:
                raise ExtractError(
                    f"the extracted repository does not identify as releasable "
                    f"'{dep.releasable.name}'"
                )
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError(
            f"the extracted repository at {dep.target_path} does not load "
            f"through rlsbl's own loader: {exc}"
        ) from exc


def _apply_lineage(dep, item, run):
    """Write the destination's lineage record: conversion first, then the rest."""
    path = get_lineage_path(
        dep.target_path,
        releasable_dir=dep.dest_state_dir if dep.is_multi else None,
    )
    conversion = ConversionEvent(
        direction="extract",
        source=LineageEndpoint(
            repo=dep.source_repo_url,
            path=dep.member_paths[0] if not dep.is_multi else None,
            project=dep.member_names[0] if not dep.is_multi else None,
            releasable=dep.releasable.name,
            tag_format=dep.releasable.effective_tag_format,
        ),
        destination=LineageEndpoint(
            repo=".",
            releasable=dep.releasable.name,
            tag_format=dep.dest_tag_format,
        ),
        commit=run.state_commit,
    )
    [stamped] = append_events(path, [conversion])

    followers = []
    mappings = getattr(run, "tag_mappings", [])
    if mappings:
        followers.append(TagMapEvent(mappings=mappings, related_to=stamped.id))
    if run.anchor_mappings:
        followers.append(AnchorRemapEvent(
            rewrite="git-filter-repo --path (release anchors)",
            mappings=run.anchor_mappings,
            related_to=stamped.id,
        ))
    if dep.tag_plan.alias:
        new_tag, old_tag = dep.tag_plan.alias
        commit = _run_git(dep.target_path, "rev-list", "-n", "1", new_tag)
        followers.append(BoundaryAliasEvent(
            aliases=[BoundaryAlias(
                alias_tag=new_tag, aliased_tag=old_tag, commit=commit,
            )],
            related_to=stamped.id,
        ))
    if followers:
        append_events(path, followers)

    commit_files(
        f"chore: record the {dep.releasable.name} extract lineage",
        [os.path.relpath(path, dep.target_path)],
        cwd=dep.target_path,
    )
    print(f"  lineage: {len(followers) + 1} event(s) recorded.")


def _apply_source(dep, item, run):
    """Remove the departed members from the source and commit the whole edit."""
    lineage_path = _source_lineage_path(dep)
    append_events(lineage_path, [DepartedGlobsEvent(
        globs=list(dep.departed_globs),
        destination=LineageEndpoint(
            repo=dep.target_path,
            releasable=dep.releasable.name,
            tag_format=dep.dest_tag_format,
        ),
    )])

    _declare_dep_floors(dep)

    for member in dep.members:
        _delete_path(
            os.path.join(dep.workspace_root, member.path),
            description=(
                f"Member '{member.name}' left this repository with releasable "
                f"'{dep.releasable.name}' (rlsbl monorepo extract)"
            ),
            delete_with_rm=dep.delete_with_rm,
        )
    _delete_path(
        dep.source_state_dir,
        description=(
            f"Release state of releasable '{dep.releasable.name}', which left "
            f"this repository (rlsbl monorepo extract)"
        ),
        delete_with_rm=dep.delete_with_rm,
    )

    remaining = [p for p in dep.projects if p.name not in set(dep.member_names)]
    remaining_releasables = [
        r for r in dep.releasables if r.name != dep.releasable.name
    ]
    save_workspace(dep.workspace_root, remaining, releasables=remaining_releasables)

    from .sync import _cmd_sync

    _cmd_sync({"auto-commit": False}, project_root=dep.workspace_root)

    projects = load_workspace(dep.workspace_root)
    graph = WorkspaceGraph(dep.workspace_root, projects)
    write_snapshot(
        dep.workspace_root, generate_snapshot(dep.workspace_root, projects, graph),
    )

    commit_files(
        f"monorepo: extract releasable {dep.releasable.name}",
        _dirty_paths(dep.workspace_root),
        cwd=dep.workspace_root,
    )
    leftover = _dirty_paths(dep.workspace_root)
    if leftover:
        raise ExtractError(
            f"the source repository still has uncommitted changes after the "
            f"extract commit: {', '.join(leftover)}. The extracted repository "
            f"at {dep.target_path} is complete; commit or revert the leftovers "
            f"in the source by hand."
        )
    print(
        f"  source: removed {', '.join(dep.member_names)} and releasable "
        f"'{dep.releasable.name}'; {SNAPSHOT_FILE} regenerated."
    )


def _floor_config_paths(dep):
    """Which config files the source declares the departed packages' floors in.

    Every releasable that STAYS, because that is the set of configs the
    ``dep-floors`` check reads: it compares a manifest against its lock using
    the config resolved for that releasable, so a declaration anywhere else
    polices nothing.

    Not the repository root's ``.rlsbl/config.json``: rlsbl's own
    ``root-rlsbl-conflict`` check refuses a root ``.rlsbl/`` beside
    ``.rlsbl-monorepo/``, so in a workspace whose root member owns no releasable
    there is no legal root config to write. The releasable configs are the
    workspace's equivalent of it, and one of them IS the root member's own when
    the root member belongs to a releasable.
    """
    return [
        os.path.join(get_releasable_dir(dep.workspace_root, rel.name), "config.json")
        for rel in dep.releasables
        if rel.name != dep.releasable.name
    ]


def _declare_dep_floors(dep):
    """Add the departing packages to the remaining releasables' floors.

    They are external packages from now on: anything here that ends up
    depending on one must declare a floor at the version it was developed
    against, and the ``dep-floors`` check only polices packages named in
    ``internal_dep_floors``. Declaring them now is what makes the check speak up
    the first time somebody adds the dependency back through a registry.
    """
    from ...dep_floors import CONFIG_KEY

    names = _departing_registry_names(dep)
    paths = _floor_config_paths(dep)
    if not paths:
        print(
            f"  {CONFIG_KEY}: no releasable stays behind, so there is no config "
            f"to declare {', '.join(names)} in. Declare them by hand if this "
            f"repository ever depends on them again.",
            file=sys.stderr,
        )
        return
    for config_path in paths:
        config = read_json_config(config_path)
        declared = config.get(CONFIG_KEY)
        config[CONFIG_KEY] = sorted(
            set(declared if isinstance(declared, list) else []) | set(names)
        )
        _write_config(config_path, config)
    print(
        f"  {CONFIG_KEY}: {', '.join(names)} declared in "
        f"{', '.join(_relative(dep.workspace_root, p) for p in paths)}"
    )


def _apply_next_steps(dep, item, run):
    """Print what the operator still has to do. rlsbl administers nothing."""
    if run.unremapped_anchors:
        # Said once more at the end, where it will still be on screen: these
        # archives record a commit that no longer exists in the new repository,
        # and the lineage record is what explains why.
        print(
            "\nRelease anchors left as recorded (their commits did not survive "
            "the filter):",
            file=sys.stderr,
        )
        for name, sha in run.unremapped_anchors:
            print(f"  - {name}: {sha[:12]}", file=sys.stderr)
    print("\nNext steps (rlsbl never administers an external system):")
    for step in _next_steps(dep):
        print(f"  - {step}")


_APPLY_STEPS = {
    ITEM_RELEASABLE: _apply_filter,
    ITEM_DEPENDENCIES: _apply_dependencies,
    ITEM_TREES: _apply_trees,
    ITEM_STATE: _apply_state,
    ITEM_TAGS: _apply_tags,
    ITEM_DESTINATION: _apply_destination,
    ITEM_LINEAGE: _apply_lineage,
    ITEM_SOURCE: _apply_source,
    ITEM_NEXT_STEPS: _apply_next_steps,
}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def cmd_extract(workspace_root, releasable_name, target_path, *,
                dry_run=False, delete_with_rm=False):
    """Extract a releasable into its own repository. Returns the Preview.

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: the releasable to extract, whole.
        target_path: where the new repository is created (must not exist).
        dry_run: render the plan and stop.
        delete_with_rm: delete the departed directories with ``rm -rf``
            instead of saferm. Without it, an absent saferm is a refusal.
    """
    dep = None

    def _observe():
        nonlocal dep
        dep = resolve_departure(
            workspace_root, releasable_name, target_path,
            delete_with_rm=delete_with_rm,
        )
        return observe(dep)

    run = Applied()

    def _apply(item):
        _APPLY_STEPS[item.key](dep, item, run)

    with ExitStack() as stack:
        run.stack = stack
        return reconcile(
            Reconciler(observe=_observe, apply_item=_apply, show_keys=True),
            dry_run=dry_run,
        )


def _cmd_extract(flags, project_root):
    """``rlsbl monorepo extract <releasable> <target-path>``."""
    root = find_workspace_root(str(project_root))
    if root is None:
        print(
            "Error: No workspace found. Run 'rlsbl monorepo init' first.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        cmd_extract(
            root,
            flags["releasable"],
            flags["target-path"],
            dry_run=bool(flags.get("dry-run", False)),
            delete_with_rm=bool(flags.get("delete-with-rm", False)),
        )
    except Exception as exc:  # noqa: BLE001 -- every failure is a CLI error
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
