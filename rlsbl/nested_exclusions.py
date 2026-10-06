"""Telling a member's own test runner to skip the members nested inside it.

A nested member's files are that member's, and its tests run in its own suite.
A parent's runner that recursed into it would collect them anyway: pytest,
started in ``sdk/``, collects ``sdk/python/tests`` and fails on that member's
dependencies. So scaffold writes one path-exact exclusion per nested member
into the parent's runner configuration, through the same mechanisms the
scratch directories use (:mod:`rlsbl.scratch_dirs`):

- pytest: ``--ignore=<path>`` in ``[tool.pytest.ini_options] addopts``. pytest
  resolves the path against the directory it starts in, which is the member's
  own directory in rlsbl's runs and in the CI templates; ``norecursedirs``
  would match by basename and could skip an unrelated directory.
- Go needs nothing: the go command never descends into a directory holding
  its own ``go.mod``.
- npm is the same stated gap as for the scratch directories: the runner is
  the project's choice, configured in a file rlsbl neither writes nor reads.

:func:`missing_exclusions` is what the ``nested-member-runner-exclusion``
check reads, so the check and the writer cannot disagree about what is owed.
"""

import os
import shlex

from . import effects
from .scratch_dirs import PYTEST_NORECURSEDIRS

#: The check that refuses a missing exclusion, named in every finding.
CHECK_NAME = "nested-member-runner-exclusion"


def nested_paths_inside(target_dir_abs, nested_dirs):
    """*nested_dirs* that lie inside *target_dir_abs*, relative to it, sorted."""
    base = os.path.realpath(target_dir_abs)
    found = []
    for nested in nested_dirs:
        real = os.path.realpath(nested)
        if real.startswith(base + os.sep):
            found.append(os.path.relpath(real, base).replace(os.sep, "/"))
    return sorted(found)


def pytest_ignore_option(rel):
    """The ``addopts`` token that makes pytest skip *rel*."""
    return f"--ignore={rel}"


def _addopts_tokens(existing):
    if existing is None:
        return []
    if isinstance(existing, str):
        return shlex.split(existing)
    return [str(entry) for entry in existing]


def _read_pyproject_addopts(pyproject):
    import tomllib

    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    return _addopts_tokens(
        data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts")
    )


def missing_exclusions(mechanism, target_dir_abs, nested_rel):
    """``(config file, [missing entries])`` for one target, or None when nothing is owed.

    *nested_rel* are the nested members' paths relative to the target
    directory. A configuration file the mechanism cannot be written into
    (a ``pytest.ini`` outranking ``pyproject.toml``, no ``pyproject.toml``)
    owes every entry, named in that file.
    """
    if not nested_rel:
        return None
    if mechanism == PYTEST_NORECURSEDIRS:
        ini = os.path.join(target_dir_abs, "pytest.ini")
        pyproject = os.path.join(target_dir_abs, "pyproject.toml")
        wanted = [pytest_ignore_option(rel) for rel in nested_rel]
        if os.path.isfile(ini) or not os.path.isfile(pyproject):
            return (ini if os.path.isfile(ini) else pyproject), wanted
        tokens = _read_pyproject_addopts(pyproject)
        missing = [w for w in wanted if w not in tokens]
        return (pyproject, missing) if missing else None
    return None


def apply_nested_member_exclusions(target_paths, nested_dirs, *, dry_run=False):
    """Merge each nested member's exclusion into each target's runner config.

    Args:
        target_paths: ``{target name: directory relative to the project root}``,
            relative to the current directory (the project being scaffolded).
        nested_dirs: absolute directories of the members nested in the project.
        dry_run: report what would change without writing anything.

    Returns ``(created, skipped, warnings)`` in scaffold's vocabulary. Like the
    scratch-directory settings, the files are the project's: merged into,
    never rendered, and never entered in the managed-files registry.
    """
    from .targets import TARGETS

    created, skipped, warnings = [], [], []
    for name in sorted(target_paths):
        target = TARGETS.get(name)
        if target is None:
            continue
        target_dir = target_paths[name] or "."
        nested_rel = nested_paths_inside(os.path.abspath(target_dir), nested_dirs)
        if not nested_rel:
            continue
        handler = _HANDLERS.get(target.scratch_test_exclusion)
        if handler is not None:
            handler(target_dir, nested_rel, created, skipped, warnings, dry_run)
    return created, skipped, warnings


def _rel(target_dir, filename):
    return filename if target_dir == "." else os.path.join(target_dir, filename)


def _apply_pytest(target_dir, nested_rel, created, skipped, warnings, dry_run):
    import tomlkit

    wanted = [pytest_ignore_option(rel) for rel in nested_rel]
    ini_rel = _rel(target_dir, "pytest.ini")
    pyproject_rel = _rel(target_dir, "pyproject.toml")
    if os.path.isfile(ini_rel) or not os.path.isfile(pyproject_rel):
        where = ini_rel if os.path.isfile(ini_rel) else pyproject_rel
        warnings.append(
            f"rlsbl could not write pytest's exclusions for the nested members "
            f"into {where}; add {' '.join(wanted)} to its addopts yourself, or "
            f"the `{CHECK_NAME}` check keeps failing."
        )
        return

    with open(pyproject_rel, encoding="utf-8") as handle:
        original = handle.read()
    doc = tomlkit.parse(original)
    table = doc
    for key in ("tool", "pytest"):
        child = table.get(key)
        if child is None:
            child = tomlkit.table(is_super_table=True)
            table[key] = child
        table = child
    ini_options = table.get("ini_options")
    created_table = ini_options is None
    if created_table:
        ini_options = tomlkit.table()
        table["ini_options"] = ini_options

    existing = ini_options.get("addopts")
    tokens = _addopts_tokens(existing)
    missing = [w for w in wanted if w not in tokens]
    if missing:
        if isinstance(existing, str):
            ini_options["addopts"] = " ".join([existing.strip(), *missing]).strip()
        else:
            ini_options["addopts"] = tokens + missing
    if created_table:
        ini_options.add(tomlkit.nl())

    updated = tomlkit.dumps(doc)
    if created_table and updated.endswith("\n\n") and not original.endswith("\n\n"):
        updated = updated[:-1]
    if updated == original:
        skipped.append((pyproject_rel, "unchanged (pytest nested-member ignores)"))
        return
    if not dry_run:
        effects.atomic_write_text(pyproject_rel, updated)
    created.append((pyproject_rel, "updated (pytest nested-member ignores)"))


_HANDLERS = {
    PYTEST_NORECURSEDIRS: _apply_pytest,
}
