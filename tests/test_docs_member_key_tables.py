"""The documented member surface is bound to ``MEMBER_KEYS``.

``rlsbl.workspace.MEMBER_KEYS`` is the one declared authority for what a
``[[projects]]`` member table may carry: the loader refuses against it,
``save_workspace`` refuses to write outside it, and ``WorkspaceProject``
exposes one accessor per key. Two documents restated that set in prose and
neither was tested, so both drifted -- the monorepo guide's field table
documented a ``target`` key the loader now refuses outright (following the
documentation broke the workspace) while omitting three keys that exist.

These tests bind both restatements to the constant, so a member key added,
removed or renamed in the code fails here instead of quietly rotting the
reference pages.
"""

import re
from pathlib import Path

from rlsbl.workspace import MEMBER_KEYS


DOCS = Path(__file__).resolve().parents[1] / "docs"
MONOREPO = DOCS / "monorepo.md"
CONFIGURATION = DOCS / "configuration.md"

#: Keys the loader refuses BY NAME with their own remedy. They are not member
#: keys, and neither document may present one as a field to declare.
RETIRED_KEYS = ("watch", "subtree_remote", "dev_node")


def _section(path, heading):
    """The body of a '### <heading>' or '## <heading>' section."""
    text = path.read_text(encoding="utf-8")
    for level in ("###", "##"):
        marker = f"\n{level} {heading}\n"
        if marker in text:
            rest = text[text.index(marker) + 1:]
            end = rest.find(f"\n{level} ", 1)
            return rest if end == -1 else rest[:end]
    raise AssertionError(f"{path.name} has no '{heading}' section")


def _field_rows(body):
    """The key in the leading backtick cell of each table row."""
    return {
        m.group(1)
        for m in re.finditer(r"^\| `([a-z_]+)` \|", body, re.M)
    }


class TestTheProjectFieldsTable:
    """docs/monorepo.md's field table IS the member surface."""

    def test_it_documents_exactly_the_member_keys(self):
        documented = _field_rows(_section(MONOREPO, "Project fields"))
        assert documented == set(MEMBER_KEYS), (
            f"docs/monorepo.md's project-fields table and MEMBER_KEYS "
            f"disagree: {sorted(documented ^ set(MEMBER_KEYS))}"
        )

    def test_it_documents_no_retired_key_as_a_field(self):
        documented = _field_rows(_section(MONOREPO, "Project fields"))
        for key in RETIRED_KEYS:
            assert key not in documented, (
                f"docs/monorepo.md documents the retired member key '{key}' "
                f"as a field to declare; the loader refuses it"
            )


class TestThePolicedSurfacesTable:
    """docs/configuration.md's known-key list IS the member surface."""

    def _member_row(self):
        body = _section(CONFIGURATION, "Policed configuration surfaces")
        for line in body.splitlines():
            if line.startswith("| `.rlsbl-monorepo/workspace.toml` — a `[[projects]]`"):
                return line
        raise AssertionError(
            "docs/configuration.md lost its member-table row"
        )

    def test_the_listed_keys_are_exactly_the_member_keys(self):
        row = self._member_row()
        listed = set(re.findall(r"`([a-z_]+)`", row.split("MEMBER_KEYS")[1]))
        assert listed == set(MEMBER_KEYS), (
            f"docs/configuration.md's known-key list and MEMBER_KEYS "
            f"disagree: {sorted(listed ^ set(MEMBER_KEYS))}"
        )

    def test_the_retired_keys_are_named_without_a_count(self):
        """A hand-typed count of the retired keys is one more thing to drift."""
        body = _section(CONFIGURATION, "Policed configuration surfaces")
        sentence = next(
            line for line in body.splitlines()
            if "retired member keys" in line
        )
        for key in RETIRED_KEYS:
            assert f"`{key}`" in sentence, sentence
        for count in ("One", "Two", "Three", "Four", "Five"):
            assert not sentence.startswith(count), (
                f"the retired-keys sentence counts them: {sentence!r}"
            )
