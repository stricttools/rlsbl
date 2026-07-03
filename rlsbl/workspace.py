"""Workspace data layer for monorepo support handling discovery, loading, saving, and resolution of workspaces from workspace.toml config."""

import os
import tempfile
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
    STANDALONE_TAG_FORMAT,
    Releasable,
    WorkspaceProject,
    get_releasable_dir,
    get_releasable_changes_dir,
    get_releasable_version_path,
    get_releasable_hook_path,
    project_is_dev_only,
    project_is_releasable,
)


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
    os.makedirs(target_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".version.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(version + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up the temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def is_explicit_mode(workspace_root):
    """Check whether the workspace has a ``[[releasables]]`` section.

    Returns True when ``[[releasables]]`` is present in workspace.toml,
    False otherwise.

    Args:
        workspace_root: path to the monorepo root.

    Returns:
        bool
    """
    path = os.path.join(workspace_root, WORKSPACE_DIR, WORKSPACE_FILE)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return False
    return data.get("releasables") is not None


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
        # Normalize: strip trailing slashes so stored paths are consistent.
        # Belt-and-suspenders with target-level tag format defenses.
        entry["path"] = entry["path"].rstrip("/")
        if "name" not in entry or not entry["name"]:
            entry["name"] = os.path.basename(entry["path"])
        result.append(WorkspaceProject(entry))

    return result


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

        tag_format = raw.get("tag_format", DEFAULT_TAG_FORMAT)
        if not isinstance(tag_format, str):
            raise WorkspaceError(
                f"releasables[{i}] ('{{name}}'): tag_format must be a string"
                .format(name=name)
            )
        releasables.append(Releasable(name=name, tag_format=tag_format))

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


def save_workspace(root, projects, releasables=None):
    """Write workspace.toml atomically using tomlkit for clean TOML output.

    Preserves top-level sections, comments, and formatting from the existing
    file by reading it with tomlkit first and modifying the ``[[projects]]``
    array in-place.  Falls back to creating a new document when the file does
    not yet exist.

    When ``releasables`` is passed (a list of Releasable instances), the
    ``[[releasables]]`` section is replaced.  When ``releasables`` is None,
    any existing ``[[releasables]]`` section is preserved as-is.  Pass an
    empty list to explicitly remove the section.

    Creates .rlsbl-monorepo/ directory if it doesn't exist.
    """
    ws_dir = os.path.join(root, WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)

    target = os.path.join(ws_dir, WORKSPACE_FILE)

    # Read existing document to preserve non-project sections/comments.
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as f:
            doc = tomlkit.loads(f.read())
        # Remove old projects key so we can replace it.
        if "projects" in doc:
            del doc["projects"]
    else:
        doc = tomlkit.document()

    # Handle releasables section.
    if releasables is not None:
        if "releasables" in doc:
            del doc["releasables"]
        if releasables:
            raot = tomlkit.aot()
            for rel in releasables:
                table = tomlkit.table()
                table.add("name", rel.name)
                if rel.tag_format != DEFAULT_TAG_FORMAT:
                    table.add("tag_format", rel.tag_format)
                raot.append(table)
            doc.add("releasables", raot)
    # When releasables is None, the existing section (if any) is preserved.

    if not projects:
        # Empty AoT produces no output in tomlkit; use inline array instead
        doc.add("projects", tomlkit.array())
    else:
        aot = tomlkit.aot()
        for proj in projects:
            d = proj.to_dict() if isinstance(proj, WorkspaceProject) else proj
            table = tomlkit.table()
            table.add("path", d["path"])
            table.add("name", d["name"])
            for key in sorted(d.keys()):
                if key not in ("path", "name"):
                    table.add(key, d[key])
            aot.append(table)
        doc.add("projects", aot)

    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
    os.replace(tmp, target)


def resolve_project(root, cwd="."):
    """Determine which project cwd is inside, returning a WorkspaceProject or None.

    If multiple projects match (nested paths), returns the most specific one.
    """
    abs_root = os.path.realpath(root)
    abs_cwd = os.path.realpath(cwd)

    projects = load_workspace(root)

    best_match = None
    best_len = -1
    for proj in projects:
        proj_abs = os.path.realpath(os.path.join(abs_root, proj["path"]))
        # cwd must be the project dir or a subdirectory of it
        if abs_cwd == proj_abs or abs_cwd.startswith(proj_abs + os.sep):
            if len(proj_abs) > best_len:
                best_match = proj
                best_len = len(proj_abs)

    return best_match


# ---------------------------------------------------------------------------
# Standalone (single-project) releasable
# ---------------------------------------------------------------------------

STANDALONE_RELEASABLE_FILE = "releasable.toml"


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
    name = data.get("name")
    if not name or not isinstance(name, str):
        raise WorkspaceError(
            f".rlsbl/{STANDALONE_RELEASABLE_FILE}: missing or invalid 'name' "
            f"(must be a non-empty string)"
        )
    tag_format = data.get("tag_format", STANDALONE_TAG_FORMAT)
    if not isinstance(tag_format, str):
        raise WorkspaceError(
            f".rlsbl/{STANDALONE_RELEASABLE_FILE}: tag_format must be a string"
        )
    return Releasable(name=name, tag_format=tag_format)


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
        return explicit
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
