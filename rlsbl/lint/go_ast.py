"""Go linter using tree-sitter AST parsing to detect library boundary violations including forbidden imports and stdout/logging usage."""

import tree_sitter_go
from tree_sitter import Language, Parser

from .config import LanguageLintConfig
from .result import LintResult
from .utils import walk_source_files

GO_LANG = Language(tree_sitter_go.language())


def _make_parser():
    parser = Parser(GO_LANG)
    return parser


def _node_line(node):
    """Return 1-based line number for a tree-sitter node."""
    return node.start_point[0] + 1


def _check_forbidden_imports(tree, filepath, config):
    """Walk AST for import_declaration nodes (single and grouped)."""
    results = []
    forbidden = frozenset(config.forbidden_imports)

    def _walk(node):
        if node.type == "import_declaration":
            # Both single and grouped imports contain import_spec children
            for child in node.children:
                _check_import_spec(child)
            return
        for child in node.children:
            _walk(child)

    def _check_import_spec(node):
        if node.type == "import_spec":
            # The import path is an interpreted_string_literal child
            for child in node.children:
                if child.type == "interpreted_string_literal":
                    # Extract content from the string literal
                    content = _extract_string_content(child)
                    if content in forbidden:
                        results.append(LintResult(
                            file=filepath,
                            line=_node_line(node),
                            rule="forbidden-import",
                            severity="error",
                            message=f"Library imports forbidden package '{content}'",
                        ))
        elif node.type == "import_spec_list":
            # Grouped import: recurse into children
            for child in node.children:
                _check_import_spec(child)

    _walk(tree.root_node)
    return results


def _extract_string_content(string_node):
    """Extract the text content from an interpreted_string_literal node."""
    for child in string_node.children:
        if child.type == "interpreted_string_literal_content":
            return child.text.decode("utf-8")
    # Fallback: strip quotes from the full text
    text = string_node.text.decode("utf-8")
    return text.strip('"')


def _check_stdout(tree, filepath, config):
    """Detect fmt.Print*, fmt.Fprint* to os.Stdout, and os.Stdout.Write() calls."""
    results = []
    ignore = set(config.stdout_ignore)

    def _walk(node):
        if node.type == "call_expression":
            func = node.children[0] if node.children else None
            if func and func.type == "selector_expression":
                obj = func.children[0] if func.children else None
                field = None
                for child in func.children:
                    if child.type == "field_identifier":
                        field = child

                if obj and field:
                    # fmt.Print, fmt.Printf, fmt.Println
                    if (obj.type == "identifier"
                            and obj.text == b"fmt"
                            and "fmt" not in ignore):
                        fname = field.text.decode("utf-8")
                        if fname in ("Print", "Printf", "Println"):
                            results.append(LintResult(
                                file=filepath,
                                line=_node_line(node),
                                rule="stdout",
                                severity="error",
                                message=f"Library calls fmt.{fname}()",
                            ))

                    # os.Stdout.Write
                    elif (obj.type == "selector_expression"
                            and field.text == b"Write"
                            and "os" not in ignore):
                        inner_obj = obj.children[0] if obj.children else None
                        inner_field = None
                        for child in obj.children:
                            if child.type == "field_identifier":
                                inner_field = child
                        if (inner_obj and inner_obj.type == "identifier"
                                and inner_obj.text == b"os"
                                and inner_field
                                and inner_field.text == b"Stdout"):
                            results.append(LintResult(
                                file=filepath,
                                line=_node_line(node),
                                rule="stdout",
                                severity="error",
                                message="Library writes to os.Stdout",
                            ))

        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return results


def _check_entry_points(tree, filepath, config):
    """Detect func main() in package main files."""
    results = []
    ignore = set(config.entry_point_ignore)

    has_package_main = False
    has_func_main = False
    func_main_line = 0

    def _walk(node):
        nonlocal has_package_main, has_func_main, func_main_line
        if node.type == "package_clause":
            for child in node.children:
                if child.type == "package_identifier" and child.text == b"main":
                    has_package_main = True
        elif node.type == "function_declaration":
            for child in node.children:
                if child.type == "identifier" and child.text == b"main":
                    has_func_main = True
                    func_main_line = _node_line(node)
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)

    if has_package_main and has_func_main and "main" not in ignore:
        results.append(LintResult(
            file=filepath,
            line=func_main_line,
            rule="entry-point",
            severity="error",
            message="Library declares entry point func main()",
        ))

    return results


def scan_imports(filepath: str) -> list[tuple[str, str, int]]:
    """Extract all import paths from a Go source file.

    Parses the file with tree-sitter and walks import_declaration nodes
    to collect every imported package path.

    Args:
        filepath: absolute path to a .go file.

    Returns:
        list of (import_path, filepath, line_number) tuples.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    parser = _make_parser()
    tree = parser.parse(source.encode("utf-8"))
    results: list[tuple[str, str, int]] = []

    def _walk(node):
        if node.type == "import_declaration":
            for child in node.children:
                _collect_import_spec(child)
            return
        for child in node.children:
            _walk(child)

    def _collect_import_spec(node):
        if node.type == "import_spec":
            for child in node.children:
                if child.type == "interpreted_string_literal":
                    content = _extract_string_content(child)
                    results.append((content, filepath, _node_line(node)))
        elif node.type == "import_spec_list":
            for child in node.children:
                _collect_import_spec(child)

    _walk(tree.root_node)
    return results


class GoAstLinter:
    """Go linter using tree-sitter AST analysis."""

    language = "go"
    parser_type = "ast"

    def lint(self, project_path: str, config: LanguageLintConfig) -> list[LintResult]:
        results = []

        parser = _make_parser()
        for filepath in walk_source_files(project_path, (".go",), config.exclude_patterns):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            source_bytes = source.encode("utf-8")
            tree = parser.parse(source_bytes)

            results.extend(_check_forbidden_imports(tree, filepath, config))
            if config.stdout_enabled:
                results.extend(_check_stdout(tree, filepath, config))
            if config.entry_point_enabled:
                results.extend(_check_entry_points(tree, filepath, config))

        return results
