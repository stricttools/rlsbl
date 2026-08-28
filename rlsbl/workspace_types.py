"""Core workspace types and path-building utilities defining WorkspaceProject, Releasable, and directory resolution for monorepo workspace operations.

This module contains the fundamental types (WorkspaceProject, Releasable)
and pure path-building functions (get_releasable_dir, etc.) that are used
across the codebase. It is intentionally free of imports from targets,
workspace, context, or checks to break circular dependency cycles.

The workspace module re-exports everything from here so existing importers
are unaffected.
"""

import os
from dataclasses import dataclass, field

from .errors import WorkspaceError


WORKSPACE_DIR = ".rlsbl-monorepo"
WORKSPACE_FILE = "workspace.toml"
RELEASABLES_DIR = "releasables"

DEFAULT_TAG_FORMAT = "{name}@v{version}"
STANDALONE_TAG_FORMAT = "v{version}"


# ---------------------------------------------------------------------------
# Per-releasable directory structure
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


def get_releasable_changes_dir(workspace_root, releasable_name):
    """Return the path to a releasable's changelog changes directory.

    Path: ``<workspace_root>/.rlsbl-monorepo/releasables/<name>/changes/``

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.

    Returns:
        Absolute path string to the changes directory.
    """
    return os.path.join(get_releasable_dir(workspace_root, releasable_name), "changes")


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


def get_releasable_hook_path(workspace_root, releasable_name, hook_name):
    """Return the absolute path to a releasable-level hook script.

    Path: ``<workspace_root>/.rlsbl-monorepo/releasables/<name>/hooks/<hook_name>``

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.
        hook_name: hook file name (e.g. ``"pre-checks.sh"``).

    Returns:
        Absolute path string. The file may or may not exist on disk.
    """
    return os.path.join(get_releasable_dir(str(workspace_root), releasable_name), "hooks", hook_name)


#: What ``tag_format`` holds when a releasable declared none.
#:
#: Absence is a real state, distinct from declaring :data:`DEFAULT_TAG_FORMAT`
#: on purpose.  A releasable that owns the repository root must declare its
#: format (the loader refuses one that does not), and ``save_workspace`` writes
#: the key only for the releasables that actually stated it -- neither is
#: decidable if absence has already been folded into the default at load time.
TAG_FORMAT_ABSENT = None


@dataclass
class Releasable:
    """A named unit of versioning: a group of packages sharing version, changelog, and release.

    Releasables are defined via ``[[releasables]]`` in workspace.toml.

    ``tag_format`` is the DECLARED format and may be
    :data:`TAG_FORMAT_ABSENT`.  Anything that needs a format to work with reads
    :attr:`effective_tag_format`, which resolves absence to
    :data:`DEFAULT_TAG_FORMAT`.  The split is the point: the declared value
    answers "what does workspace.toml say", the effective value answers "what
    do this releasable's tags look like", and only the first can tell a
    workspace that meant the default from one that never thought about it.
    """

    name: str
    tag_format: str | None = field(default=TAG_FORMAT_ABSENT)
    subtree_remote: str = ""

    def __post_init__(self):
        if not self.name:
            raise WorkspaceError("releasable name must be a non-empty string")

    @property
    def declares_tag_format(self) -> bool:
        """Did workspace.toml state a ``tag_format`` for this releasable?"""
        return self.tag_format is not TAG_FORMAT_ABSENT

    @property
    def is_mirrored(self) -> bool:
        """Is this releasable bound to a standalone subtree mirror?

        The binding is the mirror's DESTINATION, and it belongs to the
        releasable rather than to a member package: a mirror carries one
        subtree's whole history, its tags and its GitHub Releases, and the unit
        that owns a version, a changelog and a tag scheme is the releasable. A
        releasable with more than one member therefore cannot declare one at
        all -- the loader refuses that outright, because there would be no one
        subtree to mirror.
        """
        return bool(self.subtree_remote)

    @property
    def effective_tag_format(self) -> str:
        """The format this releasable's tags actually use.

        :data:`DEFAULT_TAG_FORMAT` when none was declared -- the workspace
        scheme, which is right for every releasable except one that owns the
        repository root, and the loader refuses that case outright rather than
        letting it reach here.
        """
        if self.tag_format is TAG_FORMAT_ABSENT:
            return DEFAULT_TAG_FORMAT
        return self.tag_format


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
    def library(self) -> bool:
        return bool(self._data.get("library", False))

    @property
    def dev_only(self) -> bool:
        return bool(self._data.get("dev_only", False) or self._data.get("dev_node", False))

    @property
    def dev_node(self) -> bool:
        """Derived shorthand: True when dev_only and not a member of any releasable.

        A project is considered non-releasable when ``releasable`` is explicitly
        ``False``, OR when ``releasable`` is ``None`` and the legacy
        ``dev_node`` flag is set.
        """
        if not self.dev_only:
            return False
        rel = self._data.get("releasable")
        if isinstance(rel, str):
            # Explicitly assigned to a releasable -- not a dev_node
            return False
        if rel is False:
            return True
        # rel is None: legacy dev_node semantics apply
        return bool(self._data.get("dev_node", False))

    @property
    def is_releasable(self) -> bool:
        """Whether this project can produce releases.

        A project is releasable when it belongs to some releasable unit
        (``releasable = "name"``). Returns False when ``releasable = false``
        is set explicitly, or when the project is a dev_node.
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
            None: field not set.
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
    def import_name(self) -> str:
        return self._data.get("import_name", "")

    @property
    def registry_name(self) -> str:
        return self._data.get("registry_name", "")

    # There is no ``subtree_remote`` here. The mirror's destination is a
    # RELEASABLE-level key (:attr:`Releasable.subtree_remote`); a member
    # carrying it is refused at load time. Ask
    # :func:`rlsbl.workspace.mirror_remote_for` for a member's mirror.

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
