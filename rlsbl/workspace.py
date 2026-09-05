"""Workspace data layer for monorepo support handling discovery, loading, saving, and resolution of workspaces from workspace.toml config."""

import dataclasses
import os
import tomllib

import tomlkit

from .errors import WorkspaceError

# Re-export core types and path utilities from workspace_types so that
# the 21+ existing import sites across the codebase continue to work
# unchanged.  Only targets/__init__.py imports from workspace_types
# directly (to break the circular dependency).
from .workspace_types import (  # noqa: F401
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    RELEASABLES_DIR,
    DEFAULT_TAG_FORMAT,
    MEMBER_KEYS,
    STANDALONE_TAG_FORMAT,
    TAG_FORMAT_ABSENT,
    Releasable,
    WorkspaceProject,
    is_runtime_member_key,
    get_releasable_dir,
    get_releasable_changes_dir,
    get_releasable_version_path,
    get_releasable_hook_path,
    project_is_dev_node,
    project_is_dev_only,
    project_is_releasable,
)
from .ownership import ROOT_MEMBER_NAME, ROOT_MEMBER_PATH, normalize_path  # noqa: F401
from . import effects


# ---------------------------------------------------------------------------
# Per-releasable version management
# ---------------------------------------------------------------------------


def read_releasable_version(workspace_root, releasable_name):
    """Read the version string from a releasable's version file.

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.

    Returns:
        The version string (stripped of whitespace).

    Raises:
        WorkspaceError: if the version file does not exist or is empty.
    """
    path = get_releasable_version_path(workspace_root, releasable_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            version = f.read().strip()
    except FileNotFoundError:
        raise WorkspaceError(
            f"version file missing for releasable '{releasable_name}': {path}"
        )
    if not version:
        raise WorkspaceError(
            f"version file is empty for releasable '{releasable_name}': {path}"
        )
    return version


def write_releasable_version(workspace_root, releasable_name, version):
    """Write a version string to a releasable's version file atomically.

    Creates the releasable directory if it does not exist. Writes to a
    temporary file in the same directory and then atomically replaces
    the target file via ``os.replace()``.

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.
        version: the version string to write.
    """
    path = get_releasable_version_path(workspace_root, releasable_name)
    target_dir = os.path.dirname(path)
    effects.makedirs(target_dir, exist_ok=True)

    # file_mode pins the 0o600 the mkstemp-based hand-rolled write produced
    # here before the chokepoint absorbed it (see the effects module).
    effects.atomic_write_text(path, version + "\n", file_mode=0o600)


def find_workspace_root(start_path="."):
    """Walk up from start_path looking for a .rlsbl-monorepo/workspace.toml.

    Returns the directory containing .rlsbl-monorepo/, or None if not found.
    """
    current = os.path.realpath(start_path)
    while True:
        candidate = os.path.join(current, WORKSPACE_DIR, WORKSPACE_FILE)
        if os.path.isfile(candidate):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def load_workspace(root):
    """Read and validate workspace.toml, returning a list of WorkspaceProject.

    Each project has at least 'path' (str) and 'name' (str, defaults to
    basename of path). The returned WorkspaceProject instances support
    dict-like access for backward compatibility.

    Raises FileNotFoundError if workspace.toml doesn't exist.
    Raises WorkspaceError on invalid structure.
    """
    path = os.path.join(root, WORKSPACE_DIR, WORKSPACE_FILE)
    with open(path, "rb") as f:
        data = tomllib.load(f)

    if "projects" not in data:
        raise WorkspaceError("workspace.toml missing required 'projects' key")

    projects = data["projects"]
    if not isinstance(projects, list):
        raise WorkspaceError("'projects' must be a list of tables")

    result = []
    for i, proj in enumerate(projects):
        if not isinstance(proj, dict):
            raise WorkspaceError(f"projects[{i}] must be a table, got {type(proj).__name__}")
        if "path" not in proj or not isinstance(proj["path"], str):
            raise WorkspaceError(f"projects[{i}] missing required 'path' string")
        entry = dict(proj)
        # One normalization, the resolver's own (rlsbl.ownership): a member
        # path is stored exactly as attribution will read it, so `a`, `./a`
        # and `a/` are one territory here as well as there.
        entry["path"] = normalize_path(entry["path"])
        if is_root_path(entry["path"]):
            # The repository root has one canonical spelling and one legal
            # name; an omitted name resolves to the reserved one rather than
            # to the basename of "" (which is "").
            entry["path"] = ROOT_MEMBER_PATH
            if "name" not in entry or not entry["name"]:
                entry["name"] = ROOT_MEMBER_NAME
        elif "name" not in entry or not entry["name"]:
            entry["name"] = os.path.basename(entry["path"])
        result.append(WorkspaceProject(entry))

    validate_workspace_model(data, result)

    return result


#: Every key workspace.toml carries at its top level. All three are sections
#: with a reader of their own -- the member list, the releasable list, and the
#: architectural layers (:mod:`rlsbl.layers`). A workspace has no top-level
#: scalar settings, so anything else there was written and never read.
WORKSPACE_TOP_LEVEL_KEYS = frozenset({"projects", "releasables", "layers"})


def releasable_keys() -> frozenset:
    """Every key a ``[[releasables]]`` table may carry.

    Derived from :class:`~rlsbl.workspace_types.Releasable`'s own fields rather
    than restated, so a field added to the model stops being unknown on the
    same edit that adds it.
    """
    return frozenset(f.name for f in dataclasses.fields(Releasable))


def _unknown_keys(table, known):
    """The keys of *table* outside *known*, in the order they were written."""
    return [key for key in table if key not in known]


def _refuse_unknown_keys(table, known, *, surface, file_label):
    """Refuse a table carrying a key rlsbl does not know.

    The message names the surface (which table, of what kind), the offending
    key, and the file it was read from -- everything an operator needs to find
    and delete the line. A tolerated unknown key is a line that was written and
    never read: the file would say something the tools never do.
    """
    unknown = _unknown_keys(table, known)
    if not unknown:
        return
    plural = "keys" if len(unknown) > 1 else "key"
    listed = ", ".join(f"'{key}'" for key in unknown)
    raise WorkspaceError(
        f"{file_label}: {surface} carries unknown {plural} {listed}. rlsbl "
        f"reads only {', '.join(sorted(known))} there, so a key outside that "
        f"set is written and never read -- the file would say something rlsbl "
        f"never does. Delete the line, or correct the spelling."
    )


def _refuse_releasable_unknown_keys(index, raw):
    """Refuse a ``[[releasables]]`` table carrying a key the model lacks."""
    name = raw.get("name")
    label = f"releasables[{index}]"
    if isinstance(name, str) and name:
        label += f" ('{name}')"
    _refuse_unknown_keys(
        raw, releasable_keys(),
        surface=f"{label}, a releasable table", file_label=WORKSPACE_FILE,
    )


def is_root_path(path) -> bool:
    """Does *path* spell the repository root (``""``, ``"."``, ``"./"``)?"""
    return str(path).strip().rstrip("/") in ("", ".")


# The last rlsbl version that reads an implicit-mode workspace (one with no
# [[releasables]] section). Named in the implicit-mode error so an operator who
# cannot convert right now has an exact, working pin.
LAST_IMPLICIT_MODE_VERSION = "0.117.2"

_MIGRATION_NOTE = (
    "A migration script applies this edit mechanically across a fleet of "
    "workspaces; for one workspace, make it by hand."
)


def _declared_paths(data, projects):
    """The path spellings as written, aligned with *projects* by index."""
    raw = data.get("projects")
    spellings = []
    for i, proj in enumerate(projects):
        entry = raw[i] if isinstance(raw, list) and i < len(raw) else None
        declared = entry.get("path") if isinstance(entry, dict) else None
        spellings.append(declared if isinstance(declared, str) else proj["path"])
    return spellings


def _validate_member_paths(data, projects):
    """Refuse a member list that does not give each path exactly one member.

    Two members claiming one territory make ownership depend on declaration
    order, so both spellings of the collision are refused at load: identical
    paths, and paths that differ only in spelling (``a`` and ``./a`` are the
    same directory, and normalize to the same member path).
    """
    declared = _declared_paths(data, projects)

    root_indices = [
        i for i, proj in enumerate(projects) if proj["path"] == ROOT_MEMBER_PATH
    ]
    if len(root_indices) > 1:
        first, second = root_indices[0], root_indices[1]
        raise WorkspaceError(
            f"projects[{first}] ('{projects[first]['name']}', path "
            f"'{declared[first]}') and projects[{second}] "
            f"('{projects[second]['name']}', path '{declared[second]}') both "
            f"declare the repository root. The root member owns every file no "
            f"other member claims, and a file has exactly one owner, so a "
            f"workspace has exactly one root member. Keep one of them and give "
            f"the other a path of its own -- rlsbl will not guess which member "
            f"owns the repository root."
        )

    seen = {}
    for i, proj in enumerate(projects):
        path = proj["path"]
        if path in seen:
            first = seen[path]
            spellings = ""
            if declared[first] != declared[i]:
                spellings = (
                    f" (spelled '{declared[first]}' and '{declared[i]}' -- the "
                    f"same directory)"
                )
            raise WorkspaceError(
                f"projects[{first}] ('{projects[first]['name']}') and "
                f"projects[{i}] ('{proj['name']}') both declare the path "
                f"'{path}'{spellings}. Every file has exactly one owner, which "
                f"requires exactly one member per path: with two, the owner "
                f"would be whichever member workspace.toml happens to declare "
                f"first. Merge the two entries into one, or give one of them a "
                f"path of its own."
            )
        seen[path] = i


def validate_workspace_model(data, projects):
    """Enforce the workspace ownership model on a parsed workspace.toml.

    Each condition below is a hard error carrying its own remedy, and they are
    reported in the order an operator can act on them:

    1. a key at the top level that is neither section -- first, because a
       misspelled section header is why the sections below look wrong;
    2. an implicit-mode workspace (no ``[[releasables]]``) -- because
       every other remedy below is written for an explicit-mode workspace;
    3. more than one root member;
    4. two members whose paths normalize to the same territory;
    5. no root member;
    6. a ``watch`` key on any member;
    7. a ``subtree_remote`` key on any member;
    8. a ``dev_node`` key on any member;
    9. any other unknown key on a member table;
    10. an unknown key on a ``[[releasables]]`` table;
    11. a root member named anything but ``root``;
    12. a non-root member named ``root``;
    13. a releasable owning the root member with no explicit ``tag_format``.

    Structural facts about the member list (3-5) precede per-member key
    errors (6-9): a remedy for a stray key presumes the member list itself
    is sound. The retired keys (6-8) precede the generic unknown-key refusal
    (9) so each keeps its own remedy.

    *data* is the raw parsed document (needed for the releasables section and
    for the paths as the operator spelled them), *projects* the already-built
    :class:`WorkspaceProject` list, whose paths are normalized.
    """
    # -- the top level: two sections and nothing else -------------------------
    _refuse_unknown_keys(
        data, WORKSPACE_TOP_LEVEL_KEYS,
        surface="the top level", file_label=WORKSPACE_FILE,
    )

    # -- (e) implicit mode ---------------------------------------------------
    raw_releasables = data.get("releasables")
    if raw_releasables is None:
        raise WorkspaceError(
            "workspace.toml has no [[releasables]] section: this is an "
            "implicit-mode workspace, and implicit mode is no longer "
            "supported. Every workspace declares its releasables explicitly. "
            "Convert this one -- add a [[releasables]] section and give every "
            "releasable member a `releasable = \"<name>\"` key (or "
            "`releasable = false`) -- or, if the conversion cannot happen now, "
            f"pin rlsbl to {LAST_IMPLICIT_MODE_VERSION}, the last version that "
            "reads an implicit-mode workspace, and file a todo in this "
            "repository to convert it."
        )

    # -- (g)/(h) one member per path, one root member ------------------------
    _validate_member_paths(data, projects)

    root_member = next(
        (p for p in projects if p["path"] == ROOT_MEMBER_PATH), None
    )

    # -- (a) the mandatory root member --------------------------------------
    if root_member is None:
        raise WorkspaceError(
            "workspace.toml declares no root member. Every workspace must "
            "declare the repository root itself as a member; it owns every "
            "tracked file no other member claims, so that no file is outside "
            "the ownership model. Add one, choosing its kind:\n"
            "\n"
            "  [[projects]]\n"
            "  path = \".\"\n"
            f"  name = \"{ROOT_MEMBER_NAME}\"\n"
            "  dev_only = true          # a dev node: its files need no "
            "changelog coverage\n"
            "  releasable = false       # ...and stands outside every "
            "releasable\n"
            "\n"
            "or, to give the root files changelog coverage:\n"
            "\n"
            "  [[projects]]\n"
            "  path = \".\"\n"
            f"  name = \"{ROOT_MEMBER_NAME}\"\n"
            "  releasable = \"<name of a [[releasables]] entry>\"\n"
            "\n" + _MIGRATION_NOTE
        )

    # -- (d) the watch key ---------------------------------------------------
    for i, proj in enumerate(projects):
        if "watch" in proj:
            raise WorkspaceError(
                f"projects[{i}] ('{proj['name']}'): the 'watch' key is no "
                f"longer supported. Territory is derived from declared member "
                f"paths, never enumerated: every file belongs to the member "
                f"with the most specific declared path, and the root member "
                f"owns everything no other member claims. Delete the "
                f"`watch = [...]` line; if this member genuinely needs to own "
                f"files outside its own directory, declare that directory as "
                f"a member of its own. " + _MIGRATION_NOTE
            )

    # -- the mirror destination moved onto the releasable --------------------
    for i, proj in enumerate(projects):
        if "subtree_remote" in proj:
            raise WorkspaceError(
                f"projects[{i}] ('{proj['name']}'): the 'subtree_remote' key "
                f"is no longer a member key. A mirror carries one subtree's "
                f"whole history, its tags and its GitHub Releases, and the "
                f"unit that owns a version, a changelog and a tag scheme is "
                f"the releasable -- so the mirror's destination is declared "
                f"there. Move the line into this member's [[releasables]] "
                f"entry:\n"
                f"\n"
                f"  [[releasables]]\n"
                f"  name = \"<the releasable '{proj['name']}' belongs to>\"\n"
                f"  subtree_remote = \"{proj['subtree_remote']}\"\n"
                f"\n"
                f"and delete it from this member. A releasable with more than "
                f"one member cannot declare one at all: there would be no "
                f"single subtree to mirror. " + _MIGRATION_NOTE
            )

    # -- the dev_node key, deleted in favour of the two-key form -------------
    for i, proj in enumerate(projects):
        if "dev_node" in proj:
            raise WorkspaceError(
                f"projects[{i}] ('{proj['name']}'): the 'dev_node' key is "
                f"deleted. A dev node is not one flag: it is a member that "
                f"declares WHAT IT IS (dev-only) and WHERE IT SITS (outside "
                f"every releasable), and rlsbl derives 'dev node' from the "
                f"two. Replace the `dev_node = ...` line with both of:\n"
                f"\n"
                f"  dev_only = true\n"
                f"  releasable = false\n"
                f"\n"
                f"If this member is not actually dev-only, give it "
                f"`releasable = \"<name>\"` instead. " + _MIGRATION_NOTE
            )

    # -- any other unknown member key ---------------------------------------
    for i, proj in enumerate(projects):
        _refuse_unknown_keys(
            proj.to_dict() if isinstance(proj, WorkspaceProject) else proj,
            MEMBER_KEYS,
            surface=f"projects[{i}] ('{proj['name']}'), a member table",
            file_label=WORKSPACE_FILE,
        )

    # -- unknown keys on a releasable table ----------------------------------
    if isinstance(raw_releasables, list):
        for i, raw in enumerate(raw_releasables):
            if isinstance(raw, dict):
                _refuse_releasable_unknown_keys(i, raw)

    # -- (b)/(c) the reserved name ------------------------------------------
    for i, proj in enumerate(projects):
        if proj["path"] == ROOT_MEMBER_PATH:
            if proj["name"] != ROOT_MEMBER_NAME:
                raise WorkspaceError(
                    f"projects[{i}]: the root member (path = \".\") is named "
                    f"'{proj['name']}', but '{ROOT_MEMBER_NAME}' is the only "
                    f"legal name for it. Job keys, router filters and check "
                    f"regexes are all derived from that name, so its spelling "
                    f"cannot vary from repository to repository. Set "
                    f"`name = \"{ROOT_MEMBER_NAME}\"` (or omit `name` "
                    f"entirely -- it is applied automatically). "
                    + _MIGRATION_NOTE
                )
        elif proj["name"] == ROOT_MEMBER_NAME:
            raise WorkspaceError(
                f"projects[{i}]: the member at path '{proj['path']}' is named "
                f"'{ROOT_MEMBER_NAME}', which is reserved for the member that "
                f"owns the repository root (path = \".\"). A workspace has "
                f"exactly one root member, and this workspace already declares "
                f"one, so this member cannot become a second: either rename "
                f"this member, or give it path = \".\" replacing the root "
                f"member declared today. Which one it should be is your "
                f"decision -- rlsbl will not guess which member owns the "
                f"repository root."
            )

    # -- (f) a root-member releasable needs an explicit tag format -----------
    root_releasable = _get_releasable_value(root_member)
    if isinstance(root_releasable, str) and isinstance(raw_releasables, list):
        for i, raw in enumerate(raw_releasables):
            if not isinstance(raw, dict) or raw.get("name") != root_releasable:
                continue
            if "tag_format" not in raw:
                raise WorkspaceError(
                    f"releasables[{i}] ('{root_releasable}') owns the root "
                    f"member but declares no tag_format. A releasable that "
                    f"owns the repository root must never inherit the default "
                    f"'{DEFAULT_TAG_FORMAT}' by accident: a repository root's "
                    f"releases are commonly tagged like a standalone repo's "
                    f"('{STANDALONE_TAG_FORMAT}') and its existing tags decide "
                    f"which. Declare it explicitly -- "
                    f"`tag_format = \"{STANDALONE_TAG_FORMAT}\"` for bare "
                    f"version tags, or "
                    f"`tag_format = \"{DEFAULT_TAG_FORMAT}\"` to keep the "
                    f"workspace scheme. Only you can say which scheme this "
                    f"repository's history already uses."
                )


def load_releasables(root, projects=None):
    """Load releasable definitions from workspace.toml.

    Reads and validates the ``[[releasables]]`` section, then validates that
    every releasable project has a valid ``releasable`` field referencing a
    defined releasable name (or ``false``).

    Args:
        root: path to the monorepo root (containing .rlsbl-monorepo/).
        projects: optional pre-loaded project list. If None, loads via
            load_workspace(root).

    Returns:
        A list of Releasable instances.

    Raises:
        WorkspaceError if ``[[releasables]]`` is missing, or on invalid
        releasable definitions or missing/invalid project releasable fields.
    """
    if projects is None:
        projects = load_workspace(root)

    path = os.path.join(root, WORKSPACE_DIR, WORKSPACE_FILE)
    with open(path, "rb") as f:
        data = tomllib.load(f)

    raw_releasables = data.get("releasables")

    if raw_releasables is None:
        raise WorkspaceError("[[releasables]] section required in workspace.toml")

    return _load_explicit_releasables(raw_releasables, projects)


def _load_explicit_releasables(raw_releasables, projects):
    """Parse [[releasables]] section and validate project membership.

    Every releasable project must have a ``releasable`` field that is either
    a string referencing a defined releasable name, or ``false``.
    """
    if not isinstance(raw_releasables, list):
        raise WorkspaceError("'releasables' must be a list of tables ([[releasables]])")

    releasables = []
    seen_names = set()

    for i, raw in enumerate(raw_releasables):
        if not isinstance(raw, dict):
            raise WorkspaceError(
                f"releasables[{i}] must be a table, got {type(raw).__name__}"
            )
        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise WorkspaceError(
                f"releasables[{i}] missing required 'name' string"
            )
        if name in seen_names:
            raise WorkspaceError(f"duplicate releasable name: '{name}'")
        seen_names.add(name)

        # Also refused by validate_workspace_model when the workspace is read
        # through load_workspace; repeated here because a caller may hand this
        # function a project list it loaded earlier, and every read of the
        # releasables section is a policed read.
        _refuse_releasable_unknown_keys(i, raw)

        # Absence is carried, not folded into the default: a releasable that
        # owns the repository root must DECLARE its format, and save_workspace
        # writes the key back only for the ones that stated it. Both questions
        # become unanswerable the moment absence is resolved here.
        tag_format = raw.get("tag_format", TAG_FORMAT_ABSENT)
        if tag_format is not TAG_FORMAT_ABSENT and not isinstance(tag_format, str):
            raise WorkspaceError(
                f"releasables[{i}] ('{{name}}'): tag_format must be a string"
                .format(name=name)
            )
        # The mirror's destination. Absence is the empty string rather than a
        # carried None: unlike tag_format, nothing downstream has to tell "not
        # declared" from "declared empty" -- a releasable is mirrored or it is
        # not, and an empty URL is not a mirror.
        subtree_remote = raw.get("subtree_remote", "")
        if not isinstance(subtree_remote, str):
            raise WorkspaceError(
                f"releasables[{i}] ('{name}'): subtree_remote must be a "
                f"string (the mirror repository's git URL)"
            )
        releasables.append(Releasable(
            name=name, tag_format=tag_format, subtree_remote=subtree_remote,
        ))

    # A mirror mirrors ONE subtree. A releasable with several members has no
    # single subtree to split, so the binding cannot be honoured and is refused
    # rather than silently applied to whichever member came first.
    for rel in releasables:
        if not rel.is_mirrored:
            continue
        member_names = [p["name"] for p in members_of(rel.name, projects)]
        if len(member_names) > 1:
            raise WorkspaceError(
                f"releasable '{rel.name}' declares a subtree_remote but has "
                f"{len(member_names)} members ({', '.join(member_names)}). A "
                f"mirror is the standalone repository ONE subtree is split "
                f"into, so a mirrored releasable has exactly one member -- "
                f"there is no single subtree to mirror otherwise. Either drop "
                f"the subtree_remote line, or split this releasable so the "
                f"mirrored member owns a releasable of its own."
            )

    # Validate project membership: every releasable project must declare releasable.
    defined_names = {r.name for r in releasables}
    for proj in projects:
        if not proj.is_releasable:
            continue
        val = proj.releasable
        if val is None:
            raise WorkspaceError(
                f"project '{proj.name}' missing required 'releasable' field "
                f"(explicit mode: [[releasables]] is defined, so every "
                f"releasable project must set releasable = \"<name>\" or "
                f"releasable = false)"
            )
        if isinstance(val, str) and val not in defined_names:
            raise WorkspaceError(
                f"project '{proj.name}': releasable = \"{val}\" does not "
                f"match any defined releasable (available: "
                f"{sorted(defined_names)})"
            )

    return releasables


def members_of(releasable_name, projects):
    """Return the list of projects that belong to a given releasable.

    Projects with ``releasable = "<name>"`` matching the given name are
    returned as members.

    Args:
        releasable_name: the releasable name to look up.
        projects: list of WorkspaceProject or dict instances.

    Returns:
        List of projects that are members of the releasable.
    """
    result = []
    for proj in projects:
        val = _get_releasable_value(proj)
        if isinstance(val, str) and val == releasable_name:
            result.append(proj)
    return result


def resolve_releasable_for_project(proj, releasables):
    """Return the Releasable that a project belongs to, or None.

    Looks up the project's ``releasable`` field and matches it against the
    list of releasables.

    Args:
        proj: WorkspaceProject or dict with at least ``name`` and optionally
            ``releasable``.
        releasables: list of Releasable instances.

    Returns:
        The matching Releasable, or None if the project is not releasable
        (``releasable = false``) or no match is found.
    """
    val = _get_releasable_value(proj)
    if val is False or not isinstance(val, str):
        return None
    for rel in releasables:
        if rel.name == val:
            return rel
    return None


def mirror_remote_for(proj, releasables) -> str:
    """The mirror destination a project's releasable declares, or ``""``.

    The ONE resolution of "is this member mirrored, and where": the binding
    lives on the releasable, so every reader that used to read a member's own
    ``subtree_remote`` asks this instead. A member outside every releasable, or
    one whose releasable declares no mirror, answers the empty string.
    """
    rel = resolve_releasable_for_project(proj, releasables)
    if rel is None:
        return ""
    return rel.subtree_remote


def mirrored_releasable_for(proj, releasables):
    """The mirrored :class:`Releasable` *proj* belongs to, or None."""
    rel = resolve_releasable_for_project(proj, releasables)
    if rel is None or not rel.is_mirrored:
        return None
    return rel


def _get_releasable_value(proj):
    """Extract the releasable value from a project (WorkspaceProject or dict).

    Returns str, False, or None. Does not validate -- just reads the raw value.
    """
    if isinstance(proj, WorkspaceProject):
        return proj.releasable
    # Raw dict
    val = proj.get("releasable")
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, bool) and val is False:
        return False
    return val


def _build_project_table(d):
    """Build a fresh tomlkit table for a project dict.

    Key order: ``path``, ``name``, then all remaining keys sorted. This
    matches the layout used when scaffolding a brand-new workspace.toml.
    """
    table = tomlkit.table()
    table.add("path", d["path"])
    table.add("name", d["name"])
    for key in sorted(d.keys()):
        if key not in ("path", "name"):
            table.add(key, d[key])
    return table


def _build_releasable_table(d):
    """Build a fresh tomlkit table for a releasable desired-dict.

    ``d`` carries ``name`` and (optionally) ``tag_format`` and
    ``subtree_remote``.
    """
    table = tomlkit.table()
    table.add("name", d["name"])
    if "tag_format" in d:
        table.add("tag_format", d["tag_format"])
    if "subtree_remote" in d:
        table.add("subtree_remote", d["subtree_remote"])
    return table


def _update_table_fields(table, desired):
    """Update a tomlkit table in place to match ``desired`` (a plain dict).

    - Existing keys are reassigned only when their value actually changed
      (so untouched keys keep their original formatting and inline comments).
    - New keys are appended (preserving the order of already-present keys).
    - Keys absent from ``desired`` are removed.

    Intra-table comments attached to surviving keys are preserved by tomlkit.
    """
    for key, value in desired.items():
        if key in table:
            if table[key] != value:
                table[key] = value
        else:
            table.add(key, value)
    for key in list(table.keys()):
        if key not in desired:
            del table[key]


def _sync_aot_in_place(aot, desired_list, id_key, build_fn):
    """Reconcile an existing tomlkit array-of-tables with a desired list.

    Items are matched by identity (``id_key``: ``path`` for projects,
    ``name`` for releasables). Matched tables are updated field-by-field in
    place (preserving comments and key order). Tables whose identity is not
    in ``desired_list`` are removed. Desired items with no matching table are
    appended (in desired order) as fresh tables with a leading blank line so
    they read like the surrounding array-of-tables.
    """
    desired_by_id = {d[id_key]: d for d in desired_list}

    # Remove tables whose identity is no longer desired (iterate back-to-front
    # so index deletion stays valid).
    for idx in range(len(aot) - 1, -1, -1):
        if aot[idx][id_key] not in desired_by_id:
            del aot[idx]

    # Update the survivors in place.
    present_ids = set()
    for item in aot:
        ident = item[id_key]
        present_ids.add(ident)
        _update_table_fields(item, desired_by_id[ident])

    # Append newcomers in desired order.
    for d in desired_list:
        if d[id_key] not in present_ids:
            table = build_fn(d)
            table.trivia.indent = "\n"
            aot.append(table)


def _separate_releasables_from_projects(doc):
    """Keep a blank line between the two sections when one follows the other.

    An appended array-of-tables entry carries a blank line BEFORE itself
    (``trivia.indent``), which separates it from its own siblings but not from
    whatever section comes next: a newcomer appended as the last
    ``[[releasables]]`` entry otherwise butts straight against the
    ``[[projects]]`` header. tomlkit holds the blank line AFTER a table as a
    trailing whitespace element inside that table, which is how a file that
    already reads well is recognized and left byte-for-byte alone -- a no-op
    save must not perturb a single byte.
    """
    from tomlkit.items import AoT, Whitespace

    keys = list(doc.keys())
    if "releasables" not in keys or "projects" not in keys:
        return
    if keys.index("releasables") > keys.index("projects"):
        return
    rels = doc.get("releasables")
    projs = doc.get("projects")
    if not isinstance(rels, AoT) or len(rels) == 0:
        return
    if not isinstance(projs, AoT) or len(projs) == 0:
        return
    last_table = rels[len(rels) - 1]
    body = last_table.value.body
    if not body:
        return
    trailing = body[-1][1]
    if isinstance(trailing, Whitespace) and "\n" in trailing.value:
        return
    last_table.value.add(tomlkit.ws("\n"))


def _member_to_write(proj):
    """The member dict ``save_workspace`` serializes, minus runtime bookkeeping.

    Two things a load->save cycle must never do: persist a key a caller hung
    off the member at runtime (``monorepo sync`` attaches its inlined-CI
    bookkeeping to the member dicts it walks), and write a key the loader would
    then refuse. The first is stripped -- those keys are the tools' own and
    were never part of the file -- and the second is a hard error here rather
    than an unreadable workspace.toml discovered on the next load.
    """
    data = proj.to_dict() if isinstance(proj, WorkspaceProject) else dict(proj)
    data = {k: v for k, v in data.items() if not is_runtime_member_key(k)}
    _refuse_unknown_keys(
        data, MEMBER_KEYS,
        surface=f"the member '{data.get('name')}' being written",
        file_label=WORKSPACE_FILE,
    )
    return data


def save_workspace(root, projects, releasables=None):
    """Write workspace.toml atomically, editing the existing document in place.

    When the file already exists it is parsed with tomlkit and the
    ``[[projects]]`` (and, when requested, ``[[releasables]]``) arrays-of-tables
    are reconciled surgically: matched items (by ``path`` for projects and
    ``name`` for releasables) are updated field-by-field, absent items are
    removed, and new items are appended. Untouched tables, intra-table
    comments, key order, and every other top-level section are preserved
    byte-for-byte. When the file does not yet exist, a fresh document is
    created.

    When ``releasables`` is passed (a list of Releasable instances), the
    ``[[releasables]]`` section is reconciled in place. When ``releasables`` is
    None, any existing ``[[releasables]]`` section is preserved untouched. Pass
    an empty list to write an explicitly empty section (``releasables = []``):
    a workspace with no releasables yet is still an explicit-mode workspace,
    and removing the section entirely would make it unreadable.

    Creates .rlsbl-monorepo/ directory if it doesn't exist.
    """
    from tomlkit.items import AoT

    ws_dir = os.path.join(root, WORKSPACE_DIR)
    effects.makedirs(ws_dir, exist_ok=True)

    target = os.path.join(ws_dir, WORKSPACE_FILE)

    if os.path.isfile(target):
        with open(target, encoding="utf-8") as f:
            doc = tomlkit.loads(f.read())
    else:
        doc = tomlkit.document()

    # --- releasables section ---
    if releasables is not None:
        # ``tag_format`` round-trips on the value's own terms: a releasable
        # that declared one keeps it (even when it equals the default, which
        # is an operator-written line and not ours to delete), and one that
        # declared none stays without the key. The Releasable itself carries
        # that distinction (:data:`rlsbl.workspace_types.TAG_FORMAT_ABSENT`),
        # so nothing here has to re-read the file to guess which it was.
        existing_rels = doc.get("releasables")

        # A releasable table carries the model's keys and nothing else. A key
        # outside the model is refused at load, so there is none to carry
        # across -- writing one back would produce a file rlsbl then refuses
        # to read.
        desired_rels = []
        for rel in releasables:
            d = {"name": rel.name}
            if rel.declares_tag_format:
                d["tag_format"] = rel.tag_format
            if rel.is_mirrored:
                d["subtree_remote"] = rel.subtree_remote
            desired_rels.append(d)

        if not desired_rels:
            # An empty list means "no releasables yet", NOT "no section":
            # deleting the section would leave a workspace with no
            # [[releasables]], which the loader refuses. Empty
            # arrays-of-tables produce no
            # output in tomlkit, so write the explicit empty inline array.
            if "releasables" in doc:
                del doc["releasables"]
            doc.add("releasables", tomlkit.array())
        else:
            if isinstance(existing_rels, AoT) and len(existing_rels) > 0:
                _sync_aot_in_place(
                    existing_rels, desired_rels, "name", _build_releasable_table
                )
            else:
                if "releasables" in doc:
                    del doc["releasables"]
                raot = tomlkit.aot()
                for d in desired_rels:
                    raot.append(_build_releasable_table(d))
                doc.add("releasables", raot)
    elif "releasables" not in doc:
        # releasables=None preserves an existing section -- but a document
        # that has none is a workspace the loader refuses.
        # Writing a file rlsbl cannot read back is never the right outcome, so
        # a brand-new document gets the explicit empty section.
        doc.add("releasables", tomlkit.array())

    # --- projects section ---
    desired_projs = [_member_to_write(proj) for proj in projects]
    if not desired_projs:
        # Empty AoT produces no output in tomlkit; use inline array instead.
        if "projects" in doc:
            del doc["projects"]
        doc.add("projects", tomlkit.array())
    else:
        existing = doc.get("projects")
        if isinstance(existing, AoT) and len(existing) > 0:
            _sync_aot_in_place(
                existing, desired_projs, "path", _build_project_table
            )
        else:
            if "projects" in doc:
                del doc["projects"]
            aot = tomlkit.aot()
            for d in desired_projs:
                aot.append(_build_project_table(d))
            doc.add("projects", aot)

    _separate_releasables_from_projects(doc)

    effects.atomic_write_text(target, tomlkit.dumps(doc))


def resolve_project(root, cwd="."):
    """Determine which project cwd is inside, returning a WorkspaceProject or None.

    Uses the one path rule (:func:`rlsbl.ownership.member_for_directory`): the
    most specific declared member path wins, and the root member answers for
    every directory no other member claims -- including the repository root
    itself, which is exactly what a member declared at ``path = "."`` always
    matched.

    ``None`` only when *cwd* is outside the workspace tree entirely. Inside a
    workspace, some member always answers, because a workspace always declares
    a root member.
    """
    from .ownership import member_for_directory

    abs_root = os.path.realpath(root)
    abs_cwd = os.path.realpath(cwd)

    if abs_cwd != abs_root and not abs_cwd.startswith(abs_root + os.sep):
        return None
    relative = os.path.relpath(abs_cwd, abs_root)

    return member_for_directory(
        relative, load_workspace(root), include_root=True,
    )


# ---------------------------------------------------------------------------
# Standalone (single-project) releasable
# ---------------------------------------------------------------------------

STANDALONE_RELEASABLE_FILE = "releasable.toml"

#: Every key ``.rlsbl/releasable.toml`` may carry. A standalone repository's
#: releasable declares who it is and how its tags are spelled; it has no
#: mirror (``subtree_remote`` is a monorepo releasable's key -- the standalone
#: repository IS the thing a mirror would be), so the set is narrower than
#: :func:`releasable_keys`.
STANDALONE_RELEASABLE_KEYS = frozenset({"name", "tag_format"})


def _derive_standalone_name(project_root, detected_targets=None, targets_map=None):
    """Derive a project name for the standalone releasable.

    Tries target read_name (first detected target), then falls back to
    the directory basename.

    Args:
        project_root: path to the project root (str or Path).
        detected_targets: pre-detected list of TargetEntry instances.
        targets_map: dict mapping target names to target objects.

    Returns:
        A non-empty name string.
    """
    project_root = str(project_root)
    if detected_targets is not None and targets_map is not None:
        try:
            if detected_targets:
                target_obj = targets_map.get(detected_targets[0].name)
                if target_obj is not None:
                    name = target_obj.read_name(detected_targets[0].path, None)
                    if name:
                        return name
        except Exception:
            pass
    return os.path.basename(os.path.realpath(project_root)) or "project"


def load_standalone_releasable(project_root):
    """Load an explicit releasable definition from .rlsbl/releasable.toml.

    If the file exists, reads ``name`` and ``tag_format`` from it.
    If absent, returns None (caller should use create_standalone_releasable).

    Args:
        project_root: path to the project root (str or Path).

    Returns:
        A Releasable instance, or None if the file does not exist.

    Raises:
        WorkspaceError on invalid file contents.
    """
    path = os.path.join(str(project_root), ".rlsbl", STANDALONE_RELEASABLE_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        data = tomllib.load(f)
    file_label = f".rlsbl/{STANDALONE_RELEASABLE_FILE}"
    _refuse_unknown_keys(
        data, STANDALONE_RELEASABLE_KEYS,
        surface="the standalone releasable", file_label=file_label,
    )
    name = data.get("name")
    if not name or not isinstance(name, str):
        raise WorkspaceError(
            f"{file_label}: missing or invalid 'name' "
            f"(must be a non-empty string)"
        )
    # Absence is carried, exactly as it is for a workspace releasable: this
    # function answers "what does the file SAY", and a file that states no
    # format is distinct from one that states the standalone default.
    # Resolving absence to a usable format is
    # :func:`create_standalone_releasable`'s job.
    tag_format = data.get("tag_format", TAG_FORMAT_ABSENT)
    if tag_format is not TAG_FORMAT_ABSENT and not isinstance(tag_format, str):
        raise WorkspaceError(f"{file_label}: tag_format must be a string")
    return Releasable(name=name, tag_format=tag_format)


def save_standalone_releasable(project_root, releasable):
    """Write ``.rlsbl/releasable.toml`` for *releasable*.

    ``tag_format`` is written only when the releasable declares one, so a load
    -> save cycle neither invents the key on a file that stated none nor drops
    it from a file that did.
    """
    path = os.path.join(str(project_root), ".rlsbl", STANDALONE_RELEASABLE_FILE)
    effects.makedirs(os.path.dirname(path), exist_ok=True)
    text = f'name = "{releasable.name}"\n'
    if releasable.declares_tag_format:
        text += f'tag_format = "{releasable.tag_format}"\n'
    effects.write_text(path, text)


def create_standalone_releasable(project_root):
    """Return a Releasable representing a single-project repo.

    If ``.rlsbl/releasable.toml`` exists, uses its explicit configuration.
    Otherwise, derives the name from the project's target metadata (e.g.,
    ``pyproject.toml [project].name``) or the directory basename, and uses
    the standalone tag format (``v{version}``).

    This function does NOT create any files on disk -- the releasable is
    purely an internal abstraction.

    Args:
        project_root: path to the project root (str or Path).

    Returns:
        A Releasable instance.
    """
    explicit = load_standalone_releasable(project_root)
    if explicit is not None:
        if explicit.declares_tag_format:
            return explicit
        # The file carried no format. In a standalone repository the answer is
        # the standalone scheme -- decided by the context, not invented at read
        # time, and never the workspace scheme Releasable.effective_tag_format
        # would otherwise resolve absence to.
        return dataclasses.replace(explicit, tag_format=STANDALONE_TAG_FORMAT)
    # Lazy import: targets detection is only needed when no explicit
    # releasable.toml exists. The import happens here (in the caller)
    # rather than in _derive_standalone_name to keep that function
    # free of targets imports and break the workspace->targets edge.
    try:
        from .targets import detect_targets, TARGETS
        detected = detect_targets(str(project_root))
        targets_map = TARGETS
    except Exception:
        detected = None
        targets_map = None
    name = _derive_standalone_name(project_root, detected_targets=detected, targets_map=targets_map)
    return Releasable(name=name, tag_format=STANDALONE_TAG_FORMAT)
