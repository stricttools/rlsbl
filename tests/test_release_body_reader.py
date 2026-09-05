"""One reader for a GitHub Release's body, and no second opinion about it.

``rlsbl.release_publication`` owns the whole Release document -- the notes, the
title, the pre-release flag and the released-commit marker -- and
:func:`~rlsbl.release_publication.read_release_body` is the one place the body
is read back off the forge.  Two commands used to build the ``gh release view
--json body`` argv themselves (with ``--jq`` where the module writes ``-q``),
so a change to how the body is fetched had three places to reach.

The first test is a source guard over the tree; the two after it pin that the
commands really route through the module.
"""

import re
from pathlib import Path

from unittest.mock import patch

from rlsbl import release_publication

RLSBL_ROOT = Path(release_publication.__file__).resolve().parent

# A body read is a `gh release view` argv that also asks for the body field.
# A bare `gh release view <tag>` is a different question -- does this Release
# exist -- and is not what this guard is about.
_BODY_READ_RE = re.compile(r'"release",\s*"view"[^\]]*body', re.S)


def test_only_the_publication_module_reads_a_release_body():
    offenders = []
    for path in sorted(RLSBL_ROOT.rglob("*.py")):
        if path.name == "release_publication.py":
            continue
        if "strictspec_gen" in path.parts:
            continue
        if _BODY_READ_RE.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(RLSBL_ROOT)))
    assert offenders == [], (
        "these modules build their own `gh release view --json body` argv; "
        "read the body through release_publication.read_release_body instead: "
        f"{offenders}"
    )


class TestCommandsRouteThroughTheModule:
    """The two folded call sites, each pinned by the function it now calls."""

    def test_deprecate_reads_the_body_through_the_module(self):
        from rlsbl.commands import deprecate

        with patch.object(deprecate, "read_release_body", return_value="old") as m, \
                patch.object(deprecate, "run_gh") as gh:
            deprecate._soft_deprecate("v1.2.3", "broken", None, True)
        assert m.call_args[0][0] == "v1.2.3"
        assert gh.call_count == 0, "a dry run must not edit the Release"

    def test_yank_reads_the_body_through_the_module(self):
        from rlsbl.commands import yank

        with patch.object(yank, "read_release_body", return_value="old") as m, \
                patch.object(yank, "run_gh") as gh:
            yank._mark_github_release("v1.2.3", "broken", None, True)
        assert m.call_args[0][0] == "v1.2.3"
        assert gh.call_count == 0

    def test_an_unreadable_body_is_not_fatal(self):
        """The notice still goes on; a Release with no readable body gets it alone."""
        from rlsbl.commands import deprecate

        with patch.object(
            deprecate, "read_release_body", side_effect=RuntimeError("no gh"),
        ):
            deprecate._soft_deprecate("v1.2.3", "broken", None, True)
