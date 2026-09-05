"""One derivation of "which tree did this path have at this commit?".

The rule -- ``"."`` is ``<rev>^{tree}``, anything else is ``<rev>:<path>`` --
was written out at five sites: the release flow's release-commit writer, the
release-commit remap, ``monorepo extract``, ``monorepo absorb`` and the archive
backfill.  It now lives in :func:`rlsbl.git_util.tree_rev_spec`, and the guard
below keeps it there: the runners may differ, the spelling may not.
"""

import re
from pathlib import Path

import pytest

from rlsbl import git_util
from rlsbl.git_util import tree_rev_spec

RLSBL_ROOT = Path(git_util.__file__).resolve().parent
REPO_ROOT = RLSBL_ROOT.parent

# The spelling as CODE writes it: inside an f-string the braces are doubled,
# so this matches a derivation and not a prose mention of one (a docstring or
# an error message telling an operator what to run spells it with single
# braces).
# prose mention of one. An error message quoting `git rev-parse <commit>^{tree}`
# for an operator to type is guidance, not a derivation, and interpolates
# nothing.
_SPELLING_RE = re.compile(r"\}\^\{\{tree\}\}")


@pytest.mark.parametrize("path,expected", [
    (".", "abc123^{tree}"),
    ("", "abc123^{tree}"),
    ("pkgs/core", "abc123:pkgs/core"),
    ("a b/c", "abc123:a b/c"),
])
def test_the_spelling(path, expected):
    assert tree_rev_spec("abc123", path) == expected


def test_only_git_util_spells_the_tree_rev():
    """No module and no script writes the rule out for itself."""
    offenders = []
    scanned = sorted(RLSBL_ROOT.rglob("*.py")) + sorted(
        (REPO_ROOT / "scripts").glob("*.py")
    )
    for path in scanned:
        if path.name == "git_util.py":
            continue
        if "strictspec_gen" in path.parts:
            continue
        for line_num, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1,
        ):
            # A prose mention (a docstring, an error message telling an
            # operator what to run) is not a second derivation.
            if line.lstrip().startswith("#"):
                continue
            if _SPELLING_RE.search(line) and "tree_rev_spec" not in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_num}")
    assert offenders == [], (
        "these lines spell the tree rev themselves; call "
        f"rlsbl.git_util.tree_rev_spec instead: {offenders}"
    )
