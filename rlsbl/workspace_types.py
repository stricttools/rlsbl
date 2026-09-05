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


#: Every key a ``[[projects]]`` member table may carry.
#:
#: This is the ONE declared authority for the member surface: the loader
#: refuses a member key outside it, ``save_workspace`` refuses to write one,
#: and :class:`WorkspaceProject` exposes exactly one accessor per key (the
#: suite binds both sides to this constant). A member table has no dataclass to
#: derive the set from -- the releasable surface derives its set from
#: :class:`Releasable`'s fields -- so it is declared here, as the stated interim
#: until the member surface gets a schema of its own and this is generated from
#: it.
#:
#: ``watch``, ``subtree_remote`` and ``dev_node`` are absent on purpose: each is
#: a retired key with its own refusal message naming its remedy.
MEMBER_KEYS = frozenset({
    "path",
    "name",
    "library",
    "dev_only",
    "releasable",
    "depends_on",
    "import_name",
    "registry_name",
    "description",
    "test_only",
    "lint_allow",
})


#: Keys a runtime caller may attach to a member structure that are NEVER
#: written back. ``monorepo sync`` hangs its inlined-CI bookkeeping off the
#: member dicts it is walking (``_ci_files``, ``_ci_docs``,
#: ``_root_publisher``); persisting one would produce a workspace.toml the
#: loader then refuses. The convention is the leading underscore, so a new
#: bookkeeping key needs no edit here.
def is_runtime_member_key(key) -> bool:
    """Is *key* runtime bookkeeping rather than a declared member key?"""
    return isinstance(key, str) and key.startswith("_")


class WorkspaceProject:
    """Typed wrapper over a workspace.toml project dict.

    Provides typed property access for the member keys in :data:`MEMBER_KEYS`
    -- one accessor per key, plus the derived ``dev_node`` and
    ``is_releasable``. The underlying dict is preserved for serialization.
    Dict-like ``[]``, ``get()``, and ``in`` access is supported for code that
    treats projects as dicts.
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
        return bool(self._data.get("dev_only", False))

    @property
    def dev_node(self) -> bool:
        """Derived shorthand: True when dev_only and outside every releasable.

        Both halves are required, and both are declared: ``dev_only = true``
        says what the member IS, ``releasable = false`` says it stands outside
        every releasable. There is no single key for the combination -- the
        ``dev_node`` key was deleted, and the loader refuses it by name.
        """
        if not self.dev_only:
            return False
        return self._data.get("releasable") is False

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

    @property
    def description(self) -> str:
        """Free-text description, carried into the monorepo snapshot."""
        return self._data.get("description", "")

    @property
    def test_only(self) -> bool:
        """Is this member test infrastructure? Carried into the snapshot."""
        return bool(self._data.get("test_only", False))

    @property
    def lint_allow(self) -> list[str]:
        """Imports the library-lint check allows for this member."""
        return self._data.get("lint_allow", [])

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
    return bool(proj.get("dev_only", False))


def project_is_dev_node(proj) -> bool:
    """Is *proj* a dev node (dev-only AND outside every releasable)?

    Works with :class:`WorkspaceProject` or a raw dict. The two declared keys
    are the input; "dev node" is derived from them and never declared.
    """
    if isinstance(proj, WorkspaceProject):
        return proj.dev_node
    return bool(proj.get("dev_only", False)) and proj.get("releasable") is False


def project_is_releasable(proj) -> bool:
    """Check if a project is releasable (works with WorkspaceProject or dict)."""
    if isinstance(proj, WorkspaceProject):
        return proj.is_releasable
    # For raw dicts: mirror the WorkspaceProject logic
    return proj.get("releasable") is not False
