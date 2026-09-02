"""Shared git tag-glob resolution for monorepo projects and releasables.

A single source of truth for deriving the ``git tag`` match pattern of a
monorepo project. Two code paths previously derived this independently
(``rlsbl monorepo status`` and ``rlsbl status``), and one of them ignored the
releasable's ``tag_format`` -- so releasable members reported no tag. Both now
call :func:`resolve_monorepo_tag_glob`.
"""

import os
import re
from dataclasses import dataclass
from enum import Enum

from .workspace_types import DEFAULT_TAG_FORMAT


class TagMode(Enum):
    """Whether :func:`parse_version_tag` accepts prerelease tags.

    ``FINAL_ONLY`` rejects any tag carrying a prerelease suffix (``v1.2.3-rc.1``)
    -- used by call sites that count or match *final* releases only.
    ``PRERELEASE_INCLUSIVE`` accepts both final and prerelease tags. There is no
    default: every call site must state its semantics explicitly so the mode can
    never silently flip.
    """

    FINAL_ONLY = "final_only"
    PRERELEASE_INCLUSIVE = "prerelease_inclusive"


@dataclass(frozen=True)
class TagVersion:
    """Result of parsing a git tag into a version.

    Attributes:
        version: the ``x.y.z`` (optionally ``-prerelease``) string, no ``v``.
        scheme: which tag scheme matched -- ``"standalone"`` (``v1.2.3``),
            ``"monorepo"`` (``name@v1.2.3``), or ``"path"`` (``path/v1.2.3``).
    """

    version: str
    scheme: str


# Strict semver core: exactly three dotted numeric components.
_CORE = r"\d+\.\d+\.\d+"
# Semver prerelease suffix: dot-separated identifiers of [0-9A-Za-z-].
_PRERELEASE = r"-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"


def _version_group(mode: "TagMode") -> str:
    if mode is TagMode.FINAL_ONLY:
        return f"({_CORE})"
    if mode is TagMode.PRERELEASE_INCLUSIVE:
        return f"({_CORE}(?:{_PRERELEASE})?)"
    raise TypeError(f"mode must be a TagMode, got {mode!r}")


# Anchored, whole-tag scheme patterns. Order matters only for disambiguation of
# schemes; each is fully anchored so a tag matches exactly one version shape.
# ``{ver}`` is substituted with the mode-appropriate capturing group.
_SCHEME_PATTERNS = (
    ("standalone", r"^v{ver}$"),
    ("monorepo", r"^.+@v{ver}$"),
    ("path", r"^.+/v{ver}$"),
)


def parse_version_tag(tag: str, *, mode: "TagMode") -> "TagVersion | None":
    """Parse a git *tag* into a :class:`TagVersion`, or ``None`` if it is not a
    version tag under any known scheme.

    Recognizes three schemes, matched against the *whole* tag (strict):

    - standalone: ``v1.2.3``
    - monorepo: ``name@v1.2.3``
    - Go path-style: ``some/path/v1.2.3``

    The version core must be strict semver (``x.y.z``); milestone-like tags
    (``latest``, ``milestone-3``), partial versions (``v1.2``), and tags with no
    scheme separator before ``v`` are rejected. *mode* is required and controls
    whether a prerelease suffix is accepted:

    - :attr:`TagMode.FINAL_ONLY` -- ``v1.2.3-rc.1`` is rejected (returns ``None``).
    - :attr:`TagMode.PRERELEASE_INCLUSIVE` -- ``v1.2.3-rc.1`` yields ``1.2.3-rc.1``.
    """
    ver = _version_group(mode)
    for scheme, template in _SCHEME_PATTERNS:
        m = re.match(template.format(ver=ver), tag)
        if m:
            return TagVersion(version=m.group(1), scheme=scheme)
    return None


def releasable_tag_glob(tag_format: str, releasable_name: str) -> str:
    """Derive a glob from a releasable's ``tag_format``.

    Replaces ``{version}`` with ``*`` and fills in ``{name}`` with the literal
    releasable name so ``git tag -l`` matches all versions
    (e.g. ``"{name}@v{version}"`` -> ``"www@v*"``).
    """
    return tag_format.replace("{version}", "*").format(name=releasable_name)


def _target_tag_scheme(target, name, path):
    """Classify a target's monorepo tag scheme.

    Returns ``"at"`` for the ``{name}@v*`` scheme (base default, used by all
    targets except Go) or ``"path"`` for the ``{path}/v*`` scheme (Go's
    path-based module-proxy tags). Classification is derived from the target's
    own ``monorepo_tag_glob`` output so it stays correct if new targets adopt a
    path-based scheme.
    """
    glob = target.monorepo_tag_glob(name, path=path)
    return "at" if glob.endswith("@v*") else "path"


def _mixed_tag_schemes(target_entries, name, path):
    """Group a member's targets by tag scheme.

    Returns a dict ``{scheme: [target_name, ...]}`` when the member's targets
    span more than one tag scheme (the mixed-scheme hazard), otherwise
    ``None``. Only targets present in ``TARGETS`` are considered.
    """
    from .targets import TARGETS

    schemes: dict[str, list[str]] = {}
    for te in target_entries:
        if te.name not in TARGETS:
            continue
        scheme = _target_tag_scheme(TARGETS[te.name], name, path)
        schemes.setdefault(scheme, []).append(te.name)
    return schemes if len(schemes) > 1 else None


def derive_releasable_tag_format(target_entries, name, path, *, subject):
    """The tag format an auto-singleton releasable is created with.

    Derived from the member's PRIMARY target's monorepo scheme and returned
    literally: ``{name}@v{version}`` for every target but Go, and the Go module
    proxy's ``<path>/v{version}`` for Go. A member whose targets span BOTH
    schemes has no single answer, so it is a refusal naming ``--tag-format``
    rather than a silent pick of whichever target was detected first.

    Both commands that create a releasable from a member -- ``monorepo add``
    and ``monorepo absorb`` -- derive through here, so the format a member's
    targets imply is one fact with one answer.

    Args:
        target_entries: the member's detected (or declared) TargetEntry list.
        name: the releasable's name, which fills ``{name}`` in the @-scheme.
        path: the member's repo-relative path, which the Go path-scheme tags under.
        subject: how the refusal names what declared the mixed targets
            (e.g. ``"member dir 'packages/widget'"``).

    Raises:
        MixedTagSchemeError: the member's targets span both tag schemes.
    """
    from .errors import MixedTagSchemeError
    from .targets import TARGETS

    mixed = _mixed_tag_schemes(target_entries, name, path)
    if mixed:
        path_names = ", ".join(mixed.get("path", []))
        at_names = ", ".join(mixed.get("at", []))
        raise MixedTagSchemeError(
            f"{subject} declares targets with incompatible monorepo tag "
            f"schemes: path-style ({path_names}) and @-style ({at_names}). A "
            f"releasable has exactly one tag format, and picking whichever "
            f"target was detected first would tag this unit under a scheme "
            f"nobody chose. State it: re-run with --tag-format "
            f"\"{{name}}@v{{version}}\" or --tag-format \"{path}/v{{version}}\"."
        )
    for entry in target_entries:
        target = TARGETS.get(entry.name)
        if target is None:
            continue
        # Asked of the target rather than pattern-matched on its name: which
        # scheme a target tags under is the target's own fact. The @-scheme is
        # the workspace format with the name left as a placeholder; the
        # path-scheme has no name in it at all, so the target renders it with
        # the version placeholder standing in for a version.
        if _target_tag_scheme(target, name, path) == "at":
            return DEFAULT_TAG_FORMAT
        return target.monorepo_tag_format(name, "{version}", path=path)
    return DEFAULT_TAG_FORMAT


def _mixed_scheme_error(project_path, mixed):
    """Build the hard-error message for a mixed-tag-scheme member dir."""
    path_names = ", ".join(mixed.get("path", []))
    at_names = ", ".join(mixed.get("at", []))
    return (
        f"Member dir '{project_path}' declares targets with incompatible "
        f"monorepo tag schemes: path-style ({path_names}) and @-style "
        f"({at_names}). Declare separate member dirs or drop one target."
    )


def resolve_monorepo_tag_glob(project, workspace_root, releasable=None) -> str:
    """Return the ``git tag`` glob for a monorepo *project*.

    When the project belongs to a *releasable* (explicit mode), the glob is
    derived from the releasable's ``tag_format`` -- this is the fix for
    releasable members previously falling back to the per-member target glob
    and reporting no tag. Otherwise the glob comes from the project's first
    detected target's ``monorepo_tag_glob``, or ``"{name}@v*"`` when no target
    is detected.

    Args:
        project: a WorkspaceProject or dict with ``name``/``path``.
        workspace_root: path to the monorepo root (str or Path).
        releasable: optional Releasable the project belongs to; when provided,
            its ``tag_format`` drives the glob.
    """
    if releasable is not None:
        return releasable_tag_glob(releasable.effective_tag_format, releasable.name)

    from .targets import TARGETS, detect_targets, resolve_releasable_config_dir

    rel_dir = resolve_releasable_config_dir(project, workspace_root)
    proj_dir = os.path.join(str(workspace_root), project["path"])
    target_entries = detect_targets(proj_dir, releasable_config_dir=rel_dir)
    if target_entries and target_entries[0].name in TARGETS:
        return TARGETS[target_entries[0].name].monorepo_tag_glob(
            project["name"], path=project["path"]
        )
    return f"{project['name']}@v*"
