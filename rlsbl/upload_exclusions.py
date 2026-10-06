"""What scaffold writes so that no upload carries a private path.

The rule itself is :mod:`rlsbl.private_paths`; this module spells it in each
ecosystem's own exclusion syntax, so a newly scaffolded project passes the
refusal without hand edits:

- a hatchling ``pyproject.toml`` gets the entries under ``exclude`` in
  ``[tool.hatch.build.targets.sdist]`` (merged, never replacing the project's
  own entries; ``uv build`` builds the wheel from that sdist);
- the npm ``.npmignore`` renders its private block from the template
  variables below;
- a Go module gets a stub ``go.mod`` in each private directory at its root,
  through the shared template mappings (:func:`go_stub_directories`);
- the pypi CI template embeds :mod:`rlsbl.private_paths` itself.
"""

import os

from . import effects
from .private_paths import (
    PRIVATE_DIRS,
    PRIVATE_ROOT_DIRS,
    exclude_entries,
    python_fix,
    read_build_backend,
)

#: The shared scaffold template for the stub module in a private directory.
PRIVATE_GO_MODULE_TEMPLATE = "private/go.mod.tpl"


def private_go_stub_content():
    """The bytes scaffold writes as the stub ``go.mod`` in a private directory.

    Rendered through scaffold's own template processing with no variables, so
    a template that ever gains one is refused here rather than compared
    unrendered.
    """
    from .commands.init_cmd import process_template

    path = os.path.join(
        os.path.dirname(__file__), "templates", "shared", PRIVATE_GO_MODULE_TEMPLATE,
    )
    with open(path, encoding="utf-8") as f:
        content, unreplaced = process_template(f.read(), {}, path)
    if unreplaced:
        raise ValueError(
            f"{path}: the private go.mod stub template has variables "
            f"({', '.join(unreplaced)}), so its scaffolded content cannot be "
            f"known without a project"
        )
    return content


def npmignore_block():
    """The private-path lines of a scaffolded ``.npmignore``."""
    return "\n".join(exclude_entries())


def ci_check_script(indent):
    """:mod:`rlsbl.private_paths`'s source, indented for a YAML ``run: |`` block.

    The first line is left unindented: the template places the variable at
    the indentation it wants.
    """
    from . import private_paths

    with open(private_paths.__file__, encoding="utf-8") as f:
        lines = f.read().rstrip("\n").split("\n")
    pad = " " * indent
    return "\n".join(
        [lines[0]] + [(pad + line) if line else "" for line in lines[1:]]
    )


def go_stub_directories(project_root):
    """The private directories at *project_root* a Go module needs a stub in.

    ``.rlsbl/`` always, since scaffold itself commits files there; every other
    private directory when git tracks a file in it. The proxy zips only
    committed files, so a directory the project does not have, or one it
    keeps ignored, needs no stub (and a stub in an ignored directory could
    never be committed).
    """
    from .scratch_dirs import SCRATCH_DIR_NAMES

    candidates = [
        name for name in PRIVATE_ROOT_DIRS + PRIVATE_DIRS
        if name != ".rlsbl" and name not in SCRATCH_DIR_NAMES
        and os.path.isdir(os.path.join(str(project_root), name))
    ]
    tracked = set()
    if candidates:
        result = effects.run(
            ["git", "ls-files", "-z", "--", *candidates],
            cwd=str(project_root), capture_output=True, text=True, check=False,
            timeout=60,
        )
        if result.returncode == 0:
            tracked = {rel.split("/", 1)[0] for rel in result.stdout.split("\0") if rel}
    return [".rlsbl"] + [name for name in candidates if name in tracked]


def apply_upload_exclusions(target_paths, *, dry_run=False):
    """Merge the private-path exclusions into each Python target's pyproject.

    Args:
        target_paths: ``{target name: directory relative to the project root}``.
        dry_run: report what would change without writing anything.

    Returns ``(created, skipped, warnings)`` in scaffold's vocabulary, like
    :func:`rlsbl.scratch_dirs.apply_scratch_test_exclusions`: the pyproject is
    the project's own file, merged into and never registered as managed.
    """
    created, skipped, warnings = [], [], []
    target_dir = target_paths.get("pypi")
    if target_dir is None:
        return created, skipped, warnings
    target_dir = target_dir or "."
    rel = "pyproject.toml" if target_dir == "." else os.path.join(target_dir, "pyproject.toml")
    if not os.path.isfile(rel):
        warnings.append(
            f"no {rel} to write the sdist exclusions into, so the Python upload "
            f"would carry any private path it finds."
        )
        return created, skipped, warnings
    backend = read_build_backend(rel)
    if not backend.startswith("hatchling"):
        example = python_fix(backend, "sdist", "todo/plan.md", "/todo/", "todo")
        warnings.append(
            f"{rel} builds with {backend or 'the default build backend'}, and "
            f"rlsbl writes sdist exclusions for hatchling only. Exclude every "
            f"private path from the sdist by hand -- for todo/, {example} -- "
            f"or CI refuses the upload: {', '.join(exclude_entries())}."
        )
        return created, skipped, warnings
    _merge_hatch_sdist_exclude(rel, created, skipped, dry_run)
    return created, skipped, warnings


def _merge_hatch_sdist_exclude(rel, created, skipped, dry_run):
    import tomlkit

    with open(rel, "r", encoding="utf-8") as handle:
        original = handle.read()
    doc = tomlkit.parse(original)

    table = doc
    created_table = False
    for key in ("tool", "hatch", "build", "targets", "sdist"):
        child = table.get(key)
        if child is None:
            last = key == "sdist"
            child = tomlkit.table(is_super_table=not last)
            table[key] = child
            created_table = created_table or last
        table = child
    existing = table.get("exclude")
    entries = [] if existing is None else [str(entry) for entry in existing]
    missing = [entry for entry in exclude_entries() if entry not in entries]
    if missing:
        array = tomlkit.array()
        array.multiline(True)
        for entry in entries + missing:
            array.append(entry)
        table["exclude"] = array
    if created_table:
        table.add(tomlkit.nl())

    updated = tomlkit.dumps(doc)
    if created_table and updated.endswith("\n\n") and not original.endswith("\n\n"):
        updated = updated[:-1]
    if updated == original:
        skipped.append((rel, "unchanged (hatch sdist exclude)"))
        return
    if not dry_run:
        effects.atomic_write_text(rel, updated)
    created.append((rel, "updated (hatch sdist exclude)"))

