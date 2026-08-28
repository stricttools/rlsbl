"""Convert path/workspace-sourced dependencies into registry floors.

``rlsbl rewrite uv-path-sources`` takes a working tree whose ``pyproject.toml``
resolves internal dependencies from local checkouts and turns each one into a
registry constraint floored at the version the lock already resolves:

* ``[project].dependencies``, every ``[project.optional-dependencies]`` extra
  and every PEP 735 ``[dependency-groups]`` group have their direct references
  (``dep @ file:///...``) replaced by ``dep>=<locked version>``;
* the matching ``[tool.uv.sources]`` path/workspace entry is deleted, because
  a source entry left behind keeps overriding the constraint that was just
  written.  A source declared as a LIST of marker-gated tables is PRUNED
  instead: only its path/workspace elements go, so an ``index`` sibling
  covering the platforms the checkout does not is left standing;
* ``.rlsbl/config.json``'s ``internal_dep_floors`` gains every converted name,
  so rlsbl's ``dep-floors`` preflight check starts policing the floor it just
  created (the key is created when absent).

Where the lock is read from
---------------------------

The floor is the version ``uv.lock`` resolves, and there is exactly ONE lock
that resolves a given manifest.  Which file that is comes from
:func:`rlsbl.uv_workspace.locate_uv_lock` -- the shared locator this command
originally grew and now shares with the ``dep-floors`` and ``dep-locks``
checks: the lock beside the manifest, else the lock of the uv workspace root
that claims the directory, else nothing.  A search that finds nothing is a hard
error here, naming both locations probed.

Whichever lock is read, the WRITES stay in the target directory: its own
``pyproject.toml`` and its own ``.rlsbl/config.json`` (created when absent).

Release-first, enforced
-----------------------

A floor is only meaningful if the registry can satisfy it.  Before writing
anything the command probes PyPI for the exact locked version of each
dependency, and:

* **not published** is a hard error naming the remedy -- release that
  dependency first.  Writing the floor anyway would produce a manifest no
  consumer can resolve, which is the exact failure this command exists to
  prevent.
* **probe failure** (network, HTTP 5xx, anything that is not a clean 404) is
  ALSO a hard error.  Fail-closed: "we could not ask" is not evidence of
  publication, and a floor written on a failed probe is indistinguishable from
  one written on a lie.

The probe is a plain HTTPS GET through ``effects.urlopen``, which executes in
every mode including ``--dry-run`` -- a read changes nothing on the far side,
and a preview that could not probe would have nothing to preview.

Preview and apply
-----------------

One verdict item per dependency, plus one for the config-key update.  Each
dependency's item carries the number of manifest entries it occupies, and the
apply re-counts them from disk before writing: a count that moved between
preview and apply is a hard abort with nothing further written.
"""

import os
import sys
from dataclasses import dataclass

import tomlkit

from ... import effects
from ...config import _project_config, read_json_config
from ...dep_floors import CONFIG_KEY, normalize_pypi_name, pypi_locked_at
from ...dep_rewrite import (
    SECTIONS_ALL,
    detect_path_deps_in,
    detect_uv_path_sources,
    find_dep_entries,
    floor_dep_entries,
    remove_uv_sources,
)
from ...preview_apply import Preview, Reconciler, VerdictItem, reconcile, single
from ...registry import query_pypi_release
from ...uv_workspace import locate_uv_lock
from .abort import already_written


class UvPathSourceError(Exception):
    """A hard error in the path-source conversion."""


@dataclass
class Conversion:
    """One dependency's pending conversion."""

    name: str
    locked_version: str
    source_kind: str | None      # "path", "workspace", or None (no sources entry)
    sections: tuple[str, ...] = ()   # manifest sections carrying a path ref
    dep_entries: int = 0             # how many dep-array entries it occupies
    source_entries: int = 0          # 1 when [tool.uv.sources] carries it

    @property
    def occurrences(self):
        """Everything the apply would rewrite for this dependency."""
        return self.dep_entries + self.source_entries

    @property
    def constraint(self):
        return f">={self.locked_version}"


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------


def _read_doc(pyproject_path):
    if not os.path.isfile(pyproject_path):
        raise UvPathSourceError(
            f"no pyproject.toml at {pyproject_path}: this command rewrites a "
            f"Python project's manifest"
        )
    try:
        with open(pyproject_path, "r", encoding="utf-8") as f:
            return tomlkit.parse(f.read())
    except Exception as exc:
        raise UvPathSourceError(
            f"failed to parse {pyproject_path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Locating and reading the lock
# ---------------------------------------------------------------------------


def resolve_lock(project_root):
    """``(locked versions, LockLocation)`` for the manifest in *project_root*.

    The location comes from :func:`rlsbl.uv_workspace.locate_uv_lock`; this
    function adds the two refusals that belong to READING it: no lock at all,
    and a lock that is present but does not parse.  The second is a hard error
    rather than an absence, because treating it as absent would walk past it to
    a different lock -- exactly the silent switch this command must not make.
    """
    root = str(project_root)
    search = locate_uv_lock(root)
    if search.location is None:
        raise UvPathSourceError(
            f"no uv.lock for {root}: the floor is read from the lock's resolved "
            f"version, so there is nothing to floor at. Probed {search.probed}. "
            f"Run `uv lock` and re-run."
        )

    locked = pypi_locked_at(search.location.path)
    if locked is None:
        raise UvPathSourceError(
            f"{search.location.path} exists but could not be read as TOML, so "
            f"the version to floor at cannot be determined. Fix the lock (or "
            f"run `uv lock`) and re-run."
        )
    return locked, search.location


def path_sourced_names(doc):
    """``{normalized: declared}`` for every path/workspace-sourced dependency.

    The union of two declarations, because uv accepts either shape:

    * a ``[tool.uv.sources]`` entry with ``path``/``workspace``, whose
      dependency array entry is an ORDINARY requirement (often bare
      ``"sibling"`` with no constraint at all) -- the common uv shape;
    * a direct reference in the dependency array itself
      (``"sibling @ file:///..."``) with no sources entry.
    """
    names = {}
    for name in detect_uv_path_sources(doc):
        names[normalize_pypi_name(name)] = name
    for entry in detect_path_deps_in(doc, SECTIONS_ALL):
        names.setdefault(normalize_pypi_name(entry["name"]), entry["name"])
    return names


def count_entries(doc, name):
    """``(dependency-array entries, sources entries)`` naming *name*."""
    normalized = normalize_pypi_name(name)
    dep_entries = find_dep_entries(doc, {normalized: name}, SECTIONS_ALL)
    source_entries = sum(
        1 for source in detect_uv_path_sources(doc)
        if normalize_pypi_name(source) == normalized
    )
    return len(dep_entries), source_entries


def collect_conversions(doc, locked, lock_label="uv.lock"):
    """Every path/workspace-sourced dependency, with its locked version.

    *lock_label* names the lock *locked* was read from, so a refusal points at
    the file that failed to resolve the package -- which is not always the one
    beside the manifest (see :func:`resolve_lock`).
    """
    path_sources = detect_uv_path_sources(doc)
    names = path_sourced_names(doc)

    conversions = []
    for normalized in sorted(names):
        declared = names[normalized]
        version = (locked or {}).get(normalized)
        if version is None:
            raise UvPathSourceError(
                f"{declared}: {lock_label} does not resolve this package, so "
                f"there is no locked version to floor at. Run `uv lock` and "
                f"re-run."
            )
        source_kind = next(
            (kind for source, kind in path_sources.items()
             if normalize_pypi_name(source) == normalized),
            None,
        )
        sections = tuple(
            entry["section"]
            for entry in find_dep_entries(doc, {normalized: declared}, SECTIONS_ALL)
        )
        dep_entries, source_entries = count_entries(doc, declared)
        conversions.append(Conversion(
            name=declared,
            locked_version=version,
            source_kind=source_kind,
            sections=sections,
            dep_entries=dep_entries,
            source_entries=source_entries,
        ))
    return conversions


# ---------------------------------------------------------------------------
# The release-first probe
# ---------------------------------------------------------------------------


def probe_published(name, version):
    """Hard-error unless *version* of *name* is published on PyPI.

    Fail-closed: anything that is not a definitive "found" refuses.
    """
    result = query_pypi_release(name, version)
    status = result.get("status")
    if status == "found":
        return
    if status == "not_found":
        raise UvPathSourceError(
            f"{name} {version} is not published on PyPI, so a "
            f"'{name}>={version}' floor would be unsatisfiable for every "
            f"consumer. Release {name} first, then re-run this command."
        )
    raise UvPathSourceError(
        f"{name} {version}: could not determine whether it is published "
        f"({result.get('message') or 'registry probe failed'}). Refusing to "
        f"write a floor on an unanswered probe -- fix the connection to the "
        f"registry and re-run."
    )


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

#: Preview key of the item that records the config-key update.
CONFIG_ITEM_KEY = ".rlsbl/config.json"


def observe(project_root, *, probe=probe_published):
    """Build the plan: one item per dependency, plus the config update."""
    root = str(project_root)
    doc = _read_doc(os.path.join(root, "pyproject.toml"))
    locked, lock = resolve_lock(root)

    conversions = collect_conversions(doc, locked, lock_label=lock.label(root))
    if not conversions:
        return single(VerdictItem(
            key="(project)",
            state="nothing_to_convert",
            summary=(
                "no [tool.uv.sources] path/workspace entry and no direct "
                "path reference in any dependency section."
            ),
        ))

    items = []
    for conv in conversions:
        probe(conv.name, conv.locked_version)
        facts = [
            f"locked version: {conv.locked_version}",
            lock.describe(root),
        ]
        if conv.source_kind is not None:
            facts.append(
                f"[tool.uv.sources].{conv.name}: {conv.source_kind} source"
            )
        for section in conv.sections:
            facts.append(f"[{section}]: declares this dependency")
        facts.append(f"published on PyPI: yes ({conv.locked_version})")

        actions = []
        if conv.dep_entries:
            actions.append(
                f"apply would rewrite {conv.dep_entries} dependency "
                f"entr{'y' if conv.dep_entries == 1 else 'ies'} to "
                f"'{conv.name}{conv.constraint}'."
            )
        if conv.source_entries:
            actions.append(
                f"apply would delete [tool.uv.sources].{conv.name}."
            )
        items.append(VerdictItem(
            key=conv.name,
            state="convert",
            summary=(
                f"{conv.occurrences} entr"
                f"{'y' if conv.occurrences == 1 else 'ies'} -> "
                f"{conv.name}{conv.constraint}"
            ),
            facts=tuple(facts),
            actions=tuple(actions),
            data=conv,
        ))

    config = read_json_config(_project_config(root))
    existing = config.get(CONFIG_KEY)
    declared = set(existing) if isinstance(existing, list) else set()
    additions = sorted({c.name for c in conversions} - declared)
    items.append(VerdictItem(
        key=CONFIG_ITEM_KEY,
        state="declare_floors" if additions else "floors_already_declared",
        summary=(
            f"{CONFIG_KEY} would gain {', '.join(additions)}"
            if additions else
            f"{CONFIG_KEY} already names every converted dependency"
        ),
        facts=(
            f"{CONFIG_KEY} present: {'yes' if isinstance(existing, list) else 'no'}",
        ),
        actions=(
            (f"apply would set {CONFIG_KEY} to "
             f"{sorted(declared | {c.name for c in conversions})}.",)
            if additions else ()
        ),
        data=("config", additions),
    ))
    return Preview(tuple(items))


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_item(item, project_root, applied=None):
    """Apply one item, refusing when its count moved since the preview.

    *applied* is an optional list the caller passes through every item; each
    successful write appends its key, so an abort can name what is already on
    disk (this command has no rollback -- see :mod:`.abort`).
    """
    root = str(project_root)
    if item.data is None:
        return  # the nothing-to-convert item
    if isinstance(item.data, tuple) and item.data[0] == "config":
        wrote = _apply_config(root, item.data[1])
        if wrote and applied is not None:
            applied.append(item.key)
        return

    conv = item.data
    pyproject_path = os.path.join(root, "pyproject.toml")
    doc = _read_doc(pyproject_path)

    dep_entries, source_entries = count_entries(doc, conv.name)
    found = dep_entries + source_entries
    if found != conv.occurrences:
        raise UvPathSourceError(
            f"{conv.name}: the preview counted {conv.occurrences} entr"
            f"{'y' if conv.occurrences == 1 else 'ies'} but the manifest now "
            f"has {found}. The working tree changed between the preview and "
            f"the apply; nothing further has been written. "
            f"{already_written(applied)}"
            f"Re-run with --dry-run, read the plan, and apply again -- a "
            f"re-run re-plans from the manifest as it is now."
        )

    normalized = normalize_pypi_name(conv.name)
    floor_dep_entries(doc, {normalized: conv.locked_version}, SECTIONS_ALL)
    if source_entries:
        remove_uv_sources(doc, [
            name for name in list(detect_uv_path_sources(doc))
            if normalize_pypi_name(name) == normalized
        ])
    effects.atomic_write_text(
        pyproject_path, tomlkit.dumps(doc), preserve_mode=True,
    )
    if applied is not None:
        applied.append(item.key)
    print(
        f"  {conv.name}: floored at {conv.constraint} "
        f"({found} entr{'y' if found == 1 else 'ies'})"
    )


def _apply_config(root, additions):
    """Add *additions* to ``internal_dep_floors``.  True when it wrote."""
    if not additions:
        return False
    from ...config import write_project_config

    config = read_json_config(_project_config(root))
    existing = config.get(CONFIG_KEY)
    declared = set(existing) if isinstance(existing, list) else set()
    write_project_config(CONFIG_KEY, sorted(declared | set(additions)), root)
    print(f"  {CONFIG_KEY}: added {', '.join(additions)}")
    return True


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------


def cmd_uv_path_sources(flags, project_root):
    """``rlsbl rewrite uv-path-sources`` -- path sources become registry floors.

    ``flags["dry-run"]`` -- plan only.
    """
    dry_run = bool(flags.get("dry-run", False))

    applied = []
    reconciler = Reconciler(
        observe=lambda: observe(project_root),
        apply_item=lambda item: apply_item(item, project_root, applied=applied),
        show_keys=True,
    )
    try:
        preview = reconcile(reconciler, dry_run=dry_run)
    except UvPathSourceError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not dry_run:
        converted = [i for i in preview.items if i.state == "convert"]
        if converted:
            print(f"Converted {len(converted)} path-sourced dependency(ies).")
        else:
            print("Nothing to convert: no path or workspace sources declared.")
