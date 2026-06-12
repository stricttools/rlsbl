"""Tests for rlsbl.layers."""

import pytest

from rlsbl.errors import WorkspaceError
from rlsbl.layers import (
    LayerConfig,
    check_layer_violations,
    load_layer_config,
    resolve_package_layer,
    validate_layer_assignments,
)
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE
from rlsbl.workspace_graph import WorkspaceGraph


def _write_workspace(tmp_project, content):
    """Write workspace.toml with the given TOML content."""
    ws_dir = tmp_project / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(content)


FULL_CONFIG = """\
[[projects]]
path = "schema"
name = "schema"

[[projects]]
path = "models"
name = "models"

[[projects]]
path = "payments_core"
name = "payments_core"

[[projects]]
path = "flow_checkout"
name = "flow_checkout"

[[projects]]
path = "app"
name = "app"

[[projects]]
path = "auth_spec"
name = "auth_spec"

[[projects]]
path = "auth_contract"
name = "auth_contract"

[layers]
order = ["foundation", "specs", "contracts", "implementations", "flows", "app"]

[layers.assignments]
foundation = ["schema", "models", "infra"]
specs = ["*_spec"]
contracts = ["*_contract"]
implementations = ["marketplace", "payments_*", "shipping_*"]
flows = ["flow_*"]
app = ["app"]

[layers.overrides]
unrestricted = ["conformance", "testing"]
forbidden_targets = ["legacy_*"]

[[layers.overrides.allow]]
source = "app"
target = "*"
"""


class TestLoadLayerConfig:
    """Tests for load_layer_config."""

    def test_loads_complete_config(self, tmp_project):
        _write_workspace(tmp_project, FULL_CONFIG)
        cfg = load_layer_config(str(tmp_project))
        assert cfg is not None
        assert cfg.order == ["foundation", "specs", "contracts", "implementations", "flows", "app"]
        assert cfg.assignments["foundation"] == ["schema", "models", "infra"]
        assert cfg.assignments["specs"] == ["*_spec"]
        assert cfg.unrestricted == ["conformance", "testing"]
        assert cfg.forbidden_targets == ["legacy_*"]
        assert len(cfg.allow) == 1
        assert cfg.allow[0] == {"source": "app", "target": "*"}

    def test_missing_layers_returns_none(self, tmp_project):
        _write_workspace(tmp_project, '[[projects]]\npath = "a"\nname = "a"\n')
        cfg = load_layer_config(str(tmp_project))
        assert cfg is None

    def test_missing_workspace_file_raises(self, tmp_project):
        with pytest.raises(FileNotFoundError):
            load_layer_config(str(tmp_project))

    def test_empty_order_raises(self, tmp_project):
        content = """\
[[projects]]
path = "a"
name = "a"

[layers]
order = []
"""
        _write_workspace(tmp_project, content)
        with pytest.raises(WorkspaceError, match="non-empty list"):
            load_layer_config(str(tmp_project))

    def test_assignment_key_not_in_order_raises(self, tmp_project):
        content = """\
[[projects]]
path = "a"
name = "a"

[layers]
order = ["base"]

[layers.assignments]
unknown_layer = ["a"]
"""
        _write_workspace(tmp_project, content)
        with pytest.raises(WorkspaceError, match="not in \\[layers.order\\]"):
            load_layer_config(str(tmp_project))

    def test_overrides_optional(self, tmp_project):
        content = """\
[[projects]]
path = "a"
name = "a"

[layers]
order = ["base"]

[layers.assignments]
base = ["a"]
"""
        _write_workspace(tmp_project, content)
        cfg = load_layer_config(str(tmp_project))
        assert cfg is not None
        assert cfg.unrestricted == []
        assert cfg.forbidden_targets == []
        assert cfg.allow == []

    def test_assignments_optional(self, tmp_project):
        content = """\
[[projects]]
path = "a"
name = "a"

[layers]
order = ["base"]
"""
        _write_workspace(tmp_project, content)
        cfg = load_layer_config(str(tmp_project))
        assert cfg is not None
        assert cfg.assignments == {}


class TestResolvePackageLayer:
    """Tests for resolve_package_layer."""

    def test_exact_match(self):
        cfg = LayerConfig(
            order=["foundation"],
            assignments={"foundation": ["schema", "models"]},
        )
        assert resolve_package_layer("schema", cfg) == "foundation"
        assert resolve_package_layer("models", cfg) == "foundation"

    def test_glob_match(self):
        cfg = LayerConfig(
            order=["specs", "implementations"],
            assignments={
                "specs": ["*_spec"],
                "implementations": ["payments_*"],
            },
        )
        assert resolve_package_layer("auth_spec", cfg) == "specs"
        assert resolve_package_layer("payments_core", cfg) == "implementations"

    def test_unassigned_returns_none(self):
        cfg = LayerConfig(
            order=["foundation"],
            assignments={"foundation": ["schema"]},
        )
        assert resolve_package_layer("unknown_pkg", cfg) is None

    def test_first_matching_layer_wins(self):
        # If a package could match multiple layers, the first one encountered
        # in the assignments dict iteration wins (dict preserves insertion order).
        cfg = LayerConfig(
            order=["a", "b"],
            assignments={
                "a": ["foo_*"],
                "b": ["foo_bar"],
            },
        )
        assert resolve_package_layer("foo_bar", cfg) == "a"


class TestValidateLayerAssignments:
    """Tests for validate_layer_assignments."""

    def test_all_assigned_no_errors(self):
        cfg = LayerConfig(
            order=["foundation", "app"],
            assignments={
                "foundation": ["schema", "models"],
                "app": ["app"],
            },
        )
        projects = [
            {"name": "schema", "path": "schema"},
            {"name": "models", "path": "models"},
            {"name": "app", "path": "app"},
        ]
        errors = validate_layer_assignments(projects, cfg)
        assert errors == []

    def test_unassigned_package_error(self):
        cfg = LayerConfig(
            order=["foundation"],
            assignments={"foundation": ["schema"]},
        )
        projects = [
            {"name": "schema", "path": "schema"},
            {"name": "orphan", "path": "orphan"},
        ]
        errors = validate_layer_assignments(projects, cfg)
        assert len(errors) == 1
        assert "orphan" in errors[0]
        assert "not assigned" in errors[0]

    def test_multi_assigned_package_error(self):
        cfg = LayerConfig(
            order=["a", "b"],
            assignments={
                "a": ["shared_*"],
                "b": ["*_lib"],
            },
        )
        projects = [{"name": "shared_lib", "path": "shared_lib"}]
        errors = validate_layer_assignments(projects, cfg)
        assert len(errors) == 1
        assert "shared_lib" in errors[0]
        assert "multiple layers" in errors[0]

    def test_glob_assignments_cover_projects(self):
        cfg = LayerConfig(
            order=["specs", "flows"],
            assignments={
                "specs": ["*_spec"],
                "flows": ["flow_*"],
            },
        )
        projects = [
            {"name": "auth_spec", "path": "auth_spec"},
            {"name": "flow_checkout", "path": "flow_checkout"},
        ]
        errors = validate_layer_assignments(projects, cfg)
        assert errors == []

    def test_empty_projects_no_errors(self):
        cfg = LayerConfig(
            order=["foundation"],
            assignments={"foundation": ["schema"]},
        )
        errors = validate_layer_assignments([], cfg)
        assert errors == []


def _make_graph(tmp_path, projects):
    """Build a WorkspaceGraph from project dicts with depends_on edges.

    Creates empty directories for each project so the scanners find no
    manifest files -- all edges come from explicit depends_on.
    """
    for proj in projects:
        (tmp_path / proj["path"]).mkdir(parents=True, exist_ok=True)
    return WorkspaceGraph(str(tmp_path), projects)


class TestCheckLayerViolations:
    """Tests for check_layer_violations."""

    def test_valid_downward_deps_no_violations(self, tmp_path):
        """Higher layers depending on lower layers produces no violations."""
        projects = [
            {"name": "schema", "path": "schema"},
            {"name": "app", "path": "app", "depends_on": ["schema"]},
        ]
        cfg = LayerConfig(
            order=["foundation", "app"],
            assignments={"foundation": ["schema"], "app": ["app"]},
        )
        graph = _make_graph(tmp_path, projects)
        violations = check_layer_violations(projects, cfg, graph)
        assert violations == []

    def test_upward_dep_violation(self, tmp_path):
        """Lower layer depending on higher layer is a violation."""
        projects = [
            {"name": "schema", "path": "schema", "depends_on": ["app"]},
            {"name": "app", "path": "app"},
        ]
        cfg = LayerConfig(
            order=["foundation", "app"],
            assignments={"foundation": ["schema"], "app": ["app"]},
        )
        graph = _make_graph(tmp_path, projects)
        violations = check_layer_violations(projects, cfg, graph)
        assert len(violations) == 1
        assert "schema" in violations[0]
        assert "lower layer cannot depend on higher" in violations[0]

    def test_same_layer_dep_no_violation(self, tmp_path):
        """Same-layer dependencies are allowed."""
        projects = [
            {"name": "schema", "path": "schema", "depends_on": ["models"]},
            {"name": "models", "path": "models"},
        ]
        cfg = LayerConfig(
            order=["foundation"],
            assignments={"foundation": ["schema", "models"]},
        )
        graph = _make_graph(tmp_path, projects)
        violations = check_layer_violations(projects, cfg, graph)
        assert violations == []

    def test_unrestricted_package_exempt(self, tmp_path):
        """Unrestricted packages are exempt from layer checks as source."""
        projects = [
            {"name": "testing", "path": "testing", "depends_on": ["app"]},
            {"name": "app", "path": "app"},
        ]
        cfg = LayerConfig(
            order=["foundation", "app"],
            assignments={"foundation": ["testing"], "app": ["app"]},
            unrestricted=["testing"],
        )
        graph = _make_graph(tmp_path, projects)
        violations = check_layer_violations(projects, cfg, graph)
        assert violations == []

    def test_forbidden_target_violation(self, tmp_path):
        """Depending on a forbidden target is always a violation."""
        projects = [
            {"name": "app", "path": "app", "depends_on": ["legacy_auth"]},
            {"name": "legacy_auth", "path": "legacy_auth"},
        ]
        cfg = LayerConfig(
            order=["foundation", "app"],
            assignments={"foundation": ["legacy_*"], "app": ["app"]},
            forbidden_targets=["legacy_*"],
        )
        graph = _make_graph(tmp_path, projects)
        violations = check_layer_violations(projects, cfg, graph)
        assert len(violations) == 1
        assert "forbidden target" in violations[0]

    def test_explicit_allow_overrides_layer_rule(self, tmp_path):
        """Explicit allow overrides the layer violation rule."""
        projects = [
            {"name": "schema", "path": "schema", "depends_on": ["app"]},
            {"name": "app", "path": "app"},
        ]
        cfg = LayerConfig(
            order=["foundation", "app"],
            assignments={"foundation": ["schema"], "app": ["app"]},
            allow=[{"source": "schema", "target": "app"}],
        )
        graph = _make_graph(tmp_path, projects)
        violations = check_layer_violations(projects, cfg, graph)
        assert violations == []

    def test_assignment_errors_returned_immediately(self, tmp_path):
        """If assignment validation fails, those errors are returned
        without checking edges."""
        projects = [
            {"name": "orphan", "path": "orphan"},
        ]
        cfg = LayerConfig(
            order=["foundation"],
            assignments={"foundation": ["schema"]},
        )
        graph = _make_graph(tmp_path, projects)
        violations = check_layer_violations(projects, cfg, graph)
        assert len(violations) == 1
        assert "not assigned" in violations[0]

    def test_allow_glob_patterns(self, tmp_path):
        """Allow entries support glob patterns for source and target."""
        projects = [
            {"name": "schema", "path": "schema", "depends_on": ["app"]},
            {"name": "app", "path": "app"},
        ]
        cfg = LayerConfig(
            order=["foundation", "app"],
            assignments={"foundation": ["schema"], "app": ["app"]},
            allow=[{"source": "sche*", "target": "*"}],
        )
        graph = _make_graph(tmp_path, projects)
        violations = check_layer_violations(projects, cfg, graph)
        assert violations == []

    def test_forbidden_target_takes_precedence_over_valid_direction(self, tmp_path):
        """Forbidden targets are violations even if the layer direction is valid."""
        projects = [
            {"name": "app", "path": "app", "depends_on": ["legacy_db"]},
            {"name": "legacy_db", "path": "legacy_db"},
        ]
        cfg = LayerConfig(
            order=["foundation", "app"],
            assignments={"foundation": ["legacy_*"], "app": ["app"]},
            forbidden_targets=["legacy_*"],
        )
        graph = _make_graph(tmp_path, projects)
        violations = check_layer_violations(projects, cfg, graph)
        assert len(violations) == 1
        assert "forbidden target" in violations[0]


class TestLayersViolationsCheck:
    """Test the layers-violations check registered on the strictcli check system."""

    def test_check_runs_via_system(self, tmp_path):
        """The registered check produces a result."""
        from pathlib import Path
        from unittest.mock import MagicMock

        from strictcli import CheckResult

        from rlsbl.check_context import WorkspaceCheckContext

        # Set up workspace.toml
        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "schema"\nname = "schema"\n\n'
            '[[projects]]\npath = "app"\nname = "app"\n\n'
            "[layers]\n"
            'order = ["foundation", "app"]\n\n'
            "[layers.assignments]\n"
            'foundation = ["schema"]\n'
            'app = ["app"]\n'
        )
        (tmp_path / "schema").mkdir()
        (tmp_path / "app").mkdir()

        projects = [
            {"name": "schema", "path": "schema"},
            {"name": "app", "path": "app", "depends_on": ["schema"]},
        ]
        graph = WorkspaceGraph(str(tmp_path), projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path),
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            graph=graph,
        )

        # Import the check function directly by calling register_checks
        # on a mock app that captures the registered check
        captured = {}

        class MockApp:
            _checks_enabled = True

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        from rlsbl.checks import register_checks
        register_checks(MockApp())

        assert "layers-violations" in captured
        result = captured["layers-violations"](ctx)
        assert isinstance(result, CheckResult)
        assert result.status == "pass"

    def test_check_skip_not_workspace(self):
        """The check skips when context is not a workspace."""
        from pathlib import Path

        from strictcli import CheckResult

        from rlsbl.context import ProjectContext

        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})

        captured = {}

        class MockApp:
            _checks_enabled = True

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        from rlsbl.checks import register_checks
        register_checks(MockApp())

        result = captured["layers-violations"](ctx)
        assert result.status == "skip"

    def test_check_skip_no_layers(self, tmp_path):
        """The check skips when no [layers] section exists."""
        from pathlib import Path

        from strictcli import CheckResult

        from rlsbl.check_context import WorkspaceCheckContext

        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "a"\nname = "a"\n'
        )
        (tmp_path / "a").mkdir()

        projects = [{"name": "a", "path": "a"}]
        graph = WorkspaceGraph(str(tmp_path), projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path),
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            graph=graph,
        )

        captured = {}

        class MockApp:
            _checks_enabled = True

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        from rlsbl.checks import register_checks
        register_checks(MockApp())

        result = captured["layers-violations"](ctx)
        assert result.status == "skip"
        assert "layers not configured" in result.message

    def test_check_fail_on_violation(self, tmp_path):
        """The check fails when a layer violation exists."""
        from pathlib import Path

        from strictcli import CheckResult

        from rlsbl.check_context import WorkspaceCheckContext

        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "schema"\nname = "schema"\n\n'
            '[[projects]]\npath = "app"\nname = "app"\n\n'
            "[layers]\n"
            'order = ["foundation", "app"]\n\n'
            "[layers.assignments]\n"
            'foundation = ["schema"]\n'
            'app = ["app"]\n'
        )
        (tmp_path / "schema").mkdir()
        (tmp_path / "app").mkdir()

        # schema depends on app -- upward violation
        projects = [
            {"name": "schema", "path": "schema", "depends_on": ["app"]},
            {"name": "app", "path": "app"},
        ]
        graph = WorkspaceGraph(str(tmp_path), projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path),
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            graph=graph,
        )

        captured = {}

        class MockApp:
            _checks_enabled = True

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        from rlsbl.checks import register_checks
        register_checks(MockApp())

        result = captured["layers-violations"](ctx)
        assert result.status == "fail"
        assert "1 layer violation" in result.message
