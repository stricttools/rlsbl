"""Tests for unreachable code detection via tree-sitter AST analysis."""

import textwrap

import tree_sitter_python
from tree_sitter import Language, Parser

from rlsbl.lint.python_ast import _check_unreachable_code
from rlsbl.lint.result import LintResult

PY_LANG = Language(tree_sitter_python.language())


def _parse(source: str):
    """Parse Python source and return the tree-sitter tree."""
    parser = Parser(PY_LANG)
    return parser.parse(textwrap.dedent(source).encode("utf-8"))


def _check(source: str, filepath: str = "test.py") -> list[LintResult]:
    """Parse source and run unreachable code detection."""
    tree = _parse(source)
    return _check_unreachable_code(tree, filepath)


class TestCodeAfterReturn:
    """Rule 1: code after unconditional return."""

    def test_code_after_return_detected(self):
        results = _check("""\
            def foo():
                return 1
                x = 2
        """)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "unreachable-code"
        assert r.severity == "error"
        assert "return" in r.message
        assert r.line == 3

    def test_multiple_statements_after_return(self):
        """All statements after the terminator are flagged, not just the first."""
        results = _check("""\
            def foo():
                return 1
                x = 2
                y = 3
                z = 4
        """)
        assert len(results) == 3
        assert results[0].line == 3
        assert results[1].line == 4
        assert results[2].line == 5


class TestCodeAfterRaise:
    """Rule 1: code after unconditional raise."""

    def test_code_after_raise_detected(self):
        results = _check("""\
            def foo():
                raise ValueError("bad")
                x = 2
        """)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "unreachable-code"
        assert "raise" in r.message
        assert r.line == 3


class TestExhaustiveIfElse:
    """Rule 2: code after exhaustive if/elif/else where all branches terminate."""

    def test_all_branches_return(self):
        results = _check("""\
            def foo(x):
                if x > 0:
                    return 1
                else:
                    return -1
                print("unreachable")
        """)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "unreachable-code"
        assert "if/else" in r.message
        assert r.line == 6

    def test_if_elif_else_all_return(self):
        results = _check("""\
            def foo(x):
                if x > 0:
                    return 1
                elif x == 0:
                    return 0
                else:
                    return -1
                print("unreachable")
        """)
        assert len(results) == 1
        assert "if/else" in results[0].message
        assert results[0].line == 8

    def test_if_without_else_not_detected(self):
        """If without else is not exhaustive -- code after it is reachable."""
        results = _check("""\
            def foo(x):
                if x > 0:
                    return 1
                print("reachable")
        """)
        assert len(results) == 0

    def test_if_elif_without_else_not_detected(self):
        """If/elif without else is not exhaustive -- code after it is reachable."""
        results = _check("""\
            def foo(x):
                if x > 0:
                    return 1
                elif x == 0:
                    return 0
                print("reachable")
        """)
        assert len(results) == 0

    def test_if_else_one_branch_missing_return(self):
        """If one branch does not terminate, the if is not exhaustive."""
        results = _check("""\
            def foo(x):
                if x > 0:
                    return 1
                else:
                    pass
                print("reachable")
        """)
        assert len(results) == 0


class TestNormalFlow:
    """Normal code flow should not be flagged."""

    def test_normal_function(self):
        results = _check("""\
            def foo():
                x = 1
                y = 2
                return x + y
        """)
        assert len(results) == 0

    def test_return_at_end(self):
        results = _check("""\
            def foo():
                x = compute()
                return x
        """)
        assert len(results) == 0

    def test_empty_function_body(self):
        results = _check("""\
            def foo():
                pass
        """)
        assert len(results) == 0

    def test_module_level_no_false_positive(self):
        results = _check("""\
            x = 1
            y = 2
            z = x + y
        """)
        assert len(results) == 0


class TestNestedFunctions:
    """Return in inner function does NOT make outer code unreachable."""

    def test_inner_return_does_not_affect_outer(self):
        results = _check("""\
            def outer():
                def inner():
                    return 42
                x = inner()
                return x
        """)
        assert len(results) == 0

    def test_unreachable_inside_nested_function(self):
        """Unreachable code inside a nested function IS detected."""
        results = _check("""\
            def outer():
                def inner():
                    return 42
                    x = 1
                return inner()
        """)
        assert len(results) == 1
        assert results[0].line == 4
        assert "return" in results[0].message


class TestBreakContinue:
    """break/continue as terminators within loop bodies."""

    def test_code_after_break(self):
        results = _check("""\
            def foo():
                for i in range(10):
                    break
                    x = 1
        """)
        assert len(results) == 1
        assert "break" in results[0].message
        assert results[0].line == 4

    def test_code_after_continue(self):
        results = _check("""\
            def foo():
                for i in range(10):
                    continue
                    x = 1
        """)
        assert len(results) == 1
        assert "continue" in results[0].message
        assert results[0].line == 4


class TestExhaustiveIfWithRaise:
    """Exhaustive if/else where branches terminate with raise."""

    def test_all_branches_raise(self):
        results = _check("""\
            def foo(x):
                if x > 0:
                    raise ValueError("positive")
                else:
                    raise TypeError("not positive")
                print("unreachable")
        """)
        assert len(results) == 1
        assert "if/else" in results[0].message

    def test_mixed_return_and_raise(self):
        results = _check("""\
            def foo(x):
                if x > 0:
                    return 1
                else:
                    raise ValueError("bad")
                print("unreachable")
        """)
        assert len(results) == 1
        assert "if/else" in results[0].message
