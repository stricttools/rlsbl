"""Tests verifying circular dependency cycle fixes.

Cycle 1: check_context -> context -> workspace -> targets -> check_context
Cycle 3: checks/ participating in the __init__ <-> commands/ SCC
"""

import ast
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RLSBL_DIR = os.path.join(PROJECT_ROOT, "rlsbl")


def _get_imports(filepath):
    """Parse a Python file and return all imported module paths (relative to rlsbl)."""
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=filepath)

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports


class TestCycle1WorkspaceTypes:
    """workspace_types.py must not import from targets, workspace, context, or checks."""

    def test_workspace_types_exists(self):
        path = os.path.join(RLSBL_DIR, "workspace_types.py")
        assert os.path.isfile(path), "workspace_types.py must exist"

    def test_workspace_types_no_forbidden_imports(self):
        path = os.path.join(RLSBL_DIR, "workspace_types.py")
        imports = _get_imports(path)
        forbidden_prefixes = ("targets", "workspace", "context", "checks",
                              ".targets", ".workspace", ".context", ".checks")
        for imp in imports:
            for prefix in forbidden_prefixes:
                assert not imp.startswith(prefix) and not imp.endswith(prefix), (
                    f"workspace_types.py must not import from {prefix}, found: {imp}"
                )

    def test_workspace_types_exports_core_types(self):
        """workspace_types.py must export WorkspaceProject, Releasable, get_releasable_dir."""
        from rlsbl.workspace_types import WorkspaceProject, Releasable, get_releasable_dir
        assert WorkspaceProject is not None
        assert Releasable is not None
        assert get_releasable_dir is not None

    def test_workspace_reexports_from_workspace_types(self):
        """workspace.py must re-export types from workspace_types so existing importers work."""
        from rlsbl.workspace import WorkspaceProject, Releasable, get_releasable_dir
        from rlsbl.workspace_types import (
            WorkspaceProject as WT_WP,
            Releasable as WT_R,
            get_releasable_dir as WT_grd,
        )
        assert WorkspaceProject is WT_WP
        assert Releasable is WT_R
        assert get_releasable_dir is WT_grd


class TestCycle1TargetsImport:
    """targets/__init__.py must import from workspace_types, not workspace."""

    def test_targets_imports_workspace_types_not_workspace(self):
        path = os.path.join(RLSBL_DIR, "targets", "__init__.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "workspace" in node.module:
                    # Must be workspace_types, not bare workspace
                    assert "workspace_types" in node.module, (
                        f"targets/__init__.py imports from {node.module} "
                        f"but should use workspace_types"
                    )


class TestCycle1WorkspaceTargetsEdge:
    """workspace.py's _derive_standalone_name must not import from targets."""

    def test_derive_standalone_name_no_targets_import(self):
        path = os.path.join(RLSBL_DIR, "workspace.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)

        # Find _derive_standalone_name function
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_derive_standalone_name":
                # Check no imports from targets inside this function
                for child in ast.walk(node):
                    if isinstance(child, ast.ImportFrom):
                        assert child.module is None or "targets" not in child.module, (
                            f"_derive_standalone_name must not import from targets, "
                            f"found import from {child.module}"
                        )


class TestCycle1ContextWorkspaceEdge:
    """context.py's _resolve_releasable_config_dir must not import from workspace."""

    def test_resolve_releasable_config_dir_no_workspace_import(self):
        path = os.path.join(RLSBL_DIR, "context.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_resolve_releasable_config_dir":
                for child in ast.walk(node):
                    if isinstance(child, ast.ImportFrom):
                        # Allow workspace_types but not workspace
                        if child.module and "workspace" in child.module:
                            assert "workspace_types" in child.module, (
                                f"_resolve_releasable_config_dir must not import from workspace, "
                                f"found import from {child.module}"
                            )


class TestCycle3ChecksNotImportCommands:
    """checks/ modules must not import from commands/."""

    def test_prepush_no_commands_import(self):
        path = os.path.join(RLSBL_DIR, "checks", "prepush.py")
        imports = _get_imports(path)
        for imp in imports:
            assert "commands" not in imp, (
                f"checks/prepush.py must not import from commands, found: {imp}"
            )

    def test_workspace_checks_no_commands_import(self):
        path = os.path.join(RLSBL_DIR, "checks", "workspace.py")
        imports = _get_imports(path)
        for imp in imports:
            assert "commands" not in imp, (
                f"checks/workspace.py must not import from commands, found: {imp}"
            )

    def test_project_checks_no_commands_import(self):
        path = os.path.join(RLSBL_DIR, "checks", "project.py")
        imports = _get_imports(path)
        for imp in imports:
            assert "commands" not in imp, (
                f"checks/project.py must not import from commands, found: {imp}"
            )

    def test_prepush_utils_exists(self):
        path = os.path.join(RLSBL_DIR, "prepush_utils.py")
        assert os.path.isfile(path), "prepush_utils.py must exist"

    def test_constraints_exists(self):
        path = os.path.join(RLSBL_DIR, "constraints.py")
        assert os.path.isfile(path), "constraints.py must exist"

    def test_prepush_utils_exports(self):
        from rlsbl.prepush_utils import (
            _check_jsonl_changelog,
            _get_pushed_commits,
            _parse_stdin_refs,
            _check_gitignore_guard,
            _get_release_branches,
            DEFAULT_RELEASE_BRANCHES,
        )
        assert callable(_check_jsonl_changelog)
        assert callable(_get_pushed_commits)
        assert callable(_parse_stdin_refs)
        assert callable(_check_gitignore_guard)
        assert callable(_get_release_branches)
        assert isinstance(DEFAULT_RELEASE_BRANCHES, list)

    def test_constraints_exports(self):
        from rlsbl.constraints import _evaluate_constraint, _parse_version_tuple
        assert callable(_evaluate_constraint)
        assert callable(_parse_version_tuple)

    def test_pre_push_check_module_deleted(self):
        path = os.path.join(RLSBL_DIR, "commands", "pre_push_check.py")
        assert not os.path.isfile(path), (
            "commands/pre_push_check.py must be deleted after extraction"
        )
