"""The docs directives render their tables with no importable library at all.

A custom directive script is executed by the site generator through a bare
``python3`` with the ``resolve(attrs, config, body)`` contract -- nothing is
installed for it. A directive that imported a rendering library therefore broke
the whole documentation build the moment that library was absent, which is why
``docs/directives/_matrix.py`` carries the Markdown table renderer itself.

Every test here makes ``selfdoc_core`` -- the library the renderer used to come
from -- unimportable before loading anything, so a re-introduced import fails
the suite instead of passing because the package happened to be installed.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DIRECTIVES_DIR = REPO_ROOT / "docs" / "directives"

# Every directive selfdoc.json registers, and whether its output is a table.
TABLE_DIRECTIVES = [
    "target_table",
    "target_axes",
    "feature_matrix",
    "pipeline_table",
    "check_tags",
]
PROSE_DIRECTIVES = ["target_count", "check_count"]


@pytest.fixture(autouse=True)
def _no_selfdoc_core(monkeypatch):
    """Make ``import selfdoc_core`` raise, whatever is installed."""
    monkeypatch.setitem(sys.modules, "selfdoc_core", None)
    monkeypatch.setitem(sys.modules, "selfdoc_core.tables", None)
    with pytest.raises(ImportError):
        importlib.import_module("selfdoc_core")


def _load(name):
    """Load one directive (or ``_matrix``) from docs/directives/ by path."""
    path = DIRECTIVES_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"rlsbl_docs_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(markdown):
    """Split a rendered table into its lines, refusing anything but a table."""
    lines = markdown.splitlines()
    assert len(lines) >= 3, markdown
    return lines


class TestEveryRegisteredDirectiveRenders:
    """All seven directives resolve with the renderer's former library gone."""

    def test_the_directive_list_here_is_the_registered_one(self):
        with open(REPO_ROOT / "selfdoc.json", encoding="utf-8") as f:
            registered = json.load(f)["directives"]
        modules = sorted(Path(p).stem for p in registered.values())
        assert modules == sorted(TABLE_DIRECTIVES + PROSE_DIRECTIVES)

    @pytest.mark.parametrize("directive", TABLE_DIRECTIVES)
    def test_it_renders_a_pipe_table(self, directive):
        output = _load(directive).resolve({}, None, None)
        lines = _rows(output)
        header, separator = lines[0], lines[1]
        assert header.startswith("| ") and header.endswith(" |"), header
        width = header.count(" | ") + 1
        assert separator == "| " + " | ".join(["---"] * width) + " |"
        for line in lines[2:]:
            assert line.startswith("| ") and line.endswith(" |"), line
            assert line.count(" | ") + 1 == width, line

    @pytest.mark.parametrize("directive", PROSE_DIRECTIVES)
    def test_it_renders_a_sentence(self, directive):
        output = _load(directive).resolve({}, None, None)
        assert output.strip().endswith("."), output


class TestTheVendoredRenderer:
    """The renderer's own contract, exercised directly."""

    def test_headers_separator_and_rows(self):
        matrix = _load("_matrix")
        assert matrix.render_markdown_table(["A", "B"], [["1", "2"]]) == (
            "| A | B |\n| --- | --- |\n| 1 | 2 |"
        )

    def test_a_pipe_outside_a_code_span_is_escaped(self):
        matrix = _load("_matrix")
        rendered = matrix.render_markdown_table(["A"], [["x | y"]])
        assert rendered.splitlines()[-1] == "| x \\| y |"

    def test_a_pipe_inside_a_code_span_is_left_alone(self):
        matrix = _load("_matrix")
        rendered = matrix.render_markdown_table(["A"], [["`a | b` and c | d"]])
        assert rendered.splitlines()[-1] == "| `a | b` and c \\| d |"

    def test_an_unclosed_backtick_opens_no_span(self):
        matrix = _load("_matrix")
        rendered = matrix.render_markdown_table(["A"], [["`a | b"]])
        assert rendered.splitlines()[-1] == "| `a \\| b |"

    def test_a_newline_in_a_cell_is_an_error(self):
        matrix = _load("_matrix")
        with pytest.raises(ValueError, match="newline"):
            matrix.render_markdown_table(["A"], [["one\ntwo"]])

    def test_a_short_row_is_padded(self):
        matrix = _load("_matrix")
        rendered = matrix.render_markdown_table(["A", "B"], [["1"]])
        assert rendered.splitlines()[-1] == "| 1 |  |"

    def test_a_row_wider_than_the_headers_is_an_error(self):
        matrix = _load("_matrix")
        with pytest.raises(ValueError, match="only 1 headers"):
            matrix.render_markdown_table(["A"], [["1", "2"]])

    def test_empty_headers_are_an_error(self):
        matrix = _load("_matrix")
        with pytest.raises(ValueError, match="headers must not be empty"):
            matrix.render_markdown_table([], [])

    def test_non_string_cells_are_rendered(self):
        matrix = _load("_matrix")
        rendered = matrix.render_markdown_table(["A"], [[7]])
        assert rendered.splitlines()[-1] == "| 7 |"
