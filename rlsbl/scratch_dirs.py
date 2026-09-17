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
"""

import os

#: The scratch directory names, at the root of every scaffolded project.
SCRATCH_DIR_NAMES = ("experiments", "screenshots")

#: The shared scaffold template rendered into each scratch directory.
SCRATCH_GITIGNORE_TEMPLATE = "scratch/gitignore.tpl"


def scratch_template_mappings():
    """Return the target-independent scaffold mappings for the scratch dirs.

    One template, one mapping per directory: the file's content does not vary
    by ecosystem, and neither does the convention.
    """
    return [
        {"template": SCRATCH_GITIGNORE_TEMPLATE, "target": f"{name}/.gitignore"}
        for name in SCRATCH_DIR_NAMES
    ]


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
