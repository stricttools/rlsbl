"""File ownership: the tool-owned exempt set, single-owner attribution, and the scope claims a releasable makes on its own state directory.

The questions answered here, and nowhere else:

1. **Is this path tool-owned?**  :func:`is_tool_owned_path` decides from static
   path rules alone -- no config reads, no git calls, no workspace lookups.  A
   tool-owned path is rlsbl's own bookkeeping (changelog state, release state,
   the workspace directory, the generated CI router), so it never needs a
   changelog owner and never participates in attribution.

2. **Which workspace member owns this path?**  :func:`owner_of` answers with
   exactly one member: the most specific declared member path wins, and the
   root member (``path = "."``) owns everything no other member claims.

3. **Is this path inside the scope I am asking about?**
   :class:`OwnershipScope` answers, and it is the only question a *releasable*
   participates in: a releasable is not a member, so it owns no file, but its
   scope claims its own state directory
   (``.rlsbl-monorepo/releasables/<name>/``) on top of its members' files.

Kept free of imports from git, workspace, targets and checks so every layer can
depend on it -- :mod:`rlsbl.workspace_types` is the one exception, for the
directory names, and it imports nothing but :mod:`rlsbl.errors`.  Commit-level
attribution (which needs git) is in :mod:`rlsbl.git_util`, which imports this
module.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RlsblError
from .workspace_types import RELEASABLES_DIR, WORKSPACE_DIR


class OwnershipError(RlsblError):
    """File attribution could not be performed."""


# ---------------------------------------------------------------------------
# The reserved root member
# ---------------------------------------------------------------------------

#: The path spelling of the workspace member that owns the repository root.
ROOT_MEMBER_PATH = "."

#: The reserved name of that member.  A member at ``path = "."`` is named
#: ``root`` and nothing else; no other member may take the name.
ROOT_MEMBER_NAME = "root"

#: Path spellings that all mean the repository root.
_ROOT_PATH_SPELLINGS = ("", ".", "./")


# ---------------------------------------------------------------------------
# The tool-owned exempt set (static path rules only)
# ---------------------------------------------------------------------------

# Directory trees rlsbl writes and rewrites in full, at any directory depth: a
# member's own state directory (``python/.rlsbl/changes/...``) is recognised as
# readily as the repository root's, because every member root carries one.
_TOOL_OWNED_TREES = (
    ".rlsbl/changes/",
    ".rlsbl/releases/",
    ".rlsbl/bases/",
    ".rlsbl/lint/",
)

# Individual files rlsbl generates wholesale, at any depth.  ``CHANGELOG.md``
# is generated from the JSONL corpus and ``.rlsbl/version`` records the
# scaffolding version; both sit at every member root as well as the repository
# root.
#
# ``CHANGELOG.md`` over-matches, knowingly: a hand-written file that happens to
# be named ``CHANGELOG.md`` -- ``docs/CHANGELOG.md``, a vendored dependency's
# changelog -- is exempt from ownership and therefore from changelog coverage,
# even though rlsbl never wrote it.  Narrowing it would take a workspace lookup
# (which member roots exist?), and these rules are deliberately static: the
# same path answers the same way with no config read, no git call and no
# member list.  The accepted cost is that a commit touching only such a file
# needs no changelog entry.
_TOOL_OWNED_FILES = (
    ".rlsbl/version",
    "CHANGELOG.md",
)

# Trees and files that exist exactly once, at the repository root, and are
# therefore matched only there.  rlsbl writes one ``.rlsbl-monorepo/`` and one
# ``ci-router.yml`` per repository; a path with the same name deeper in the
# tree is somebody's own file and needs an owner like any other.  (The other
# scaffolded workflows are three-way merged with operator edits, so they stay
# owned by whoever's territory they sit in and are not listed here.)
_ROOT_ONLY_TREES = (
    WORKSPACE_DIR + "/",
)

_ROOT_ONLY_FILES = (
    ".github/workflows/ci-router.yml",
)


def releasable_state_dir(releasable_name) -> str:
    """Repo-relative path of a releasable's own state directory.

    ``.rlsbl-monorepo/releasables/<name>`` -- the changelog, the release
    archives, the version, the transition record and the config of one releasable.  No
    member's declared path claims it (it is tool-owned, and a releasable is not
    a member), so it is the releasable itself that claims it, at the scope
    level: see :meth:`OwnershipScope.for_releasable`.
    """
    return f"{WORKSPACE_DIR}/{RELEASABLES_DIR}/{releasable_name}"


def normalize_path(path) -> str:
    """Return *path* with ``\\`` separators folded and trailing slashes gone."""
    text = str(path).replace("\\", "/").strip()
    if text.startswith("./"):
        text = text[2:]
    if text in _ROOT_PATH_SPELLINGS:
        return ""
    return text.rstrip("/")


def tool_owned_rule(path) -> str | None:
    """Return the static rule making *path* tool-owned, or ``None``.

    The returned string is the rule itself (``".rlsbl/changes/**"``,
    ``"CHANGELOG.md"``), suitable for putting in a message that has to explain
    why a path needs no owner.  Root-anchored rules answer only for a path at
    the repository root; the rest answer at any depth.
    """
    normalized = normalize_path(path)
    if not normalized:
        return None
    for tree in _ROOT_ONLY_TREES:
        if normalized.startswith(tree):
            return tree + "**"
    for name in _ROOT_ONLY_FILES:
        if normalized == name:
            return name
    parts = normalized.split("/")
    for i in range(len(parts)):
        tail = "/".join(parts[i:])
        for tree in _TOOL_OWNED_TREES:
            if tail.startswith(tree):
                return tree + "**"
        for name in _TOOL_OWNED_FILES:
            if tail == name:
                return name
    return None


def is_tool_owned_path(path) -> bool:
    """Is *path* rlsbl's own bookkeeping, and therefore exempt from ownership?"""
    return tool_owned_rule(path) is not None


def tool_owned_rules() -> tuple[str, ...]:
    """Every static rule in the tool-owned set, for messages and tests."""
    return (
        tuple(tree + "**" for tree in _TOOL_OWNED_TREES)
        + _TOOL_OWNED_FILES
        + tuple(tree + "**" for tree in _ROOT_ONLY_TREES)
        + _ROOT_ONLY_FILES
    )


# ---------------------------------------------------------------------------
# Member accessors (WorkspaceProject or plain dict)
# ---------------------------------------------------------------------------


def member_path(member) -> str:
    """Return a member's declared path, normalized (``""`` for the root member)."""
    return normalize_path(member.get("path", ""))


def member_name(member) -> str:
    """Return a member's name."""
    return member.get("name", "")


def is_root_member(member) -> bool:
    """Does *member* declare the repository root as its territory?"""
    return member_path(member) == ""


def member_prefix(member) -> str:
    """Return the path prefix a member's files carry.

    ``""`` for the root member -- it claims every path -- and ``"pkg/"`` for a
    member at ``pkg``.  The root member's prefix used to be computed as
    ``"./"``, which matches no path git ever prints, so a root member owned
    nothing at all.
    """
    path = member_path(member)
    if not path:
        return ""
    return path + "/"


def find_root_member(members):
    """Return the root member of *members*, or ``None`` when there is none."""
    for member in members:
        if is_root_member(member):
            return member
    return None


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def owner_of(filepath, members):
    """Return the single member owning *filepath*, or ``None``.

    ``None`` means one of two things, both of which the caller must be able to
    live with: the path is tool-owned (:func:`is_tool_owned_path`), or the
    member list has no root member and no declared path claims the file.  A
    loaded workspace always has a root member, so within a real workspace only
    the first case occurs.

    Most specific wins: with members at ``.``, ``pkg`` and ``pkg/inner``, the
    file ``pkg/inner/a.py`` belongs to ``pkg/inner`` alone.
    """
    normalized = normalize_path(filepath)
    if not normalized or is_tool_owned_path(normalized):
        return None
    return _most_specific_claim(normalized, members)


def member_for_directory(dirpath, members, *, include_root):
    """Return the member whose territory a *directory* falls in.

    The same most-specific-path rule :func:`owner_of` uses, minus the
    tool-owned exclusion: a directory is a place to run a command from, not a
    file needing a changelog owner.

    *include_root* decides whether the root member may answer. It is mandatory
    because the two questions it separates are genuinely different: file
    attribution always gives the root member the residual, while "which project
    am I standing in?" is a question several commands answer with "none of
    them, you are at the workspace root" and act on. Callers state which they
    are asking.
    """
    normalized = normalize_path(dirpath)
    candidates = members if include_root else [
        m for m in members if not is_root_member(m)
    ]
    return _most_specific_claim(normalized, candidates)


def _most_specific_claim(normalized, members):
    """The most specific member path claiming *normalized*; root is the residual."""
    best = None
    best_len = -1
    for member in members:
        path = member_path(member)
        if path:
            if normalized != path and not normalized.startswith(path + "/"):
                continue
            claim_len = len(path)
        else:
            # The root member claims the residual: every path, at length 0, so
            # any declared member path outranks it.
            claim_len = 0
        if claim_len > best_len:
            best = member
            best_len = claim_len
    return best


def owner_name_of(filepath, members) -> str | None:
    """Name of the member owning *filepath*, or ``None`` (see :func:`owner_of`)."""
    owner = owner_of(filepath, members)
    return None if owner is None else member_name(owner)


def owners_of_files(files, members) -> dict:
    """Map each path in *files* to its owning member (or ``None``)."""
    return {path: owner_of(path, members) for path in files}


def owner_names_of_files(files, members) -> set:
    """Return the set of member names owning any path in *files*."""
    names = set()
    for path in files:
        owner = owner_of(path, members)
        if owner is not None:
            names.add(member_name(owner))
    return names


@dataclass(frozen=True)
class OwnershipScope:
    """A question asked of attribution: *which* members' files am I after?

    Attribution needs the **whole** member list to answer at all -- a file
    under ``pkg/inner`` belongs to ``pkg/inner`` even when the caller only
    cares about ``pkg``, and a root file belongs to the root member even when
    the caller only cares about ``pkg``.  Handing a function the members it
    cares about and nothing else is what let a file be claimed by two members
    at once, so the two halves travel together: ``members`` is every member in
    the workspace, ``owned`` names the subset in scope.

    ``state_dirs`` is the third half, and the one thing here that is not a
    member question.  A releasable's own state directory -- its changelog, its
    release archives, its version -- belongs to no member: no declared path
    claims it, and inventing a phantom member for it would break the invariant
    that every file has exactly one member owner.  A RELEASABLE's scope claims
    it directly instead, so archiving that releasable's release file and
    finalizing its changelog are commits inside that releasable's scope rather
    than outside every scope in the workspace.
    """

    members: tuple
    owned: frozenset
    state_dirs: frozenset = frozenset()

    @classmethod
    def for_members(cls, all_members, scope_members) -> "OwnershipScope":
        """Scope covering *scope_members*, claiming no state directory."""
        return cls(
            members=tuple(all_members),
            owned=frozenset(member_name(m) for m in scope_members),
        )

    @classmethod
    def for_releasable(cls, all_members, scope_members, releasable_name) -> "OwnershipScope":
        """Scope covering a releasable: its members AND its state directory."""
        return cls(
            members=tuple(all_members),
            owned=frozenset(member_name(m) for m in scope_members),
            state_dirs=frozenset({releasable_state_dir(releasable_name)}),
        )

    @classmethod
    def for_member(cls, all_members, one_member) -> "OwnershipScope":
        """Scope covering exactly one member."""
        return cls.for_members(all_members, [one_member])

    @classmethod
    def everything(cls, all_members) -> "OwnershipScope":
        """Scope covering every member of the workspace."""
        return cls.for_members(all_members, all_members)

    def owner_name_of(self, filepath) -> str | None:
        """Name of the member owning *filepath* (any member, in scope or not)."""
        return owner_name_of(filepath, self.members)

    def claims_state_dir(self, filepath) -> bool:
        """Does *filepath* sit inside a state directory this scope claims?"""
        normalized = normalize_path(filepath)
        if not normalized:
            return False
        for state_dir in self.state_dirs:
            if normalized == state_dir or normalized.startswith(state_dir + "/"):
                return True
        return False

    def claims(self, filepath) -> bool:
        """Is *filepath* in scope -- owned by a member, or in a state directory?

        The two answers cannot collide: a state directory is tool-owned, so it
        has no member owner to disagree with.  Being claimed here says which
        releasable a path belongs to; it says nothing about whether a changelog
        entry is REQUIRED for it, which the tool-owned exempt set still answers
        with "no".
        """
        return self.claims_state_dir(filepath) or self.owner_name_of(filepath) in self.owned

    def claims_any(self, files) -> bool:
        """Is any path in *files* owned by a member in scope?"""
        for path in files:
            if self.claims(path):
                return True
        return False

    def owned_members(self) -> list:
        """The in-scope members themselves, in workspace declaration order."""
        return [m for m in self.members if member_name(m) in self.owned]

    def describe(self) -> str:
        """A short human name for the scope, for error messages.

        A releasable's state-directory claim is named alongside its members:
        the directory belongs to no member, so a description built from the
        member names alone reads as if it were outside the scope -- and the
        message that carries it would then contradict what
        :meth:`claims` answers.
        """
        names = sorted(self.owned)
        described = ", ".join(names) if names else "(no members)"
        claims = sorted(self.state_dirs)
        if claims:
            described += " (and " + ", ".join(f"{c}/" for c in claims) + ")"
        return described


def unowned_paths(files, members) -> list:
    """Return the paths in *files* that are neither tool-owned nor claimed.

    Empty for every loaded workspace, since a root member is mandatory.  Used
    by the invariant tests and by diagnostics that have to explain a member
    list built by hand.
    """
    return [
        path
        for path in files
        if not is_tool_owned_path(path)
        and normalize_path(path)
        and owner_of(path, members) is None
    ]
