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


def _documented_member_blocks():
    """Every ``[[projects]]`` table declared in a docs code block.

    Yields ``(page, line number, {key: ...})``. The scan is over the whole
    docs tree rather than one page: an example is a thing readers copy, and a
    copied member table the loader refuses is the same defect wherever it sits.
    """
    for page in sorted(DOCS.rglob("*.md")):
        lines = page.read_text(encoding="utf-8").splitlines()
        in_block = False
        keys = None
        start = 0
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_block = not in_block
                if keys is not None:
                    yield page, start, keys
                    keys = None
                continue
            if not in_block:
                continue
            if stripped == "[[projects]]":
                if keys is not None:
                    yield page, start, keys
                keys, start = {}, number
                continue
            if stripped.startswith("[") or not stripped:
                if keys is not None:
                    yield page, start, keys
                    keys = None
                continue
            if keys is not None and "=" in stripped:
                keys[stripped.split("=")[0].strip()] = stripped
        if keys is not None:
            yield page, start, keys


class TestTheDocumentedExamples:
    """A member table a reader copies must be one the loader accepts."""

    def test_every_documented_member_block_declares_known_keys_only(self):
        offenders = []
        for page, number, keys in _documented_member_blocks():
            for key in keys:
                if key not in MEMBER_KEYS:
                    offenders.append(f"{page.name}:{number} declares '{key}'")
        assert not offenders, (
            "documented [[projects]] examples the loader would refuse: "
            + "; ".join(offenders)
        )

    def test_the_scan_finds_the_examples_it_is_guarding(self):
        """A scan that matched nothing would pass the test above forever."""
        found = list(_documented_member_blocks())
        assert found, "no [[projects]] example found in docs/"


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
