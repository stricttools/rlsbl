"""Tests for hard errors on unresolved template variables.

Covers:
- Scaffold (apply_plans / process_mappings) raises ConfigError on unresolved vars
- Sync raises ConfigError on unresolved vars
- _generate_merged_publish tolerates unresolved vars (multi-target mode)
- Edge cases: {{#if}}, escaped, GoReleaser, action, Docker patterns
- Resolved vars do not trigger errors
- Error messages include unresolved var names
"""

from __future__ import annotations

import os

import pytest

from rlsbl.commands.init_cmd import (
    _generate_merged_publish,
    apply_plans,
    check_unreplaced_vars,
    plan_mappings,
    process_mappings,
    process_template,
)
from rlsbl.errors import ConfigError


# ---------------------------------------------------------------------------
# Scaffold: unresolved vars raise ConfigError
# ---------------------------------------------------------------------------


class TestScaffoldUnresolvedVarsError:
    """apply_plans and process_mappings raise ConfigError on unresolved vars."""

    def test_process_mappings_raises_on_unresolved_var(self, tmp_project):
        """process_mappings raises ConfigError when a template has unresolved vars."""
        tpl_dir = tmp_project / "templates"
        tpl_dir.mkdir()
        (tpl_dir / "ci.yml.tpl").write_text(
            "name: CI\npython-version: '{{minPython}}'\n"
        )
        mappings = [{"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"}]

        with pytest.raises(ConfigError, match="minPython"):
            process_mappings(str(tpl_dir), mappings, {})

    def test_apply_plans_raises_on_unresolved_var(self, tmp_project):
        """apply_plans raises ConfigError when a plan has unresolved vars."""
        tpl_dir = tmp_project / "templates"
        tpl_dir.mkdir()
        (tpl_dir / "hook.sh.tpl").write_text("echo {{projectName}}\n")
        mappings = [{"template": "hook.sh.tpl", "target": "hook.sh"}]

        plans = plan_mappings(str(tpl_dir), mappings, {})
        # plan_mappings stores unreplaced in the plan dict
        assert any(p.get("unreplaced") for p in plans)

        with pytest.raises(ConfigError, match="projectName"):
            apply_plans(plans)

    def test_resolved_vars_no_error(self, tmp_project):
        """When all vars are resolved, no error is raised."""
        tpl_dir = tmp_project / "templates"
        tpl_dir.mkdir()
        (tpl_dir / "ci.yml.tpl").write_text(
            "name: CI\npython-version: '{{minPython}}'\n"
        )
        mappings = [{"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"}]

        created, skipped, warnings, _ = process_mappings(
            str(tpl_dir), mappings, {"minPython": "3.11"},
        )
        content = (tmp_project / ".github" / "workflows" / "ci.yml").read_text()
        assert "3.11" in content
        assert "{{minPython}}" not in content

    def test_error_message_includes_all_unresolved_names(self, tmp_project):
        """The ConfigError message lists all unresolved var names."""
        tpl_dir = tmp_project / "templates"
        tpl_dir.mkdir()
        (tpl_dir / "config.tpl").write_text("{{alpha}} and {{beta}} and {{gamma}}\n")
        mappings = [{"template": "config.tpl", "target": "config.txt"}]

        with pytest.raises(ConfigError) as exc_info:
            process_mappings(str(tpl_dir), mappings, {})
        msg = str(exc_info.value)
        assert "alpha" in msg
        assert "beta" in msg
        assert "gamma" in msg

    def test_multiple_unresolved_in_same_file(self, tmp_project):
        """Multiple unresolved vars in the same template all appear in the error."""
        tpl_dir = tmp_project / "templates"
        tpl_dir.mkdir()
        (tpl_dir / "f.tpl").write_text("{{foo}} {{bar}}\n")
        mappings = [{"template": "f.tpl", "target": "f.txt"}]

        with pytest.raises(ConfigError, match="foo") as exc_info:
            process_mappings(str(tpl_dir), mappings, {})
        assert "bar" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Sync: unresolved vars raise ConfigError
# ---------------------------------------------------------------------------


class TestSyncUnresolvedVarsError:
    """Monorepo sync raises ConfigError on unresolved template vars in workflows."""

    def test_sync_raises_on_unresolved_var(self, tmp_project):
        """process_template + check_unreplaced_vars raises ConfigError.

        Exercises the same code path as sync.py: process_template detects
        unresolved vars, then check_unreplaced_vars raises ConfigError.
        """
        content = (
            "name: CI\n"
            "on: push\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/setup-python@v4\n"
            "        with:\n"
            "          python-version: '{{pypi.minRequiredPython}}'\n"
        )
        result_content, unreplaced = process_template(content, {})
        assert unreplaced == ["pypi.minRequiredPython"]
        with pytest.raises(ConfigError, match="pypi.minRequiredPython"):
            check_unreplaced_vars("test.yml", unreplaced)

    def test_sync_no_error_when_all_resolved(self):
        """When tvars resolves all vars, no error is raised."""
        content = "python-version: '{{pypi.minRequiredPython}}'\n"
        result_content, unreplaced = process_template(
            content, {"pypi.minRequiredPython": "3.11"},
        )
        assert unreplaced == []
        assert "3.11" in result_content

    def test_sync_detects_vars_even_with_empty_tvars(self):
        """When tvars is empty, unresolved vars are still detected (no guard)."""
        content = "python-version: '{{pypi.minRequiredPython}}'\n"
        result_content, unreplaced = process_template(content, {})
        assert "pypi.minRequiredPython" in unreplaced


# ---------------------------------------------------------------------------
# _generate_merged_publish tolerates unresolved vars
# ---------------------------------------------------------------------------


class TestMergedPublishToleratesUnresolved:
    """_generate_merged_publish intentionally handles unresolved vars without error.

    Tests the full pipeline: process_template -> whole-line drop -> inline
    sheltering -> YAML round-trip -> unsheltering.
    """

    def test_process_template_returns_unreplaced_without_error(self):
        """process_template itself does not raise on unresolved vars
        (it returns them in the unreplaced list). The caller decides."""
        content = "version: '{{npmVersion}}'\npython: '{{pypi.minPython}}'\n"
        result, unreplaced = process_template(content, {"npmVersion": "1.0.0"})
        assert "1.0.0" in result
        assert "pypi.minPython" in unreplaced
        # No error -- caller must check unreplaced and decide

    def test_whole_line_unresolved_dropped(self):
        """Whole-line {{var}} placeholders (e.g. {{homebrewEnv}}) are dropped
        from the merged output when unresolved."""
        result = _generate_merged_publish(
            ["npm", "go"],
            template_vars={
                # Resolve npm's registryUrl so only go's whole-line vars are unresolved
                "registryUrl": "https://registry.npmjs.org",
            },
        )
        # go template has {{homebrewEnv}} and {{npmPublishJobs}} as whole-line
        # placeholders -- both should be dropped entirely
        assert "homebrewEnv" not in result
        assert "npmPublishJobs" not in result
        # Both targets' jobs should be present
        assert "npm:" in result or "npm" in result
        assert "goreleaser" in result or "go:" in result

    def test_inline_unresolved_survives_via_sheltering(self):
        """Inline {{var}} placeholders survive YAML round-trip via the
        __UNRESOLVED__ sheltering mechanism and appear as {{var}} in output."""
        result = _generate_merged_publish(
            ["npm", "pypi"],
            template_vars={
                # Don't provide registryUrl -- it's inline in npm's template
                # (registry-url: {{registryUrl}}) so it should be sheltered
                # and restored after YAML round-trip
            },
        )
        # The inline {{registryUrl}} should survive as a template placeholder
        assert "{{registryUrl}}" in result
        # The sheltering sentinel must NOT appear in the final output
        assert "__UNRESOLVED__" not in result
        # Both targets' jobs should be present
        assert "npm:" in result
        assert "pypi:" in result


# ---------------------------------------------------------------------------
# Edge cases from 5c: patterns that must NOT appear in unreplaced
# ---------------------------------------------------------------------------


class TestEdgeCasesNotUnreplaced:
    """Patterns that should NOT trigger unresolved var errors."""

    def test_conditional_block_absent_var_not_in_unreplaced(self):
        """{{#if varName}} with absent var: block removed, var NOT in unreplaced."""
        template = "before\n{{#if feature}}body with {{innerVar}}\n{{/if}}after"
        content, unreplaced = process_template(template, {})
        assert "feature" not in unreplaced
        assert "innerVar" not in unreplaced
        assert "body" not in content

    def test_conditional_block_present_var_not_in_unreplaced(self):
        """{{#if varName}} with present var: block kept, condvar NOT in unreplaced."""
        template = "{{#if feature}}enabled{{/if}}"
        content, unreplaced = process_template(template, {"feature": "yes"})
        assert content == "enabled"
        assert "feature" not in unreplaced
        assert unreplaced == []

    def test_escaped_placeholder_not_in_unreplaced(self):
        r"""Escaped \{{word}} becomes literal {{word}}, NOT in unreplaced."""
        content, unreplaced = process_template(r"\{{version}}", {})
        assert content == "{{version}}"
        assert unreplaced == []

    def test_escaped_placeholder_with_var_existing_not_in_unreplaced(self):
        r"""Even when vars_dict has the key, \{{key}} is sheltered -- not in unreplaced."""
        content, unreplaced = process_template(
            r"\{{version}}", {"version": "9.9.9"},
        )
        assert content == "{{version}}"
        assert unreplaced == []

    def test_goreleaser_dot_version_not_in_unreplaced(self):
        """GoReleaser {{.Version}} does not match pass 2 regex (starts with dot)."""
        content, unreplaced = process_template(
            "version: {{.Version}}\ntag: {{.Tag}}", {},
        )
        # These should pass through unchanged and NOT appear in unreplaced
        assert "{{.Version}}" in content
        assert "{{.Tag}}" in content
        assert unreplaced == []

    def test_goreleaser_mixed_with_rlsbl_var(self):
        """GoReleaser patterns are safe, but rlsbl vars are still detected."""
        content, unreplaced = process_template(
            "version: {{.Version}}\nproject: {{projectName}}", {},
        )
        assert "{{.Version}}" in content
        assert unreplaced == ["projectName"]

    def test_action_placeholder_not_in_unreplaced(self):
        """{{action "..."}} is resolved in pass 1 -- never reaches pass 2."""
        content, unreplaced = process_template(
            '- uses: {{action "actions/checkout"}}', {},
        )
        assert "{{action" not in content
        assert unreplaced == []

    def test_docker_escaped_version_not_in_unreplaced(self):
        r"""Docker \{{version}} (escaped) becomes literal {{version}}, NOT in unreplaced."""
        template = (
            "tags: |\n"
            r"  type=semver,pattern=\{{version}}" "\n"
            r"  type=semver,pattern=\{{major}}.\{{minor}}" "\n"
        )
        content, unreplaced = process_template(template, {})
        assert "{{version}}" in content
        assert "{{major}}" in content
        assert "{{minor}}" in content
        assert unreplaced == []

    def test_docker_escaped_with_rlsbl_var(self):
        r"""Docker \{{version}} is safe but rlsbl {{imageName}} is detected."""
        template = (
            r"image: {{imageName}}" "\n"
            r"tag: \{{version}}" "\n"
        )
        content, unreplaced = process_template(template, {})
        assert "imageName" in unreplaced
        assert len(unreplaced) == 1
        # Escaped {{version}} is literal, not unreplaced
        assert "{{version}}" in content
