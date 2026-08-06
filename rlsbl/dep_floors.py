"""Dependency floor enforcement for ecosystem-internal dependencies.

The failure class this exists to catch: a release ships work that REQUIRES
new behavior from a sibling framework package. The development lock already
resolves the new framework version, so the repo's own suite passes -- but
the published manifest carries no ``>=`` floor (or a stale one), so a
consumer installing the artifact resolves an OLDER framework and breaks.
Three published releases shipped broken this way before this check existed.

The convention (campaign decision ledger): when a release requires new
framework behavior, the manifest carries a ``>=`` floor at that version.
Floors are not pins; upper bounds stay banned.

What is compared, per ecosystem:

| target | declared floor                                                | locked version       |
| ------ | ------------------------------------------------------------- | -------------------- |
| pypi   | ``pyproject.toml`` ``[project]`` dependencies + optional-dependencies | ``uv.lock``    |
| npm    | ``package.json`` dependencies / peerDependencies / optionalDependencies | ``package-lock.json`` |
| go     | ``go.mod`` ``require``                                        | ``go.mod`` (same file) |

Go is automatically satisfied and carries no comparison: a ``require`` line
IS the declared minimum, and the go toolchain resolves builds by minimal
version selection, so the build can never sit ahead of the declared floor.
The check degenerates to "a declared minimum exists", which the toolchain
guarantees -- so Go is reported as satisfied with a note, not evaluated.

The enforced set of "ecosystem-internal" dependencies comes from the
``internal_dep_floors`` config key (a list of package names) plus, in a
monorepo, every workspace sibling's package name. No project names are
hardcoded here, and nothing on this path touches the network: it reads only
committed manifests and lockfiles.

Semantics per enforced dependency, once the lock resolves it:

- the manifest does not declare it at all -> not this project's floor to
  declare (it is transitive); no verdict.
- the manifest declares it with no readable ``>=`` floor -> error.
- the LOCKED major.minor exceeds the DECLARED floor's major.minor -> error.
  Patch drift above the floor is fine; a minor or major boundary is not.
"""

import json
import os
import re
import tomllib

# Config key that gates adoption AND names the cross-repo internal deps.
CONFIG_KEY = "internal_dep_floors"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

# A PEP 508 requirement: name, optional extras, then the specifier set.
_REQ_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")

# npm range prefixes that point at a checkout or a remote, not a registry
# version -- no floor is meaningful for them.
_NPM_NON_REGISTRY = (
    "workspace:", "file:", "link:", "portal:", "npm:", "git+", "git:",
    "github:", "http://", "https://",
)


class DepFloorVerdict:
    """Result of evaluating internal dependency floors for one project."""

    def __init__(self, *, adopted, skip_reason=None, problems=None, notes=None):
        self.adopted = adopted
        self.skip_reason = skip_reason
        self.problems = list(problems or [])
        self.notes = list(notes or [])

    @property
    def ok(self):
        return not self.problems


# ---------------------------------------------------------------------------
# Version + floor parsing
# ---------------------------------------------------------------------------


def version_tuple(text):
    """Leading ``(major, minor)`` of a version string, or None.

    Floors are compared at major.minor: a patch bump in the lock never
    crosses a behavior boundary, so it is not a floor violation.
    """
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.match(text.strip().lstrip("vV="))
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)))


def pypi_floor(spec):
    """Read the lower bound of a PEP 440 specifier set.

    Returns ``("floor", (major, minor))`` when a lower bound is readable,
    ``("none", None)`` when the constraint pins no floor, or
    ``("skip", None)`` for constraints that carry no comparable version.
    """
    best = None
    for clause in (spec or "").split(","):
        clause = clause.strip()
        if not clause or clause.startswith(("!=", "<")):
            continue
        for op in ("===", ">=", "==", "~=", ">"):
            if clause.startswith(op):
                parsed = version_tuple(clause[len(op):])
                if parsed is not None and (best is None or parsed > best):
                    best = parsed
                break
    return ("floor", best) if best is not None else ("none", None)


def npm_floor(rng):
    """Read the lower bound of an npm semver range.

    Returns ``("floor", (major, minor))``, ``("none", None)`` when no
    lower bound is readable, or ``("skip", None)`` for non-registry
    ranges (workspace:, file:, git+, a URL) which have no floor to state.
    """
    text = (rng or "").strip()
    if not text:
        return ("none", None)
    if text.startswith(_NPM_NON_REGISTRY):
        return ("skip", None)
    # A disjunction has no single floor -- refuse to guess one.
    if "||" in text:
        return ("none", None)
    best = None
    for comparator in text.split():
        comparator = comparator.strip()
        if not comparator or comparator.startswith(("<", "!")):
            continue
        stripped = comparator.lstrip("^~>=v ")
        if stripped.startswith("*") or stripped.lower().startswith("x"):
            continue
        parsed = version_tuple(stripped)
        if parsed is not None and (best is None or parsed > best):
            best = parsed
    return ("floor", best) if best is not None else ("none", None)


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------


def normalize_pypi_name(name):
    """PEP 503 normalization: lowercase, runs of ``-_.`` collapse to ``-``."""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


def normalize_npm_name(name):
    return (name or "").strip().lower()


# ---------------------------------------------------------------------------
# Internal-dep identification
# ---------------------------------------------------------------------------


def workspace_package_names(workspace_root):
    """Package names of every sibling in a monorepo workspace.

    In a monorepo the workspace graph already knows which dependencies are
    ecosystem-internal, so siblings never need listing in config. Returns
    an empty set outside a monorepo.
    """
    if workspace_root is None:
        return set()
    from .workspace import WORKSPACE_DIR, WORKSPACE_FILE, load_workspace

    path = os.path.join(str(workspace_root), WORKSPACE_DIR, WORKSPACE_FILE)
    if not os.path.isfile(path):
        return set()
    return {
        (project.registry_name or project.name)
        for project in load_workspace(str(workspace_root))
    }


# ---------------------------------------------------------------------------
# pypi readers
# ---------------------------------------------------------------------------


def _load_toml(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _split_requirement(text):
    """Split a PEP 508 requirement into ``(normalized_name, kind, constraint)``.

    *kind* is ``"spec"`` for a version specifier or ``"url"`` for a direct
    reference (``name @ file:///...``), which has no floor to declare.
    Returns None when the requirement is unparseable.
    """
    if not isinstance(text, str):
        return None
    body = text.split(";", 1)[0].strip()
    if not body:
        return None
    if "@" in body:
        head = body.split("@", 1)[0].strip()
        m = _REQ_RE.match(head)
        if m is None:
            return None
        return (normalize_pypi_name(m.group(1)), "url", body)
    m = _REQ_RE.match(body)
    if m is None:
        return None
    return (normalize_pypi_name(m.group(1)), "spec", m.group(2).strip())


def pypi_declared(project_root):
    """Declared runtime + extra requirements from ``pyproject.toml``.

    Returns ``{normalized_name: (section_label, kind, constraint)}``, or
    None when there is no readable ``pyproject.toml``.
    """
    data = _load_toml(os.path.join(str(project_root), "pyproject.toml"))
    if data is None:
        return None
    project = data.get("project") or {}
    buckets = [("[project].dependencies", project.get("dependencies") or [])]
    for extra, entries in (project.get("optional-dependencies") or {}).items():
        buckets.append(
            (f"[project].optional-dependencies.{extra}", entries or [])
        )

    declared = {}
    for label, entries in buckets:
        for entry in entries:
            split = _split_requirement(entry)
            if split is None:
                continue
            name, kind, constraint = split
            # First declaration wins: runtime deps are read before extras,
            # and a runtime floor is the one consumers actually resolve.
            declared.setdefault(name, (label, kind, constraint))
    return declared


def pypi_locked(project_root):
    """Resolved versions from ``uv.lock``, or None when there is no lock."""
    data = _load_toml(os.path.join(str(project_root), "uv.lock"))
    if data is None:
        return None
    locked = {}
    for package in data.get("package") or []:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            locked[normalize_pypi_name(name)] = version
    return locked


# ---------------------------------------------------------------------------
# npm readers
# ---------------------------------------------------------------------------


def npm_declared(project_root):
    """Declared consumer-visible dependency ranges from ``package.json``.

    devDependencies are excluded: they never reach a consumer's resolver.
    Returns ``{normalized_name: (section_label, declared_name, range)}``, or
    None when there is no readable ``package.json``.
    """
    data = _load_json(os.path.join(str(project_root), "package.json"))
    if data is None:
        return None
    declared = {}
    for section in ("dependencies", "peerDependencies", "optionalDependencies"):
        entries = data.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for name, rng in entries.items():
            if isinstance(name, str) and isinstance(rng, str):
                declared.setdefault(
                    normalize_npm_name(name), (section, name, rng)
                )
    return declared


def npm_locked(project_root):
    """Resolved versions from ``package-lock.json`` (v1, v2 and v3 shapes)."""
    data = _load_json(os.path.join(str(project_root), "package-lock.json"))
    if data is None:
        return None
    locked = {}

    packages = data.get("packages")
    if isinstance(packages, dict):
        for key, entry in packages.items():
            if not isinstance(entry, dict) or not key.startswith("node_modules/"):
                continue
            name = key[len("node_modules/"):]
            # Nested installs (a/node_modules/b) are not the top-level resolution.
            if "node_modules/" in name or entry.get("link"):
                continue
            version = entry.get("version")
            if isinstance(version, str):
                locked[normalize_npm_name(name)] = version

    if not locked:
        # lockfileVersion 1 keeps the tree under "dependencies".
        deps = data.get("dependencies")
        if isinstance(deps, dict):
            for name, entry in deps.items():
                if not isinstance(entry, dict):
                    continue
                version = entry.get("version")
                if isinstance(version, str):
                    locked[normalize_npm_name(name)] = version
    return locked


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _floor_problem(name, constraint, locked_version, *, kind, floor, where, remedy):
    """One problem string for a dependency whose floor lags the lock, else None."""
    if kind == "skip":
        return None
    locked_tuple = version_tuple(locked_version)
    if locked_tuple is None:
        return None
    if kind == "none":
        return (
            f"{name}: {where} declares no version floor, but the lock resolves "
            f"{locked_version} -- a consumer can resolve an older {name} than "
            f"this release was built against; declare {remedy}"
        )
    if locked_tuple > floor:
        return (
            f"{name}: {where} declares '{constraint}', but the lock resolves "
            f"{locked_version} -- the declared floor is behind the version this "
            f"release was built against; declare {remedy}"
        )
    return None


def _evaluate_pypi(root, names):
    declared = pypi_declared(root)
    if declared is None:
        return [], []
    locked = pypi_locked(root)
    if locked is None:
        return [], ["pypi: no uv.lock -- no locked versions to compare"]

    problems = []
    for name in sorted({normalize_pypi_name(n) for n in names} & set(declared)):
        label, req_kind, constraint = declared[name]
        version = locked.get(name)
        if version is None:
            continue
        if req_kind == "url":
            continue
        kind, floor = pypi_floor(constraint)
        problem = _floor_problem(
            name,
            constraint,
            version,
            kind=kind,
            floor=floor,
            where=f"pyproject.toml {label}",
            remedy=f'"{name}>={version}"',
        )
        if problem is not None:
            problems.append(problem)
    return problems, []


def _evaluate_npm(root, names):
    declared = npm_declared(root)
    if declared is None:
        return [], []
    locked = npm_locked(root)
    if locked is None:
        return [], ["npm: no package-lock.json -- no locked versions to compare"]

    problems = []
    for key in sorted({normalize_npm_name(n) for n in names} & set(declared)):
        section, declared_name, rng = declared[key]
        version = locked.get(key)
        if version is None:
            continue
        kind, floor = npm_floor(rng)
        problem = _floor_problem(
            declared_name,
            rng,
            version,
            kind=kind,
            floor=floor,
            where=f"package.json {section}",
            remedy=f'"{declared_name}": ">={version}"',
        )
        if problem is not None:
            problems.append(problem)
    return problems, []


def _evaluate_go(root, names):
    """Go floors are structural -- see the module docstring."""
    if not os.path.isfile(os.path.join(str(root), "go.mod")):
        return [], []
    return [], [
        "go: go.mod require lines ARE the declared minimums and the build "
        "resolves by minimal version selection -- the lock cannot run ahead "
        "of the floor, so there is nothing to enforce"
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def evaluate_dep_floors(config, project_root, workspace_names=None):
    """Evaluate internal dependency floors for one project.

    Returns a :class:`DepFloorVerdict`. Projects that have not adopted the
    ``internal_dep_floors`` config key come back with ``adopted=False`` and
    a skip reason.
    """
    config = config or {}
    if CONFIG_KEY not in config:
        return DepFloorVerdict(
            adopted=False,
            skip_reason=(
                f"internal dep floors not adopted (no {CONFIG_KEY} config key)"
            ),
        )

    listed = config.get(CONFIG_KEY)
    if not isinstance(listed, list) or any(
        not isinstance(n, str) or not n.strip() for n in listed
    ):
        return DepFloorVerdict(
            adopted=True,
            problems=[
                f"{CONFIG_KEY} must be a list of package names, got "
                f"{type(listed).__name__}"
            ],
        )

    names = {n.strip() for n in listed} | set(workspace_names or ())
    if not names:
        return DepFloorVerdict(
            adopted=True, notes=["no ecosystem-internal dependencies to enforce"]
        )

    root = str(project_root)
    problems = []
    notes = []
    for evaluate in (_evaluate_pypi, _evaluate_npm, _evaluate_go):
        found, ecosystem_notes = evaluate(root, names)
        problems.extend(found)
        notes.extend(ecosystem_notes)
    return DepFloorVerdict(adopted=True, problems=problems, notes=notes)
