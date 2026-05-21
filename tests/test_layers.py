"""Tests for rlsbl.layers."""

import pytest

from rlsbl.layers import LayerConfig, load_layer_config, resolve_package_layer, validate_layer_assignments
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE


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
        with pytest.raises(ValueError, match="non-empty list"):
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
        with pytest.raises(ValueError, match="not in \\[layers.order\\]"):
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
