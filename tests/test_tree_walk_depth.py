"""Every tree-sitter walker handles a syntax tree far deeper than Python's
recursion limit.

Generated code nests arbitrarily deep: a protobuf descriptor written as one
string concatenation of thousands of terms parses into a left-leaning binary
expression thousands of levels deep.  A walker that recursed once per level
crashed ``rlsbl rewrite go-module-path`` with RecursionError on such a file.
Each case below builds a tree at least :data:`DEPTH` levels deep, well past the
default limit of 1000 frames, and asserts the walker both finishes and still
reports what sits in, beside, or after the deep expression.
"""

import sys

import pytest
import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
from tree_sitter import Language, Parser

from rlsbl.lint import go_ast, npm_ast, python_ast
from rlsbl.lint.config import LanguageLintConfig
from rlsbl.lint.tree_walk import SourceParseError, iter_preorder, naming_source

DEPTH = 2000

GO = Language(tree_sitter_go.language())
JS = Language(tree_sitter_javascript.language())
PY = Language(tree_sitter_python.language())


def _parse(lang, source):
    return Parser(lang).parse(source.encode("utf-8"))


def _chain(first, term, sep=" + "):
    """``first + term + term + ...`` with DEPTH terms: a DEPTH-deep tree
    whose deepest leaf is *first*."""
    return first + sep + sep.join([term] * DEPTH)


def _depth(tree):
    """The tree's depth, measured iteratively."""
    deepest = 0
    stack = [(tree.root_node, 1)]
    while stack:
        node, d = stack.pop()
        deepest = max(deepest, d)
        stack.extend((c, d + 1) for c in node.children)
    return deepest


GO_CHAIN = _chain("fmt.Println(1)", '"x"')
JS_REQUIRE_CHAIN = _chain("require('leftpad')", "'x'")
JS_CONSOLE_CHAIN = _chain("console.log(1)", "'x'")
PY_CHAIN = _chain("print(1)", "'x'")

GO_DEEP = (
    'package p\n\nimport "os"\n\n'
    "func main() {\n"
    f"\tx := {GO_CHAIN}\n"
    "\tos.Stdout.Write(nil)\n"
    "}\n"
)

JS_DEEP = (
    f"const s = {JS_REQUIRE_CHAIN};\n"
    f"const t = {JS_CONSOLE_CHAIN};\n"
    "import('late');\n"
)

PY_DEEP = (
    "import os\n\n"
    "def f():\n"
    f"    s = {PY_CHAIN}\n"
    "    return s\n"
    "    unreachable = 1\n"
)


def _go_forbidden():
    tree = _parse(GO, GO_DEEP)
    found = go_ast._check_forbidden_imports(
        tree, "deep.go", LanguageLintConfig(forbidden_imports=["os"]))
    assert [r.message for r in found] == ["Library imports forbidden package 'os'"]
    return tree


def _go_stdout():
    tree = _parse(GO, GO_DEEP)
    found = go_ast._check_stdout(tree, "deep.go", LanguageLintConfig())
    assert [r.message for r in found] == [
        "Library calls fmt.Println()", "Library writes to os.Stdout",
    ]
    return tree


def _go_entry_points():
    tree = _parse(GO, GO_DEEP.replace("package p", "package main"))
    found = go_ast._check_entry_points(tree, "deep.go", LanguageLintConfig())
    assert [r.rule for r in found] == ["entry-point"]
    return tree


def _go_scan_imports(tmp_path):
    path = tmp_path / "deep.go"
    path.write_text(GO_DEEP)
    assert go_ast.scan_imports(str(path)) == [("os", str(path), 3)]
    return _parse(GO, GO_DEEP)


def _npm_imports():
    tree = _parse(JS, JS_DEEP)
    found = npm_ast._collect_all_imports(tree, "deep.js")
    assert {pkg for pkg, _f, _l, _g in found} == {"leftpad", "late"}
    return tree


def _npm_stdout():
    tree = _parse(JS, JS_DEEP)
    found = npm_ast._check_stdout(tree, "deep.js", LanguageLintConfig())
    assert [r.message for r in found] == ["Library calls console.log()"]
    return tree


def _python_imports():
    tree = _parse(PY, PY_DEEP)
    found = python_ast._collect_all_imports(tree, "deep.py")
    assert {r.full_path for r in found} == {"os"}
    return tree


def _python_stdout():
    tree = _parse(PY, PY_DEEP)
    found = python_ast._check_stdout(tree, "deep.py", LanguageLintConfig())
    assert [r.message for r in found] == ["Library calls print()"]
    return tree


def _python_unreachable():
    tree = _parse(PY, PY_DEEP)
    found = python_ast._check_unreachable_code(tree, "deep.py")
    assert [(r.line, r.message) for r in found] == [
        (6, "unreachable code after return"),
    ]
    return tree


WALKERS = {
    "go _check_forbidden_imports": _go_forbidden,
    "go _check_stdout": _go_stdout,
    "go _check_entry_points": _go_entry_points,
    "go scan_imports": _go_scan_imports,
    "npm _collect_all_imports": _npm_imports,
    "npm _check_stdout": _npm_stdout,
    "python _collect_all_imports": _python_imports,
    "python _check_stdout": _python_stdout,
    "python _check_unreachable_code": _python_unreachable,
}


@pytest.mark.parametrize("name", sorted(WALKERS))
def test_walker_handles_a_tree_deeper_than_the_recursion_limit(name, tmp_path):
    walker = WALKERS[name]
    args = (tmp_path,) if name == "go scan_imports" else ()
    tree = walker(*args)
    assert _depth(tree) > DEPTH
    assert _depth(tree) > sys.getrecursionlimit()


def test_always_terminates_handles_deep_if_else_nesting():
    """``_always_terminates`` follows if/else nesting; each level cost
    several frames when it recursed."""
    levels = 600
    lines = ["def f(a):"]
    for i in range(levels):
        pad = " " * (i + 1)
        lines += [f"{pad}if a:", f"{pad} return {i}", f"{pad}else:"]
    lines += [" " * (levels + 1) + "return -1", " x = 1"]
    tree = _parse(PY, "\n".join(lines) + "\n")
    found = python_ast._check_unreachable_code(tree, "nested.py")
    assert [(r.line, r.message) for r in found] == [
        (len(lines), "unreachable code after if/else"),
    ]


def test_iter_preorder_is_document_order_and_prunes():
    tree = _parse(PY, "a = 1\nb = 2\n")

    def recursive(node, out):
        out.append((node.type, node.start_byte))
        for child in node.children:
            recursive(child, out)
        return out

    assert [(n.type, n.start_byte) for n in iter_preorder(tree.root_node)] == \
        recursive(tree.root_node, [])

    pruned = [n.type for n in iter_preorder(
        tree.root_node, descend=lambda n: n.type != "expression_statement")]
    assert pruned == ["module", "expression_statement", "expression_statement"]


def test_a_failure_while_analyzing_names_the_file():
    with pytest.raises(SourceParseError) as info:
        with naming_source("gen/descriptor.pb.go"):
            raise RecursionError("maximum recursion depth exceeded")
    assert str(info.value).startswith("gen/descriptor.pb.go: could not be parsed: ")
    assert "RecursionError" in str(info.value)
