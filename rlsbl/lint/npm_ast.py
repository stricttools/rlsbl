"""JavaScript and TypeScript linter using tree-sitter AST parsing to detect library boundary violations such as forbidden imports and logging."""

import json
import os

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Parser

from .config import LanguageLintConfig
from .result import LintResult
from .utils import walk_source_files

JS_LANG = Language(tree_sitter_javascript.language())
TS_LANG = Language(tree_sitter_typescript.language_typescript())
TSX_LANG = Language(tree_sitter_typescript.language_tsx())

_JS_EXTENSIONS = frozenset({".js", ".mjs", ".cjs"})
_TS_EXTENSIONS = frozenset({".ts"})
_TSX_EXTENSIONS = frozenset({".tsx"})
_ALL_EXTENSIONS = (".js", ".ts", ".mjs", ".cjs", ".tsx")


def _lang_for_ext(ext):
    """Select the tree-sitter language based on file extension."""
    if ext in _JS_EXTENSIONS:
        return JS_LANG
    if ext in _TS_EXTENSIONS:
        return TS_LANG
    if ext in _TSX_EXTENSIONS:
        return TSX_LANG
    return JS_LANG


def _node_line(node):
    """Return 1-based line number for a tree-sitter node."""
    return node.start_point[0] + 1


def _extract_string(node):
    """Extract text content from a string/string_fragment node."""
    for child in node.children:
        if child.type == "string_fragment":
            return child.text.decode("utf-8")
    # Fallback: strip quotes
    text = node.text.decode("utf-8")
    return text.strip("'\"")


def _collect_all_imports(tree, filepath):
    """Walk AST and collect all imported package names.

    Returns a set of (package_name, file_path, line_number) tuples.
    """
    imports = set()

    def _walk(node):
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "string":
                    pkg = _extract_string(child)
                    imports.add((pkg, filepath, _node_line(node)))
            for child in node.children:
                _walk(child)
            return

        if node.type == "export_statement":
            for child in node.children:
                if child.type == "string":
                    pkg = _extract_string(child)
                    imports.add((pkg, filepath, _node_line(node)))
            for child in node.children:
                _walk(child)
            return

        if node.type == "call_expression":
            func = node.children[0] if node.children else None
            args = None
            for child in node.children:
                if child.type == "arguments":
                    args = child

            if func and args:
                # require('pkg')
                if func.type == "identifier" and func.text == b"require":
                    for child in args.children:
                        if child.type == "string":
                            pkg = _extract_string(child)
                            imports.add((pkg, filepath, _node_line(node)))
                            break

                # import('pkg') -- dynamic import
                elif func.type == "import":
                    for child in args.children:
                        if child.type == "string":
                            pkg = _extract_string(child)
                            imports.add((pkg, filepath, _node_line(node)))
                            break

        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return imports


def _check_forbidden_imports(tree, filepath, config):
    """Walk AST for import/require/export nodes."""
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
                message=f"Library imports forbidden package '{pkg}'",
            ))

    return results


def _check_stdout(tree, filepath, config):
    """Detect console.log/warn/error/info calls."""
    results = []
    ignore = set(config.stdout_ignore)

    if "console" in ignore:
        return results

    def _walk(node):
        if node.type == "call_expression":
            func = node.children[0] if node.children else None
            if func and func.type == "member_expression":
                obj = None
                prop = None
                for child in func.children:
                    if child.type == "identifier":
                        obj = child
                    elif child.type == "property_identifier":
                        prop = child

                if (obj and obj.text == b"console"
                        and prop
                        and prop.text in (b"log", b"warn", b"error", b"info")):
                    method = prop.text.decode("utf-8")
                    results.append(LintResult(
                        file=filepath,
                        line=_node_line(node),
                        rule="stdout",
                        severity="error",
                        message=f"Library calls console.{method}()",
                    ))

        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return results


def _check_entry_points(project_path, config):
    """Check package.json for 'bin' field."""
    pkg_path = os.path.join(project_path, "package.json")
    if not os.path.isfile(pkg_path):
        return []

    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    ignore = set(config.entry_point_ignore)
    results = []
    bin_field = data.get("bin")

    if isinstance(bin_field, str):
        # "bin": "./cli.js" -- use package name as the command name
        name = data.get("name", "")
        if name not in ignore:
            results.append(LintResult(
                file=pkg_path,
                line=0,
                rule="entry-point",
                severity="error",
                message=f"Library declares CLI entry point '{name}'",
            ))
    elif isinstance(bin_field, dict):
        # "bin": { "mycli": "./cli.js" }
        for name in bin_field:
            if name not in ignore:
                results.append(LintResult(
                    file=pkg_path,
                    line=0,
                    rule="entry-point",
                    severity="error",
                    message=f"Library declares CLI entry point '{name}'",
                ))

    return results


class NpmAstLinter:
    """npm (JS/TS) linter using tree-sitter AST analysis."""

    language = "npm"
    parser_type = "ast"

    def lint(self, project_path: str, config: LanguageLintConfig) -> list[LintResult]:
        results = []

        # Entry point check
        if config.entry_point_enabled:
            results.extend(_check_entry_points(project_path, config))

        # Source file checks
        for filepath in walk_source_files(project_path, _ALL_EXTENSIONS, config.exclude_patterns):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            ext = os.path.splitext(filepath)[1]
            lang = _lang_for_ext(ext)
            parser = Parser(lang)
            source_bytes = source.encode("utf-8")
            tree = parser.parse(source_bytes)

            results.extend(_check_forbidden_imports(tree, filepath, config))
            if config.stdout_enabled:
                results.extend(_check_stdout(tree, filepath, config))

        return results

    def scan_imports(self, project_path: str) -> set[tuple[str, str, int]]:
        """Collect all imported package names from JS/TS files.

        Returns a set of (package_name, file_path, line_number) tuples.
        """
        all_imports: set[tuple[str, str, int]] = set()
        for filepath in walk_source_files(project_path, _ALL_EXTENSIONS, []):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            ext = os.path.splitext(filepath)[1]
            lang = _lang_for_ext(ext)
            parser = Parser(lang)
            source_bytes = source.encode("utf-8")
            tree = parser.parse(source_bytes)
            all_imports.update(_collect_all_imports(tree, filepath))

        return all_imports
