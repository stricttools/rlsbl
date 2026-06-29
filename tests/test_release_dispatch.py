"""Tests for the release-dispatch workflow template and scaffold integration."""

import json
import os
from pathlib import Path

import pytest

from rlsbl.commands.init_cmd import (
    _append_release_dispatch_if_configured,
    plan_mappings,
    process_template,
)


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "rlsbl" / "templates"
SHARED_TEMPLATE_DIR = TEMPLATES_DIR / "shared"


class TestReleaseDispatchTemplate:
    """The release-dispatch.yml.tpl template exists and has correct content."""

    def test_template_exists(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        assert tpl.exists(), "release-dispatch.yml.tpl should exist in shared templates"

    def test_template_has_workflow_dispatch_trigger(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert "workflow_dispatch:" in content

    def test_template_has_bump_input(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert "bump:" in content
        assert "patch" in content
        assert "minor" in content
        assert "major" in content
        assert "prerelease" in content
        assert "hotfix" in content

    def test_template_has_description_input(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert "description:" in content

    def test_template_has_release_token(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert "RELEASE_TOKEN" in content

    def test_template_uses_action_placeholders(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert '{{action "actions/checkout"}}' in content
        assert '{{action "astral-sh/setup-uv"}}' in content

    def test_template_renders_without_errors(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        rendered, unreplaced = process_template(content, {}, template_path=str(tpl))
        assert unreplaced == []
        assert "actions/checkout@" in rendered
        assert "astral-sh/setup-uv@" in rendered

    def test_template_has_preid_input(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert "preid:" in content
        assert "Pre-release identifier" in content
        assert "- none" in content
        assert "- alpha" in content
        assert "- beta" in content
        assert "- rc" in content
        assert "- stable" in content

    def test_template_has_rlsbl_release_run(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert "rlsbl release run" in content
        assert "--bump" in content
        assert "--description" in content

    def test_template_has_conditional_preid_flag(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert "PREID_FLAG" in content
        assert '--preid ${{ inputs.preid }}' in content

    def test_template_has_contents_write_permission(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert "contents: write" in content

    def test_template_has_fetch_depth_zero(self):
        """Full history is needed for version detection and changelog."""
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-dispatch.yml.tpl"
        content = tpl.read_text()
        assert "fetch-depth: 0" in content


class TestAppendReleaseDispatchIfConfigured:
    """_append_release_dispatch_if_configured conditionally adds the mapping."""

    def test_not_appended_when_config_missing(self):
        mappings = [{"template": "foo.tpl", "target": "foo"}]
        result = _append_release_dispatch_if_configured(mappings, {})
        assert len(result) == 1

    def test_not_appended_when_false(self):
        mappings = [{"template": "foo.tpl", "target": "foo"}]
        result = _append_release_dispatch_if_configured(mappings, {"remote_release": False})
        assert len(result) == 1

    def test_appended_when_true(self):
        mappings = [{"template": "foo.tpl", "target": "foo"}]
        result = _append_release_dispatch_if_configured(mappings, {"remote_release": True})
        assert len(result) == 2
        assert result[1]["target"] == ".github/workflows/release-dispatch.yml"
        assert result[1]["template"] == ".github/workflows/release-dispatch.yml.tpl"

    def test_does_not_mutate_original(self):
        mappings = [{"template": "foo.tpl", "target": "foo"}]
        result = _append_release_dispatch_if_configured(mappings, {"remote_release": True})
        assert len(mappings) == 1
        assert len(result) == 2


class TestReleaseDispatchScaffoldIntegration:
    """Scaffold generates release-dispatch.yml when remote_release is configured."""

    def test_plan_includes_release_dispatch(self, tmp_project):
        """plan_mappings generates a plan for release-dispatch.yml."""
        mappings = [{
            "template": ".github/workflows/release-dispatch.yml.tpl",
            "target": ".github/workflows/release-dispatch.yml",
        }]
        plans = plan_mappings(str(SHARED_TEMPLATE_DIR), mappings, {}, force=False)
        assert len(plans) == 1
        assert plans[0]["target"] == ".github/workflows/release-dispatch.yml"
        assert plans[0]["status"] == "created"
        assert "rlsbl release run" in plans[0]["content"]

    def test_plan_not_generated_without_config(self, tmp_project):
        """Without remote_release in config, no release-dispatch mapping is added."""
        from rlsbl.targets.base import BaseTarget

        base = BaseTarget()
        shared_mappings = base.shared_template_mappings(None)
        result = _append_release_dispatch_if_configured(shared_mappings, {})
        targets = [m["target"] for m in result]
        assert ".github/workflows/release-dispatch.yml" not in targets

    def test_plan_generated_with_config(self, tmp_project):
        """With remote_release: true, release-dispatch mapping is added."""
        from rlsbl.targets.base import BaseTarget

        base = BaseTarget()
        shared_mappings = base.shared_template_mappings(None)
        result = _append_release_dispatch_if_configured(
            shared_mappings, {"remote_release": True}
        )
        targets = [m["target"] for m in result]
        assert ".github/workflows/release-dispatch.yml" in targets
