"""Tests for the release-finalize workflow template and scaffold integration."""

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from rlsbl.commands.init_cmd import (
    _append_release_finalize_if_configured,
    plan_mappings,
    process_template,
)


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "rlsbl" / "templates"
SHARED_TEMPLATE_DIR = TEMPLATES_DIR / "shared"


class TestReleaseFinalizeTemplate:
    """The release-finalize.yml.tpl template exists and has correct content."""

    def test_template_exists(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        assert tpl.exists(), "release-finalize.yml.tpl should exist in shared templates"

    def test_template_renders_to_valid_yaml(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        rendered, unreplaced = process_template(content, {}, template_path=str(tpl))
        assert unreplaced == []
        # Rendered content uses ${{ ... }} GitHub expressions which are not
        # valid YAML values on their own. Replace them with placeholders so
        # the YAML parser can validate the structure.
        sanitized = rendered.replace("${{", "__GH__").replace("}}", "__END__")
        yml = YAML(typ="safe")
        parsed = yml.load(sanitized)
        assert isinstance(parsed, dict)

    def test_template_has_pr_closed_trigger(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        assert "pull_request:" in content
        assert "types: [closed]" in content
        assert "branches: [main]" in content

    def test_template_has_merged_and_starts_with_condition(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        assert "github.event.pull_request.merged == true" in content
        assert "startsWith(github.event.pull_request.head.ref, 'release/')" in content

    def test_template_dispatches_publish_workflows(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        assert "Dispatch publish workflows" in content
        assert "jq -r '.dispatch[]'" in content
        assert "gh workflow run" in content

    def test_template_uses_action_placeholders(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        assert '{{action "actions/checkout"}}' in content

    def test_template_resolves_action_versions(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        rendered, _ = process_template(content, {}, template_path=str(tpl))
        assert "actions/checkout@" in rendered

    def test_template_has_contents_write_permission(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        assert "contents: write" in content

    def test_template_has_fetch_depth_zero(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        assert "fetch-depth: 0" in content

    def test_template_globs_releasable_pending_location(self):
        """pending.json is located by globbing both the standalone location
        and the releasable state dirs -- non-root representatives would
        otherwise be broken by construction."""
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        assert ".rlsbl/releases/pending.json" in content
        assert ".rlsbl-monorepo/releasables/*/releases/pending.json" in content

    def test_template_errors_on_multiple_pending_files(self):
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        assert "multiple pending.json" in content
        assert "exit 1" in content

    def test_template_steps_use_resolved_pending_path(self):
        """Later steps must reference the resolved pending path output, not
        a hardcoded repo-root path."""
        tpl = SHARED_TEMPLATE_DIR / ".github" / "workflows" / "release-finalize.yml.tpl"
        content = tpl.read_text()
        assert "steps.meta.outputs.pending" in content
        # The only hardcoded .rlsbl/releases/pending.json occurrences are in
        # the glob candidate list of the metadata step.
        assert 'PENDING=".rlsbl/releases/pending.json"' not in content


class TestAppendReleaseFinalizeIfConfigured:
    """_append_release_finalize_if_configured conditionally adds the mapping."""

    def test_not_appended_when_config_missing(self):
        mappings = [{"template": "foo.tpl", "target": "foo"}]
        result = _append_release_finalize_if_configured(mappings, {})
        assert len(result) == 1

    def test_not_appended_when_mode_is_imperative(self):
        mappings = [{"template": "foo.tpl", "target": "foo"}]
        config = {"release": {"mode": "imperative"}}
        result = _append_release_finalize_if_configured(mappings, config)
        assert len(result) == 1

    def test_not_appended_when_release_has_no_mode(self):
        mappings = [{"template": "foo.tpl", "target": "foo"}]
        config = {"release": {}}
        result = _append_release_finalize_if_configured(mappings, config)
        assert len(result) == 1

    def test_appended_when_mode_is_pr(self):
        mappings = [{"template": "foo.tpl", "target": "foo"}]
        config = {"release": {"mode": "pr"}}
        result = _append_release_finalize_if_configured(mappings, config)
        assert len(result) == 2
        assert result[1]["target"] == ".github/workflows/release-finalize.yml"
        assert result[1]["template"] == ".github/workflows/release-finalize.yml.tpl"

    def test_does_not_mutate_original(self):
        mappings = [{"template": "foo.tpl", "target": "foo"}]
        config = {"release": {"mode": "pr"}}
        result = _append_release_finalize_if_configured(mappings, config)
        assert len(mappings) == 1
        assert len(result) == 2


class TestReleaseFinalizeScaffoldIntegration:
    """Scaffold generates release-finalize.yml when release.mode is "pr"."""

    def test_plan_includes_release_finalize(self, tmp_project):
        """plan_mappings generates a plan for release-finalize.yml."""
        mappings = [{
            "template": ".github/workflows/release-finalize.yml.tpl",
            "target": ".github/workflows/release-finalize.yml",
        }]
        plans = plan_mappings(str(SHARED_TEMPLATE_DIR), mappings, {}, force=False)
        assert len(plans) == 1
        assert plans[0]["target"] == ".github/workflows/release-finalize.yml"
        assert plans[0]["status"] == "created"
        assert "pending.json" in plans[0]["content"]

    def test_plan_not_generated_when_mode_is_imperative(self, tmp_project):
        """Without release.mode == "pr", no release-finalize mapping is added."""
        from rlsbl.targets.base import BaseTarget

        base = BaseTarget()
        shared_mappings = base.shared_template_mappings(None)
        result = _append_release_finalize_if_configured(
            shared_mappings, {"release": {"mode": "imperative"}}
        )
        targets = [m["target"] for m in result]
        assert ".github/workflows/release-finalize.yml" not in targets

    def test_plan_generated_when_mode_is_pr(self, tmp_project):
        """With release.mode == "pr", release-finalize mapping is added."""
        from rlsbl.targets.base import BaseTarget

        base = BaseTarget()
        shared_mappings = base.shared_template_mappings(None)
        result = _append_release_finalize_if_configured(
            shared_mappings, {"release": {"mode": "pr"}}
        )
        targets = [m["target"] for m in result]
        assert ".github/workflows/release-finalize.yml" in targets
