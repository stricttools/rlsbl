"""Tests for the TemplateVars class, BaseTarget.template_vars(), and _overlay_target_vars helper."""

from rlsbl.targets.base import BaseTarget, TemplateVars
from rlsbl.commands.init_cmd import _overlay_target_vars


class TestTemplateVars:
    """TemplateVars auto-generates namespaced keys on construction."""

    def test_auto_generates_namespaced_keys(self):
        tv = TemplateVars("pypi", {"name": "foo", "version": "1.0"})
        assert tv["name"] == "foo"
        assert tv["version"] == "1.0"
        assert tv["pypi.name"] == "foo"
        assert tv["pypi.version"] == "1.0"

    def test_is_dict_subclass(self):
        tv = TemplateVars("npm", {"key": "val"})
        assert isinstance(tv, dict)

    def test_empty_dict_produces_empty(self):
        tv = TemplateVars("go", {})
        assert len(tv) == 0

    def test_none_base_dict_produces_empty(self):
        tv = TemplateVars("go")
        assert len(tv) == 0

    def test_dotted_keys_also_namespaced(self):
        """Keys already containing dots (but not the target prefix) are also namespaced."""
        tv = TemplateVars("cargo", {"some.nested": "val"})
        assert tv["some.nested"] == "val"
        assert tv["cargo.some.nested"] == "val"

    def test_post_construction_mutation_bare_only(self):
        """Post-construction __setitem__ does NOT auto-generate namespaced keys."""
        tv = TemplateVars("pypi", {"name": "foo"})
        tv["year"] = "2026"
        assert tv["year"] == "2026"
        assert "pypi.year" not in tv

    def test_preserves_all_original_keys(self):
        base = {"a": 1, "b": 2, "c": 3}
        tv = TemplateVars("t", base)
        for key in base:
            assert key in tv
            assert f"t.{key}" in tv
        assert len(tv) == 6  # 3 bare + 3 namespaced

    def test_values_match_between_bare_and_namespaced(self):
        tv = TemplateVars("npm", {"registryUrl": "https://registry.npmjs.org"})
        assert tv["registryUrl"] == tv["npm.registryUrl"]

    def test_bool_values_preserved(self):
        tv = TemplateVars("zig", {"isLibrary": True})
        assert tv["isLibrary"] is True
        assert tv["zig.isLibrary"] is True

    def test_update_does_not_auto_namespace(self):
        """dict.update() after construction stays bare-only."""
        tv = TemplateVars("pypi", {"name": "foo"})
        tv.update({"repoName": "user/repo"})
        assert tv["repoName"] == "user/repo"
        assert "pypi.repoName" not in tv


class TestBaseTargetTemplateVars:
    """BaseTarget.template_vars() returns TemplateVars, not a plain dict."""

    def test_returns_template_vars_instance(self):
        target = BaseTarget()
        result = target.template_vars(".", None)
        assert isinstance(result, TemplateVars)

    def test_returns_empty_template_vars(self):
        target = BaseTarget()
        result = target.template_vars(".", None)
        assert len(result) == 0

    def test_base_target_has_name(self):
        target = BaseTarget()
        assert target.name == "base"


class TestOverlayTargetVars:
    """_overlay_target_vars promotes namespaced keys to bare."""

    def test_basic_overlay(self):
        merged = {"pypi.minRequiredPython": "3.11", "name": "test"}
        _overlay_target_vars(merged, "pypi")
        assert merged["minRequiredPython"] == "3.11"
        # Original namespaced key still present
        assert merged["pypi.minRequiredPython"] == "3.11"

    def test_overwrites_existing_bare_keys(self):
        """Overlay promotes even when a bare key already exists."""
        merged = {"name": "primary", "npm.name": "npm-pkg"}
        _overlay_target_vars(merged, "npm")
        assert merged["name"] == "npm-pkg"

    def test_no_match_leaves_dict_unchanged(self):
        merged = {"pypi.name": "foo", "name": "bar"}
        _overlay_target_vars(merged, "go")
        assert merged == {"pypi.name": "foo", "name": "bar"}

    def test_empty_dict(self):
        merged = {}
        _overlay_target_vars(merged, "npm")
        assert merged == {}

    def test_multiple_keys_overlaid(self):
        merged = {
            "go.modulePath": "github.com/user/repo",
            "go.minRequiredGo": "1.22",
            "pypi.name": "other",
        }
        _overlay_target_vars(merged, "go")
        assert merged["modulePath"] == "github.com/user/repo"
        assert merged["minRequiredGo"] == "1.22"
        # pypi keys not promoted
        assert "name" not in merged
