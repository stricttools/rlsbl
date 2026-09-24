"""The scratch directories every scaffolded project carries at its root.

``experiments/`` holds throwaway probes -- prototypes, produced repositories,
captures, one-off outputs, anything written to find something out rather than
to ship.  ``screenshots/`` holds the mid-work screenshots taken to look at a
piece of work while verifying it; it is not a home for published images, which
belong in a committed ``assets/``.

Each directory carries a committed ``.gitignore`` whose whole content is::

    *
    !.gitignore

That spelling is what makes the directory exist in a fresh clone -- tooling may
rely on it being there -- while nothing inside it can be committed by accident.
A line in the repository's root ``.gitignore`` would leave the directory absent
after a clone instead, which is the opposite of the point.

Repository-integrity walks (dead-module detection, the import scanners, the
linters, the ldflags Go source reader, the unregistered-project scan)
deliberately ignore ``.gitignore``, so a throwaway git repository or a stray
source file inside a scratch directory would otherwise turn a check red.  Every
such walk prunes these names, and this module is the one place that spells
them.

The pruning is scoped to the PROJECT ROOT, matching where scaffold creates
them: a directory of the same name nested deeper inside a project is an
ordinary source directory and is walked normally.  In a workspace each member
is walked with its own directory as the root, so every member's own scratch
directories are pruned.

Pruning rlsbl's own walks keeps rlsbl's checks green and says nothing to the
PROJECT's test runner, which would still collect a ``test_*.py`` or build a
``.go`` file planted in a scratch directory.  So scaffold also writes the one
setting each ecosystem's runner honours, through the mechanism each target
declares; the vocabulary and the writers live in the second half of this
module.
"""

import json
import os
import re

from . import effects

#: The scratch directory names, at the root of every scaffolded project.
SCRATCH_DIR_NAMES = ("experiments", "screenshots")

#: The shared scaffold template rendered into each scratch directory.
SCRATCH_GITIGNORE_TEMPLATE = "scratch/gitignore.tpl"

#: The shared scaffold template that makes a scratch directory its own Go
#: module, and the filename it is rendered to.
SCRATCH_GO_MODULE_TEMPLATE = "scratch/go.mod.tpl"
SCRATCH_GO_MODULE_FILENAME = "go.mod"

# --------------------------------------------------------------------------
# Telling an ecosystem's own test runner to skip the scratch directories.
#
# Pruning the directories from rlsbl's OWN walks keeps rlsbl's checks green; it
# says nothing to the project's test runner. A `test_*.py` planted in
# `experiments/` is still collected by a bare `pytest`, and a `.go` file there
# is still built by `go test ./...` -- so a half-finished probe breaks a suite
# it has nothing to do with, which is the opposite of what a disposable scratch
# directory is for.
#
# There is no cross-ecosystem mechanism: each runner has its own, or none. The
# vocabulary below is closed, every target declares one member of it (the
# ``scratch_test_exclusion`` support axis), and this module is the one place
# that spells what each member means.
# --------------------------------------------------------------------------

#: The ecosystem's test runner collects only from directories the project
#: declares (a Maven test source set, a Swift package target, a ``test/``
#: convention), so a file in a scratch directory is never reached.
NO_TEST_RUNNER_RECURSION = "no-test-runner-recursion"

#: pytest recurses from its rootdir, and prunes the directory basename patterns
#: named by the ``norecursedirs`` ini option.
PYTEST_NORECURSEDIRS = "pytest-norecursedirs"

#: The go command never descends into a directory that declares its own module,
#: so each scratch directory carries a scaffolded ``go.mod``.
GO_NESTED_MODULE = "go-nested-module"

#: ``deno test`` skips every path named by the top-level ``exclude`` array in
#: the project's Deno configuration file.
DENO_CONFIG_EXCLUDE = "deno-config-exclude"

#: The project's own manifest names the test runner (an npm ``test`` script
#: runs whichever of jest, vitest, mocha or ``node --test`` the project chose),
#: each with a configuration file of its own that rlsbl neither writes nor
#: reads. rlsbl cannot write the setting without first taking ownership of a
#: file it does not own, so it writes nothing and this declaration is where the
#: gap is stated.
RUNNER_CHOSEN_BY_PROJECT = "runner-chosen-by-project"

#: The closed vocabulary. A target declaring anything else is an error.
SCRATCH_TEST_EXCLUSION_MECHANISMS = (
    NO_TEST_RUNNER_RECURSION,
    PYTEST_NORECURSEDIRS,
    GO_NESTED_MODULE,
    DENO_CONFIG_EXCLUDE,
    RUNNER_CHOSEN_BY_PROJECT,
)

#: pytest's own default ``norecursedirs``. Setting the option REPLACES the
#: default rather than adding to it, so a project that had no setting is given
#: the default back alongside the scratch directories -- otherwise scaffolding
#: would start collecting from ``build/``, ``dist/`` and ``node_modules/``.
#: ``tests/test_scratch_test_runners.py`` asks the installed pytest for its own
#: default and fails when this copy drifts from it.
PYTEST_DEFAULT_NORECURSEDIRS = (
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
)


def scratch_mechanisms(target_names):
    """Return the exclusion mechanisms declared by *target_names*.

    Unknown names are ignored: the caller passes whatever the project's config
    and the scaffold invocation name between them, which may include a target
    this rlsbl does not register.
    """
    from .targets import TARGETS

    return frozenset(
        TARGETS[name].scratch_test_exclusion
        for name in target_names
        if name in TARGETS
    )


def scratch_template_mappings(mechanisms=()):
    """Return the shared scaffold mappings for the scratch directories.

    The ``.gitignore`` mapping is the same for every project. A project whose
    targets declare :data:`GO_NESTED_MODULE` also gets a ``go.mod`` in each
    directory, which is what keeps the go command out of it.
    """
    mappings = [
        {"template": SCRATCH_GITIGNORE_TEMPLATE, "target": f"{name}/.gitignore"}
        for name in SCRATCH_DIR_NAMES
    ]
    if GO_NESTED_MODULE in mechanisms:
        mappings.extend(
            {
                "template": SCRATCH_GO_MODULE_TEMPLATE,
                "target": f"{name}/{SCRATCH_GO_MODULE_FILENAME}",
            }
            for name in SCRATCH_DIR_NAMES
        )
    return mappings


#: The files scaffold itself commits into a scratch directory.
_SCAFFOLD_OWNED_SCRATCH_FILES = frozenset({".gitignore", SCRATCH_GO_MODULE_FILENAME})


def tracked_scratch_files(project_dir="."):
    """The files git tracks inside *project_dir*'s scratch directories,
    other than the ones scaffold itself commits there, sorted.

    Outside a git work tree nothing is tracked, and the answer is empty.
    """
    result = effects.run(
        ["git", "ls-files", "-z", "--", *SCRATCH_DIR_NAMES],
        cwd=project_dir, capture_output=True, text=True, check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return []
    found = []
    for rel in result.stdout.split("\0"):
        if not rel:
            continue
        parts = rel.split("/")
        if len(parts) == 2 and parts[1] in _SCAFFOLD_OWNED_SCRATCH_FILES:
            continue
        found.append(rel)
    return sorted(found)


def refuse_tracked_scratch_files(project_dir="."):
    """Refuse to scaffold while a scratch directory holds tracked files.

    Scaffold writes an ignore-everything ``.gitignore`` into each scratch
    directory; committed files under it would stay tracked while every new
    file beside them is silently ignored, and a directory meant for throwaway
    output would keep shipping them.

    Raises:
        ConfigError: listing every such file and the remedies.
    """
    from .errors import ConfigError

    files = tracked_scratch_files(project_dir)
    if not files:
        return
    listing = "\n".join(f"  {rel}" for rel in files)
    dirs = sorted({rel.split("/", 1)[0] for rel in files})
    example_dest = "assets/" + files[0].split("/", 1)[1]
    raise ConfigError(
        f"{' and '.join(d + '/' for d in dirs)} "
        f"{'is a scratch directory' if len(dirs) == 1 else 'are scratch directories'} "
        f"(scaffold makes git ignore everything inside), but git tracks these "
        f"files there:\n{listing}\n"
        f"Move them out before scaffolding -- images a reader sees belong in "
        f"the committed assets/ directory, e.g. `mkdir -p "
        f"{os.path.dirname(example_dest)} && git mv {files[0]} {example_dest}` "
        f"-- or, if the directory is not "
        f"scratch space at all, rename the directory (`git mv {dirs[0]} "
        f"<another name>`). Commit the move and re-run rlsbl scaffold."
    )


def scratch_template_vars(mechanisms=()):
    """Return the template variables the scratch ``.gitignore`` renders from.

    The ignore file un-ignores exactly the files scaffold itself commits into
    the directory. That is only ``.gitignore`` until a Go project's module
    marker joins it, and a marker the ignore file swallowed would be absent
    from a fresh clone -- which is where a planted probe does its damage.
    """
    return {"scratchGoModule": "1" if GO_NESTED_MODULE in mechanisms else ""}


def apply_scratch_test_exclusions(target_paths, *, dry_run=False):
    """Write each target's scratch-directory exclusion into its own config file.

    Args:
        target_paths: ``{target name: directory relative to the project root}``.
        dry_run: report what would change without writing anything.

    Returns ``(created, skipped, warnings)`` in scaffold's own vocabulary:
    ``(path, status)`` pairs for the files written and for the files already
    carrying the setting, plus warning strings.

    The files touched here (``pyproject.toml``, ``deno.json``) are the
    project's, not scaffold's: they are merged into rather than rendered, they
    never enter the managed-files registry, and everything around the one
    setting is byte-preserved. :data:`GO_NESTED_MODULE` is absent from this
    dispatch on purpose -- its mechanism is a scaffolded FILE, so it rides the
    ordinary template mappings instead.
    """
    from .targets import TARGETS

    created, skipped, warnings = [], [], []
    for name in sorted(target_paths):
        target = TARGETS.get(name)
        if target is None:
            continue
        handler = _MECHANISM_HANDLERS.get(target.scratch_test_exclusion)
        if handler is None:
            continue
        handler(target_paths[name] or ".", created, skipped, warnings, dry_run)
    return created, skipped, warnings


def _rel(target_dir, filename):
    """The project-root-relative path of *filename* inside *target_dir*."""
    return filename if target_dir == "." else os.path.join(target_dir, filename)


def _apply_pytest_norecursedirs(target_dir, created, skipped, warnings, dry_run):
    """Merge the scratch directories into ``norecursedirs`` in pyproject.toml."""
    import tomlkit

    wanted = " ".join(PYTEST_DEFAULT_NORECURSEDIRS + SCRATCH_DIR_NAMES)
    ini_rel = _rel(target_dir, "pytest.ini")
    if os.path.isfile(ini_rel):
        warnings.append(
            f"{ini_rel} outranks pyproject.toml as pytest's configuration file, "
            f"so rlsbl wrote no norecursedirs setting. Add "
            f"'norecursedirs = {wanted}' under [pytest] in {ini_rel} yourself, "
            f"or delete {ini_rel} and re-run scaffold."
        )
        return

    pyproject_rel = _rel(target_dir, "pyproject.toml")
    if not os.path.isfile(pyproject_rel):
        warnings.append(
            f"no {pyproject_rel} to write pytest's norecursedirs into, so a test "
            f"file left in {' or '.join(SCRATCH_DIR_NAMES)} would be collected."
        )
        return

    with open(pyproject_rel, "r", encoding="utf-8") as handle:
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

    existing = ini_options.get("norecursedirs")
    if existing is None:
        patterns = list(PYTEST_DEFAULT_NORECURSEDIRS)
    elif isinstance(existing, str):
        patterns = existing.split()
    else:
        patterns = [str(entry) for entry in existing]
    missing = [name for name in SCRATCH_DIR_NAMES if name not in patterns]
    if missing or existing is None:
        ini_options["norecursedirs"] = patterns + missing
    if created_table:
        # A table tomlkit inserts carries no trailing blank line, so the next
        # table header would follow on the very next line.
        ini_options.add(tomlkit.nl())

    updated = tomlkit.dumps(doc)
    if created_table and updated.endswith("\n\n") and not original.endswith("\n\n"):
        # ...unless the new table ends the file, where the blank line would
        # be a trailing one.
        updated = updated[:-1]
    if updated == original:
        skipped.append((pyproject_rel, "unchanged (pytest norecursedirs)"))
        return
    if not dry_run:
        effects.atomic_write_text(pyproject_rel, updated)
    created.append((pyproject_rel, "updated (pytest norecursedirs)"))


def _apply_deno_exclude(target_dir, created, skipped, warnings, dry_run):
    """Merge the scratch directories into ``exclude`` in the Deno config."""
    json_rel = _rel(target_dir, "deno.json")
    jsonc_rel = _rel(target_dir, "deno.jsonc")
    listing = ", ".join(f'"{name}"' for name in SCRATCH_DIR_NAMES)
    if not os.path.isfile(json_rel):
        if os.path.isfile(jsonc_rel):
            warnings.append(
                f"{jsonc_rel} carries comments that a rewrite would lose, so "
                f"rlsbl wrote no exclude entries. Add {listing} to the "
                f'top-level "exclude" array in {jsonc_rel} yourself.'
            )
        else:
            warnings.append(
                f"no {json_rel} to write deno's exclude entries into, so a test "
                f"file left in {' or '.join(SCRATCH_DIR_NAMES)} would be collected."
            )
        return

    with open(json_rel, "r", encoding="utf-8") as handle:
        original = handle.read()
    data = json.loads(original)
    exclude = data.get("exclude")
    if exclude is None:
        exclude = []
        data["exclude"] = exclude
    elif not isinstance(exclude, list):
        warnings.append(
            f'the "exclude" key in {json_rel} is not an array, so rlsbl left it '
            f"alone. Make it an array containing {listing}."
        )
        return

    missing = [name for name in SCRATCH_DIR_NAMES if name not in exclude]
    if not missing:
        skipped.append((json_rel, "unchanged (deno exclude)"))
        return
    exclude.extend(missing)

    # Indent and trailing newline are read off the file, matching how the deno
    # target rewrites a version, so the diff is the exclude entries alone.
    indent_match = re.search(r'^( +|\t+)"', original, re.MULTILINE)
    indent = indent_match.group(1) if indent_match else "  "
    trailing = "\n" if original.endswith("\n") else ""
    updated = json.dumps(data, indent=indent, ensure_ascii=False) + trailing
    if not dry_run:
        effects.atomic_write_text(json_rel, updated)
    created.append((json_rel, "updated (deno exclude)"))


#: One handler per mechanism that merges a setting into a project-owned file.
_MECHANISM_HANDLERS = {
    PYTEST_NORECURSEDIRS: _apply_pytest_norecursedirs,
    DENO_CONFIG_EXCLUDE: _apply_deno_exclude,
}


def scratch_dir_paths(root):
    """Return the resolved absolute paths of *root*'s own scratch directories."""
    return frozenset(
        os.path.realpath(os.path.join(root, name)) for name in SCRATCH_DIR_NAMES
    )


def prune_scratch_dirs(root, dirpath, dirnames):
    """Drop *root*'s scratch directories from an ``os.walk`` *dirnames*, in place.

    Args:
        root: the root of the walk -- the project (or module) being examined.
        dirpath: the directory ``os.walk`` is currently visiting.
        dirnames: that directory's subdirectory names, pruned in place.

    Comparison is by resolved path rather than by name, so only the scratch
    directories belonging to *root* are pruned.
    """
    scratch = scratch_dir_paths(root)
    if not scratch:
        return
    dirnames[:] = [
        name for name in dirnames
        if os.path.realpath(os.path.join(dirpath, name)) not in scratch
    ]


def is_scratch_dir_name(name):
    """True when *name* is one of the scratch directory names."""
    return name in SCRATCH_DIR_NAMES
