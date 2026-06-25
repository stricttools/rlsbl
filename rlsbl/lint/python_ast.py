"""Python linter using tree-sitter AST parsing to detect library boundary violations including forbidden imports and stdout/logging usage."""

import dataclasses
import os
import tomllib

import tree_sitter_python
from tree_sitter import Language, Parser

from .config import LanguageLintConfig
from .result import LintResult
from .utils import walk_source_files

@dataclasses.dataclass(frozen=True)
class ImportRecord:
    top_level: str
    full_path: str
    filepath: str
    line: int
    guarded: bool = False
    type_checking: bool = False

PY_LANG = Language(tree_sitter_python.language())


def _make_parser():
    parser = Parser(PY_LANG)
    return parser


def _node_line(node):
    """Return 1-based line number for a tree-sitter node."""
    return node.start_point[0] + 1


def _is_in_try_except_import_error(node):
    """Check if an import node is inside a try body protected by except ImportError/ModuleNotFoundError.

    Walks up the parent chain looking for a try_statement ancestor where:
    1. The import is in the try body (not inside an except_clause's block)
    2. At least one except_clause catches ImportError or ModuleNotFoundError

    Returns True if ANY ancestor try_statement satisfies both conditions.
    """
    _IMPORT_ERROR_NAMES = frozenset({"ImportError", "ModuleNotFoundError"})

    current = node.parent
    while current is not None:
        if current.type == "except_clause":
            # The import is inside an except body -- walk past this
            # try_statement without matching it (it's a fallback import,
            # not an optional one). Skip to the parent of the enclosing
            # try_statement.
            parent = current.parent  # the try_statement
            if parent is not None:
                current = parent.parent
            else:
                break
            continue

        if current.type == "try_statement":
            # The import is in the try body (we didn't pass through an
            # except_clause to get here). Check if any except_clause
            # catches ImportError or ModuleNotFoundError.
            if _try_catches_import_error(current, _IMPORT_ERROR_NAMES):
                return True

        current = current.parent

    return False


def _try_catches_import_error(try_node, error_names):
    """Check if a try_statement has an except_clause catching ImportError/ModuleNotFoundError."""
    for child in try_node.children:
        if child.type != "except_clause":
            continue
        # Look at the except_clause's children for the exception type(s)
        for ec_child in child.children:
            if ec_child.type == "identifier":
                if ec_child.text.decode("utf-8") in error_names:
                    return True
            elif ec_child.type == "as_pattern":
                # except ImportError as e: or except (ImportError, X) as e:
                for ap_child in ec_child.children:
                    if ap_child.type == "identifier":
                        if ap_child.text.decode("utf-8") in error_names:
                            return True
                    elif ap_child.type == "tuple":
                        for t_child in ap_child.children:
                            if t_child.type == "identifier":
                                if t_child.text.decode("utf-8") in error_names:
                                    return True
            elif ec_child.type == "tuple":
                # except (ImportError, ValueError): -- identifiers inside tuple
                for t_child in ec_child.children:
                    if t_child.type == "identifier":
                        if t_child.text.decode("utf-8") in error_names:
                            return True
    return False


def _is_in_type_checking_block(node):
    """Check if an import node is inside an `if TYPE_CHECKING:` block.

    Walks up the parent chain looking for an if_statement ancestor whose
    condition is:
    - An identifier node with text `TYPE_CHECKING`, OR
    - An attribute node whose last identifier is `TYPE_CHECKING`
      (for `typing.TYPE_CHECKING`)

    Returns True if found, False otherwise.
    """
    current = node.parent
    while current is not None:
        if current.type == "if_statement":
            # The condition is the second child (index 1) of if_statement:
            # if_statement -> "if" <condition> ":" <body>
            condition = current.children[1] if len(current.children) > 1 else None
            if condition is not None:
                if (condition.type == "identifier"
                        and condition.text.decode("utf-8") == "TYPE_CHECKING"):
                    return True
                if condition.type == "attribute":
                    # Check the last identifier child (e.g., typing.TYPE_CHECKING)
                    attr = condition.child_by_field_name("attribute")
                    if (attr is not None
                            and attr.text.decode("utf-8") == "TYPE_CHECKING"):
                        return True
        current = current.parent
    return False


def _collect_all_imports(tree, filepath):
    """Walk AST and collect all imported module names with full paths.

    Returns a set of ImportRecord with top_level, full_path, filepath,
    line, guarded, and type_checking fields.
    ``top_level`` is the first component of the dotted path (e.g., "orxt").
    ``full_path`` is the complete dotted module path (e.g., "orxt.protocols").
    Imports inside try/except ImportError blocks are marked guarded=True.
    Imports inside ``if TYPE_CHECKING:`` blocks are marked type_checking=True.
    """
    imports = set()

    def _walk(node):
        if node.type == "import_statement":
            guarded = _is_in_try_except_import_error(node)
            tc = _is_in_type_checking_block(node)
            if guarded:
                # Still walk children for nested structures, but also
                # collect the import as guarded below.
                for child in node.children:
                    _walk(child)
            for child in node.children:
                if child.type == "dotted_name":
                    module = child.text.decode("utf-8")
                    top_level = module.split(".")[0]
                    imports.add(ImportRecord(
                        top_level=top_level, full_path=module,
                        filepath=filepath, line=_node_line(node),
                        guarded=guarded, type_checking=tc,
                    ))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        module = name_node.text.decode("utf-8")
                        top_level = module.split(".")[0]
                        imports.add(ImportRecord(
                            top_level=top_level, full_path=module,
                            filepath=filepath, line=_node_line(node),
                            guarded=guarded, type_checking=tc,
                        ))
            if guarded:
                return
        elif node.type == "import_from_statement":
            guarded = _is_in_try_except_import_error(node)
            tc = _is_in_type_checking_block(node)
            if guarded:
                for child in node.children:
                    _walk(child)
            module_node = child_by_field(node, "module_name")
            if module_node:
                module = module_node.text.decode("utf-8")
                top_level = module.split(".")[0]
                imports.add(ImportRecord(
                    top_level=top_level, full_path=module,
                    filepath=filepath, line=_node_line(node),
                    guarded=guarded, type_checking=tc,
                ))
            if guarded:
                return
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return imports


def _check_forbidden_imports(tree, filepath, config):
    """Walk AST for import_statement and import_from_statement nodes."""
    results = []
    forbidden = frozenset(config.forbidden_imports)
    all_imports = _collect_all_imports(tree, filepath)

    for record in all_imports:
        pkg, fpath, line = record.top_level, record.filepath, record.line
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


_TERMINATOR_TYPES = frozenset({
    "return_statement", "raise_statement", "break_statement", "continue_statement",
})


def _always_terminates(node):
    """Return True if the given node unconditionally terminates its enclosing block.

    A node always terminates if:
    - It is a return, raise, break, or continue statement.
    - It is a block whose last child always terminates.
    - It is an if_statement with an else_clause where the if body, all elif
      bodies, and the else body all always terminate.
    """
    if node.type in _TERMINATOR_TYPES:
        return True

    if node.type == "block":
        children = [c for c in node.children if c.is_named]
        return bool(children) and _always_terminates(children[-1])

    if node.type == "if_statement":
        has_else = False
        # Collect the if body and all elif/else clause bodies
        bodies = []
        for child in node.children:
            if child.type == "block":
                # The if branch body
                bodies.append(child)
            elif child.type == "elif_clause":
                for ec_child in child.children:
                    if ec_child.type == "block":
                        bodies.append(ec_child)
            elif child.type == "else_clause":
                has_else = True
                for ec_child in child.children:
                    if ec_child.type == "block":
                        bodies.append(ec_child)

        if not has_else:
            return False

        return all(_always_terminates(body) for body in bodies)

    return False


def _terminator_label(node):
    """Return a human-readable label for the terminating construct."""
    if node.type == "return_statement":
        return "return"
    if node.type == "raise_statement":
        return "raise"
    if node.type == "break_statement":
        return "break"
    if node.type == "continue_statement":
        return "continue"
    if node.type == "if_statement":
        return "if/else"
    return node.type


def _check_unreachable_code(tree, filepath):
    """Detect unreachable statements in block nodes.

    Walks all block nodes in the tree. Within each block, if a child statement
    unconditionally terminates (return, raise, break, continue, or exhaustive
    if/else with all branches terminating), any subsequent sibling statements
    are flagged as unreachable.

    Skips nested function and class definitions -- a return inside a nested
    function does not make the outer code unreachable.
    """
    results = []

    def _walk_blocks(node):
        if node.type == "block":
            named_children = [c for c in node.children if c.is_named]
            found_terminator = None
            for child in named_children:
                if found_terminator is not None:
                    results.append(LintResult(
                        file=filepath,
                        line=_node_line(child),
                        rule="unreachable-code",
                        severity="error",
                        message=f"unreachable code after {_terminator_label(found_terminator)}",
                    ))
                elif _always_terminates(child):
                    found_terminator = child

        # Recurse into children, but skip nested function/class definitions
        # to avoid false positives (a return in an inner function does not
        # terminate the outer block)
        for child in node.children:
            if child.type in ("function_definition", "class_definition"):
                # Still walk inside the function/class to find unreachable
                # code within those scopes
                for grandchild in child.children:
                    _walk_blocks(grandchild)
            else:
                _walk_blocks(child)

    _walk_blocks(tree.root_node)
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
            results.extend(_check_unreachable_code(tree, filepath))

        return results

    def scan_imports(
        self,
        project_path: str,
        exclude_dirs: list[str] | None = None,
    ) -> set[ImportRecord]:
        """Collect all imported module names from Python files.

        Returns a set of ImportRecord with top_level, full_path, filepath,
        line, guarded, and type_checking fields.
        Guarded imports are those inside try/except ImportError blocks.
        TYPE_CHECKING imports are those inside ``if TYPE_CHECKING:`` blocks.
        """
        all_imports: set[ImportRecord] = set()
        parser = _make_parser()
        for filepath in walk_source_files(project_path, (".py",), [], exclude_dirs=exclude_dirs):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            source_bytes = source.encode("utf-8")
            tree = parser.parse(source_bytes)
            all_imports.update(_collect_all_imports(tree, filepath))

        return all_imports
