"""Workspace data layer for monorepo support handling discovery, loading, saving, and resolution of workspaces from workspace.toml config."""

import os
import tempfile
import tomllib
from dataclasses import dataclass, field

import tomlkit

from .errors import WorkspaceError


WORKSPACE_DIR = ".rlsbl-monorepo"
WORKSPACE_FILE = "workspace.toml"
RELEASABLES_DIR = "releasables"

DEFAULT_TAG_FORMAT = "{name}@v{version}"


# ---------------------------------------------------------------------------
# Per-releasable directory structure and version management
# ---------------------------------------------------------------------------


def get_releasable_dir(workspace_root, releasable_name):
    """Return the directory path for a releasable's state files.

    Path: ``<workspace_root>/.rlsbl-monorepo/releasables/<name>/``

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.

    Returns:
        Absolute path string to the releasable directory.
    """
    return os.path.join(workspace_root, WORKSPACE_DIR, RELEASABLES_DIR, releasable_name)


def get_releasable_version_path(workspace_root, releasable_name):
    """Return the path to a releasable's version file.

    Path: ``<workspace_root>/.rlsbl-monorepo/releasables/<name>/version``

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.

    Returns:
        Absolute path string to the version file.
    """
    return os.path.join(get_releasable_dir(workspace_root, releasable_name), "version")


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
    """Check whether the workspace uses explicit releasable definitions.

    Returns True when ``[[releasables]]`` is present in workspace.toml,
    False otherwise (implicit mode where each project is its own releasable).

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


@dataclass
class Releasable:
    """A named unit of versioning: a group of packages sharing version, changelog, and release.

    In explicit mode, releasables are defined via ``[[releasables]]`` in
    workspace.toml.  In implicit mode (no ``[[releasables]]`` section),
    each releasable project is its own single-member releasable.
    """

    name: str
    tag_format: str = field(default=DEFAULT_TAG_FORMAT)

    def __post_init__(self):
        if not self.name:
            raise WorkspaceError("releasable name must be a non-empty string")


class WorkspaceProject:
    """Typed wrapper over a workspace.toml project dict.

    Provides typed property access for known fields while preserving the
    underlying dict for round-trip serialization. Unknown fields are kept
    intact. Dict-like ``[]``, ``get()``, and ``in`` access is supported
    for backward compatibility with code that treats projects as dicts.
    """

    def __init__(self, data: dict):
        self._data = data

    @property
    def name(self) -> str:
        return self._data["name"]

    @property
    def path(self) -> str:
        return self._data["path"]

    @property
    def watch(self) -> list[str]:
        return self._data.get("watch", [])

    @property
    def library(self) -> bool:
        return bool(self._data.get("library", False))

    @property
    def dev_only(self) -> bool:
        return bool(self._data.get("dev_only", False) or self._data.get("dev_node", False))

    @property
    def dev_node(self) -> bool:
        """Derived shorthand: True when dev_only and not a member of any releasable.

        A project is considered non-releasable when ``releasable`` is explicitly
        ``False``, OR when ``releasable`` is ``None`` (implicit mode) and the
        legacy ``dev_node`` flag is set.  This preserves backward compatibility
        with workspaces that still use ``dev_node = true``.
        """
        if not self.dev_only:
            return False
        rel = self._data.get("releasable")
        if isinstance(rel, str):
            # Explicitly assigned to a releasable -- not a dev_node
            return False
        if rel is False:
            return True
        # rel is None (implicit mode): legacy dev_node semantics apply
        return bool(self._data.get("dev_node", False))

    @property
    def is_releasable(self) -> bool:
        """Whether this project can produce releases.

        A project is releasable when it belongs to some releasable unit
        (explicit ``releasable = "name"`` or implicit single-member mode).
        Returns False when ``releasable = false`` is set explicitly, or
        when the project is a legacy ``dev_node`` in implicit mode.
        """
        return not self.dev_node and self.releasable is not False

    @property
    def depends_on(self) -> list[str]:
        return self._data.get("depends_on", [])

    @property
    def releasable(self) -> "str | bool | None":
        """The releasable this project belongs to.

        Returns:
            str: name of the releasable group this project belongs to.
            False: project is explicitly unversioned (no releases).
            None: field not set (implicit mode -- project is its own releasable).
        """
        val = self._data.get("releasable")
        if val is None:
            return None
        if isinstance(val, str):
            return val
        if isinstance(val, bool):
            if val is True:
                raise WorkspaceError(
                    f"project '{self.name}': releasable = true is not valid; "
                    "use a string name or false"
                )
            return False
        raise WorkspaceError(
            f"project '{self.name}': releasable must be a string or false, "
            f"got {type(val).__name__}"
        )

    @property
    def registry_name(self) -> str:
        return self._data.get("registry_name", "")

    def get(self, key, default=None):
        """Dict-like access for backward compatibility."""
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def __contains__(self, key):
        return key in self._data

    def __eq__(self, other):
        if isinstance(other, WorkspaceProject):
            return self._data == other._data
        if isinstance(other, dict):
            return self._data == other
        return NotImplemented

    def __repr__(self):
        return f"WorkspaceProject({self._data!r})"

    def to_dict(self) -> dict:
        """Return the underlying dict for serialization."""
        return self._data


def project_is_dev_only(proj) -> bool:
    """Check if a project is dev_only (works with WorkspaceProject or dict)."""
    if isinstance(proj, WorkspaceProject):
        return proj.dev_only
    return bool(proj.get("dev_only", False) or proj.get("dev_node", False))


def project_is_releasable(proj) -> bool:
    """Check if a project is releasable (works with WorkspaceProject or dict)."""
    if isinstance(proj, WorkspaceProject):
        return proj.is_releasable
    # For raw dicts: mirror the WorkspaceProject logic
    if proj.get("dev_node", False):
        return False
    return proj.get("releasable") is not False


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

    In explicit mode (``[[releasables]]`` section present), reads and validates
    the section, then validates that every releasable project has a valid
    ``releasable`` field referencing a defined releasable name (or ``false``).

    In implicit mode (no ``[[releasables]]`` section), each releasable
    project becomes its own single-member releasable with the default tag
    format.

    Args:
        root: path to the monorepo root (containing .rlsbl-monorepo/).
        projects: optional pre-loaded project list. If None, loads via
            load_workspace(root).

    Returns:
        A list of Releasable instances.

    Raises:
        WorkspaceError on invalid releasable definitions or missing/invalid
        project releasable fields in explicit mode.
    """
    if projects is None:
        projects = load_workspace(root)

    path = os.path.join(root, WORKSPACE_DIR, WORKSPACE_FILE)
    with open(path, "rb") as f:
        data = tomllib.load(f)

    raw_releasables = data.get("releasables")

    if raw_releasables is not None:
        return _load_explicit_releasables(raw_releasables, projects)
    else:
        return _load_implicit_releasables(projects)


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


def _load_implicit_releasables(projects):
    """Generate implicit single-member releasables for projects without explicit config.

    Each releasable project becomes its own releasable with the default
    tag format.
    """
    releasables = []
    for proj in projects:
        if not proj.is_releasable:
            continue
        releasables.append(Releasable(name=proj.name))
    return releasables


def members_of(releasable_name, projects):
    """Return the list of projects that belong to a given releasable.

    In explicit mode, these are projects with ``releasable = "<name>"``.
    In implicit mode (no releasable field set), a project is a member of
    the releasable with its own name.

    Args:
        releasable_name: the releasable name to look up.
        projects: list of WorkspaceProject or dict instances.

    Returns:
        List of projects that are members of the releasable.
    """
    result = []
    for proj in projects:
        val = _get_releasable_value(proj)
        name = proj.name if isinstance(proj, WorkspaceProject) else proj["name"]
        if isinstance(val, str) and val == releasable_name:
            # Explicit membership
            result.append(proj)
        elif val is None and name == releasable_name:
            # Implicit mode: project is its own releasable
            result.append(proj)
    return result


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
