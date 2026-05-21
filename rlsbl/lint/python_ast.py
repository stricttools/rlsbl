"""Python linter using tree-sitter AST parsing to detect library boundary violations including forbidden imports and stdout/logging usage."""

import os
import sys
import tomllib

import tree_sitter_python
from tree_sitter import Language, Parser

from .config import LanguageLintConfig
from .result import LintResult
from .utils import walk_source_files

PY_LANG = Language(tree_sitter_python.language())


def _make_parser():
    parser = Parser(PY_LANG)
    return parser


def _node_line(node):
    """Return 1-based line number for a tree-sitter node."""
    return node.start_point[0] + 1


def _collect_all_imports(tree, filepath):
    """Walk AST and collect all imported top-level module names.

    Returns a set of (package_name, file_path, line_number) tuples.
    """
    imports = set()

    def _walk(node):
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    module = child.text.decode("utf-8")
                    top_level = module.split(".")[0]
                    imports.add((top_level, filepath, _node_line(node)))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        module = name_node.text.decode("utf-8")
                        top_level = module.split(".")[0]
                        imports.add((top_level, filepath, _node_line(node)))
        elif node.type == "import_from_statement":
            module_node = child_by_field(node, "module_name")
            if module_node:
                module = module_node.text.decode("utf-8")
                top_level = module.split(".")[0]
                imports.add((top_level, filepath, _node_line(node)))
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return imports


def _check_forbidden_imports(tree, filepath, config):
    """Walk AST for import_statement and import_from_statement nodes."""
    results = []
    forbidden = frozenset(config.forbidden_imports)
    all_imports = _collect_all_imports(tree, filepath)

    for pkg, fpath, line in all_imports:
        if pkg in forbidden:
            results.append(LintResult(
                file=fpath,
                line=line,
                rule="forbidden-import",
                severity="error",
                message=f"Library imports interface module '{pkg}'",
            ))

    return results


def child_by_field(node, field_name):
    """Find the module name in an import_from_statement.

    tree-sitter-python represents 'from foo import bar' with the module
    as a dotted_name or relative_import child before the 'import' keyword.
    """
    if field_name == "module_name":
        for child in node.children:
            if child.type in ("dotted_name", "relative_import"):
                return child
    return None


def _check_stdout(tree, filepath, config):
    """Detect print(), sys.stdout/stderr.write(), and logging.* calls."""
    results = []
    ignore = set(config.stdout_ignore)

    def _walk(node):
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func is None:
                for child in node.children:
                    _walk(child)
                return

            # print() calls
            if func.type == "identifier" and func.text == b"print" and "print" not in ignore:
                results.append(LintResult(
                    file=filepath,
                    line=_node_line(node),
                    rule="stdout",
                    severity="error",
                    message="Library calls print()",
                ))

            # sys.stdout.write() / sys.stderr.write()
            elif func.type == "attribute":
                attr_name = func.child_by_field_name("attribute")
                obj = func.child_by_field_name("object")
                if (attr_name and attr_name.text == b"write"
                        and obj and obj.type == "attribute"):
                    inner_attr = obj.child_by_field_name("attribute")
                    inner_obj = obj.child_by_field_name("object")
                    if (inner_attr and inner_attr.text in (b"stdout", b"stderr")
                            and inner_obj and inner_obj.type == "identifier"
                            and inner_obj.text == b"sys"
                            and "sys" not in ignore):
                        stream = inner_attr.text.decode("utf-8")
                        results.append(LintResult(
                            file=filepath,
                            line=_node_line(node),
                            rule="stdout",
                            severity="error",
                            message=f"Library writes to sys.{stream}",
                        ))

                # logging.* calls
                if (obj and obj.type == "identifier"
                        and obj.text == b"logging"
                        and "logging" not in ignore):
                    results.append(LintResult(
                        file=filepath,
                        line=_node_line(node),
                        rule="stdout",
                        severity="warning",
                        message="Library uses logging directly",
                    ))

        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return results


def _check_entry_points(project_path, config):
    """Check pyproject.toml for CLI entry point declarations."""
    pyproject_path = os.path.join(project_path, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return []

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return []

    ignore = set(config.entry_point_ignore)
    results = []
    project = data.get("project", {})
    for section_key in ("scripts", "gui-scripts"):
        entries = project.get(section_key, {})
        for name in entries:
            if name not in ignore:
                results.append(LintResult(
                    file=pyproject_path,
                    line=0,
                    rule="entry-point",
                    severity="error",
                    message=f"Library declares CLI entry point '{name}'",
                ))
    return results


class PythonAstLinter:
    """Python linter using tree-sitter AST analysis."""

    language = "python"
    parser_type = "ast"

    def lint(self, project_path: str, config: LanguageLintConfig) -> list[LintResult]:
        results = []

        # Entry point check
        if config.entry_point_enabled:
            results.extend(_check_entry_points(project_path, config))

        # Source file checks
        parser = _make_parser()
        for filepath in walk_source_files(project_path, (".py",), config.exclude_patterns):
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

        return results

    def scan_imports(self, project_path: str) -> set[tuple[str, str, int]]:
        """Collect all imported top-level module names from Python files.

        Returns a set of (package_name, file_path, line_number) tuples.
        """
        all_imports: set[tuple[str, str, int]] = set()
        parser = _make_parser()
        for filepath in walk_source_files(project_path, (".py",), []):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            source_bytes = source.encode("utf-8")
            tree = parser.parse(source_bytes)
            all_imports.update(_collect_all_imports(tree, filepath))

        return all_imports
