"""Path-dependency detection and rewriting for Python projects.

Two consumers share this module, and they want the same primitives applied to
different section families:

* **The PyPI build** (:mod:`rlsbl.targets.pypi`) rewrites a COPY of
  ``pyproject.toml`` in a temp directory so a monorepo package's sibling path
  dependencies become version constraints in the built artifact.  It covers
  the sections that end up in published metadata --
  :data:`SECTIONS_PUBLISHED`.
* **``rlsbl rewrite uv-path-sources``** rewrites the WORKING TREE, converting
  every path/workspace-sourced internal dependency into a registry floor.  It
  covers :data:`SECTIONS_ALL`, which adds PEP 735 ``[dependency-groups]``:
  a dev-only path source is exactly as unbuildable for a consumer's checkout
  as a runtime one, even though it never reaches published metadata.

The section family is always passed explicitly.  A default would silently
decide which of those two jobs a caller is doing.
"""

import os
import re
import sys

import tomlkit

from .errors import VersionError
from .targets import TARGETS, detect_targets, resolve_releasable_config_dir
from .workspace_graph import _parse_pypi_dep_name

#: Sections whose contents reach a consumer through published metadata.
SECTIONS_PUBLISHED = ("dependencies", "optional-dependencies")

#: Every section a dependency can be declared in, including PEP 735 groups.
SECTIONS_ALL = ("dependencies", "optional-dependencies", "dependency-groups")


# ---------------------------------------------------------------------------
# Section iteration -- the one place that knows where dependencies live
# ---------------------------------------------------------------------------


def iter_dep_arrays(doc, families):
    """Yield ``(section_label, array)`` for each dependency array in *doc*.

    *doc* is a parsed tomlkit document; the arrays are yielded live, so a
    caller can mutate them in place and dump the document afterwards.
    *families* selects which section families to visit -- see
    :data:`SECTIONS_PUBLISHED` / :data:`SECTIONS_ALL`.
    """
    project = doc.get("project")
    if project is not None:
        if "dependencies" in families:
            main = project.get("dependencies")
            if main is not None:
                yield ("dependencies", main)
        if "optional-dependencies" in families:
            optional = project.get("optional-dependencies")
            if optional is not None:
                for group, entries in optional.items():
                    yield (f"optional-dependencies.{group}", entries)
    if "dependency-groups" in families:
        groups = doc.get("dependency-groups")
        if groups is not None:
            for group, entries in groups.items():
                yield (f"dependency-groups.{group}", entries)


def detect_path_deps_in(doc, families):
    """Path dependencies declared in *doc*, restricted to *families*.

    Returns a list of dicts with keys ``name``, ``original``, ``line_in_deps``
    (index within its array) and ``section``.
    """
    results = []
    for label, entries in iter_dep_arrays(doc, families):
        for i, dep_str in enumerate(entries):
            name, is_path, _constraint = _parse_pypi_dep_name(str(dep_str))
            if name and is_path:
                results.append({
                    "name": name,
                    "original": str(dep_str),
                    "line_in_deps": i,
                    "section": label,
                })
    return results


def rewrite_dep_arrays(doc, rewrites, families):
    """Rewrite path deps named in *rewrites*, in place.  Returns the count.

    *rewrites* maps package name to a version constraint (``">=1.2.0"``).
    Only PATH dependencies are rewritten: a dependency already carrying a
    registry constraint is left alone.
    """
    changed = 0
    for _label, entries in iter_dep_arrays(doc, families):
        for i in range(len(entries)):
            dep_str = str(entries[i])
            name, is_path, _constraint = _parse_pypi_dep_name(dep_str)
            if name and is_path and name in rewrites:
                entries[i] = f"{name}{rewrites[name]}"
                changed += 1
    return changed


# ---------------------------------------------------------------------------
# Flooring a dependency entry at a version
# ---------------------------------------------------------------------------

#: The leading ``name`` and optional ``[extras]`` of a PEP 508 requirement.
_NAME_EXTRAS_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?")


def _split_requirement_parts(text):
    """``(name, extras, marker)`` of a requirement, or None when unparseable.

    *extras* keeps its brackets (``"[cli]"``) or is empty; *marker* keeps its
    leading ``;`` (``"; python_version < '3.12'"``) or is empty.  Both are
    carried through a rewrite verbatim: dropping an extra changes what gets
    installed, and dropping a marker changes WHERE it gets installed.
    """
    body, sep, marker = str(text).partition(";")
    m = _NAME_EXTRAS_RE.match(body)
    if m is None:
        return None
    return (m.group(1), m.group(2) or "", (sep + marker) if sep else "")


def find_dep_entries(doc, names, families):
    """Every dependency-array entry naming one of *names*.

    *names* is a mapping of normalized name to the name as declared, so the
    caller decides the normalization (PEP 503 for PyPI).  Returns a list of
    dicts with ``section``, ``index``, ``original``, ``normalized``.
    """
    found = []
    for label, entries in iter_dep_arrays(doc, families):
        for i, entry in enumerate(entries):
            parts = _split_requirement_parts(entry)
            if parts is None:
                continue
            normalized = _normalize(parts[0])
            if normalized in names:
                found.append({
                    "section": label,
                    "index": i,
                    "original": str(entry),
                    "normalized": normalized,
                })
    return found


def floor_dep_entries(doc, floors, families):
    """Rewrite entries naming a package in *floors* to ``name[extras]>=version``.

    *floors* maps normalized package name to the floor version string.  Unlike
    :func:`rewrite_dep_arrays` this touches an entry whatever its current form
    -- a bare ``"sibling"``, an existing ``"sibling>=0.1"`` and a direct
    ``"sibling @ file:///..."`` all become ``"sibling>=<floor>"``.  That is the
    point of the conversion: the floor must be the LOCKED version, not
    whatever the manifest happened to say while the dependency resolved from a
    checkout.  Returns the number of entries rewritten.
    """
    changed = 0
    for _label, entries in iter_dep_arrays(doc, families):
        for i in range(len(entries)):
            parts = _split_requirement_parts(entries[i])
            if parts is None:
                continue
            name, extras, marker = parts
            normalized = _normalize(name)
            if normalized not in floors:
                continue
            entries[i] = f"{name}{extras}>={floors[normalized]}{marker}"
            changed += 1
    return changed


def _normalize(name):
    """PEP 503 normalization, local to avoid a cycle through dep_floors."""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


# ---------------------------------------------------------------------------
# [tool.uv.sources]
# ---------------------------------------------------------------------------


def detect_uv_path_sources(doc):
    """Path/workspace entries in ``[tool.uv.sources]``.

    Returns ``{package_name: kind}`` where *kind* is ``"path"`` or
    ``"workspace"``.  Registry-neutral sources (``git``, ``url``, ``index``)
    are not returned: they already resolve to something a consumer can fetch.
    A source declared as a LIST of marker-gated tables counts when any of its
    entries is a path or workspace source.
    """
    tool = doc.get("tool")
    if tool is None:
        return {}
    uv = tool.get("uv")
    if uv is None:
        return {}
    sources = uv.get("sources")
    if sources is None:
        return {}

    found = {}
    for package, spec in sources.items():
        entries = spec if isinstance(spec, list) else [spec]
        for entry in entries:
            kind = _source_kind(entry)
            if kind is not None:
                found[str(package)] = kind
                break
    return found


def _source_kind(entry):
    """``"path"``, ``"workspace"`` or None for one ``[tool.uv.sources]`` entry."""
    if not hasattr(entry, "get"):
        return None
    if "path" in entry:
        return "path"
    if entry.get("workspace"):
        return "workspace"
    return None


def remove_uv_sources(doc, names):
    """Remove the path/workspace sources for *names*.  Returns the count.

    A source declared as a LIST of marker-gated tables is pruned rather than
    deleted: only its path/workspace elements go, and a non-path sibling (an
    ``index`` variant for the platforms the checkout does not cover, say)
    stays.  Deleting the whole key would drop a declaration the caller never
    asked about and that nothing else restores.  A list left with exactly one
    element stays a list -- uv reads it identically, and collapsing it would
    rewrite formatting this function was not asked to touch.

    A list whose every element is a path/workspace source is removed entirely,
    like a plain table.  An emptied ``sources`` table is removed too (and an
    emptied ``[tool.uv]`` with it), so the rewrite does not leave a header for
    nothing.

    The count is per NAME, not per element: it answers "how many of the names
    I asked about were sourced from a checkout", which is what both callers
    compare against their preview.
    """
    tool = doc.get("tool")
    if tool is None:
        return 0
    uv = tool.get("uv")
    if uv is None:
        return 0
    sources = uv.get("sources")
    if sources is None:
        return 0

    removed = 0
    for name in names:
        if name not in sources:
            continue
        spec = sources[name]
        if not isinstance(spec, list):
            del sources[name]
            removed += 1
            continue
        # Delete back to front so the surviving indices stay valid.
        doomed = [
            i for i, entry in enumerate(spec)
            if _source_kind(entry) is not None
        ]
        if not doomed:
            continue
        if len(doomed) == len(spec):
            del sources[name]
        else:
            for i in reversed(doomed):
                del spec[i]
        removed += 1
    if removed and len(sources) == 0:
        del uv["sources"]
        if len(uv) == 0:
            del tool["uv"]
            if len(tool) == 0:
                del doc["tool"]
    return removed


# ---------------------------------------------------------------------------
# The PyPI build's entry points (published sections only)
# ---------------------------------------------------------------------------


def detect_path_deps(pyproject_path):
    """Detect path dependencies in a pyproject.toml file.

    Covers the published sections only (:data:`SECTIONS_PUBLISHED`) -- this is
    the build's question: "does the artifact's metadata reference a checkout?"

    Returns a list of dicts with keys:
      - name: the dependency package name
      - original: the full original dependency string
      - line_in_deps: index within the dependencies array
      - section: "dependencies" or "optional-dependencies.<group>"
    """
    if not os.path.isfile(pyproject_path):
        return []

    try:
        with open(pyproject_path, "r", encoding="utf-8") as f:
            data = tomlkit.parse(f.read())
    except Exception as exc:
        print(f"Warning: failed to parse {pyproject_path}: {exc}", file=sys.stderr)
        return []

    return detect_path_deps_in(data, SECTIONS_PUBLISHED)


def rewrite_pyproject_deps(content, rewrites):
    """Rewrite path dependencies in pyproject.toml content to versioned constraints.

    Args:
        content: raw pyproject.toml content string
        rewrites: dict mapping package name to version constraint string
                  (e.g., {"core": ">=1.2.0"})

    Returns the modified content as a string with formatting preserved.
    """
    if not rewrites:
        return content

    doc = tomlkit.parse(content)
    if doc.get("project") is None:
        return content
    rewrite_dep_arrays(doc, rewrites, SECTIONS_PUBLISHED)
    return tomlkit.dumps(doc)


def build_rewrite_map(workspace_root, projects, graph):
    """Build a mapping of dependency names to version constraints.

    For each project in the workspace that has a detectable version,
    adds ``name: ">=version"`` to the map. This map is intended to be
    passed to ``rewrite_pyproject_deps``.

    Args:
        workspace_root: absolute path to the workspace root
        projects: list of project dicts (each with "name" and "path")
        graph: a WorkspaceGraph instance (unused currently, reserved for
               future constraint refinement)

    Returns a dict mapping package name to version constraint string.
    """
    rewrite_map = {}
    for proj in projects:
        proj_dir = os.path.join(workspace_root, proj["path"])
        rel_dir = resolve_releasable_config_dir(proj, workspace_root)
        targets = detect_targets(proj_dir, releasable_config_dir=rel_dir)
        for entry in targets:
            target = TARGETS.get(entry.name)
            if target is None:
                continue
            try:
                version = target.read_version(entry.path)
            except (VersionError, FileNotFoundError, KeyError):
                continue
            if version:
                rewrite_map[proj["name"]] = f">={version}"
                break  # one version per project is enough
    return rewrite_map
