"""Focused tests for the template engine's process_template() function.

Covers the five behavioral areas of the multi-pass renderer:

1. Pass 1 -- action resolution ({{action "owner/name"}} -> pinned version)
2. Escape handling (\\{{varName}} -> literal {{varName}})
3. required_vars validation (missing required vars raise ConfigError)
4. Unreplaced variable collection (unknown vars stay in output, listed in unreplaced)
5. Conditional blocks ({{#if varName}}...{{/if}} -> include/exclude body)
"""

from __future__ import annotations

import pytest

from rlsbl.action_versions import UnknownActionError, format_action, get_all_versions
from rlsbl.commands.init_cmd import process_template
from rlsbl.errors import ConfigError


# ---------------------------------------------------------------------------
# 1. Pass 1 -- action resolution
# ---------------------------------------------------------------------------


class TestActionResolution:
    """{{action "owner/name"}} placeholders resolve to pinned versions."""

    def test_known_action_resolves(self):
        content, unreplaced = process_template(
            '- uses: {{action "actions/checkout"}}', {}
        )
        expected = format_action("actions/checkout")
        assert content == f"- uses: {expected}"
        assert unreplaced == []

    def test_multiple_known_actions_resolve(self):
        template = (
            '- uses: {{action "actions/checkout"}}\n'
            '- uses: {{action "actions/setup-python"}}\n'
            '- uses: {{action "astral-sh/setup-uv"}}\n'
        )
        content, unreplaced = process_template(template, {})
        table = get_all_versions()
        for name in ("actions/checkout", "actions/setup-python", "astral-sh/setup-uv"):
            assert f"{name}@{table[name]}" in content
        assert "{{action" not in content
        assert unreplaced == []

    def test_unknown_action_raises(self):
        with pytest.raises(UnknownActionError, match="nonexistent/action"):
            process_template('{{action "nonexistent/action"}}', {})

    def test_unknown_action_error_includes_template_path(self):
        with pytest.raises(UnknownActionError, match="workflows/ci.yml"):
            process_template(
                '{{action "nonexistent/action"}}',
                {},
                template_path="workflows/ci.yml",
            )

    def test_actions_resolved_before_variables(self):
        """Pass 1 (actions) runs before Pass 2 (variables)."""
        template = '{{action "actions/checkout"}} for {{project}}'
        content, unreplaced = process_template(template, {"project": "myproj"})
        assert content == f"{format_action('actions/checkout')} for myproj"
        assert unreplaced == []

    def test_action_with_extra_whitespace(self):
        """Whitespace between 'action' and the quoted name is tolerated."""
        content, unreplaced = process_template(
            '{{action   "actions/checkout"}}', {}
        )
        assert content == format_action("actions/checkout")
        assert unreplaced == []


# ---------------------------------------------------------------------------
# 2. Escape handling
# ---------------------------------------------------------------------------


class TestEscapeHandling:
    r"""Escaped placeholders (\{{word}}) emit literal {{word}} in output."""

    def test_escaped_placeholder_becomes_literal(self):
        content, unreplaced = process_template(r"\{{version}}", {})
        assert content == "{{version}}"
        assert unreplaced == []

    def test_escaped_placeholder_not_substituted_even_when_var_exists(self):
        r"""When vars_dict has 'version', \{{version}} still becomes literal."""
        content, unreplaced = process_template(
            r"\{{version}}", {"version": "9.9.9"}
        )
        assert content == "{{version}}"
        # The variable was not consumed -- it was sheltered by the escape.
        assert unreplaced == []

    def test_mixed_escaped_and_normal(self):
        template = r"name={{name}} pattern=\{{version}}"
        content, unreplaced = process_template(template, {"name": "myimg"})
        assert content == "name=myimg pattern={{version}}"
        assert unreplaced == []

    def test_multiple_escapes(self):
        template = r"\{{a}} and \{{b}}"
        content, unreplaced = process_template(template, {})
        assert content == "{{a}} and {{b}}"
        assert unreplaced == []

    def test_escaped_action_placeholder_not_resolved(self):
        r"""\{{action "..."}} is NOT treated as an action -- escape wins."""
        template = r'\{{action "actions/checkout"}}'
        content, unreplaced = process_template(template, {})
        assert content == '{{action "actions/checkout"}}'
        assert unreplaced == []

    def test_docker_metadata_pattern(self):
        """Real-world pattern: Docker metadata-action uses {{version}}."""
        template = (
            "tags: |\n"
            r"  type=semver,pattern=\{{version}}" "\n"
            r"  type=pep440,pattern=\{{major}}.\{{minor}}" "\n"
        )
        content, unreplaced = process_template(template, {"version": "2.0.0"})
        assert "pattern={{version}}" in content
        assert "pattern={{major}}.{{minor}}" in content
        # The escaped vars are NOT substituted, so 2.0.0 must not appear.
        assert "2.0.0" not in content
        assert unreplaced == []


# ---------------------------------------------------------------------------
# 3. required_vars validation
# ---------------------------------------------------------------------------


class TestRequiredVars:
    """When required_vars is set, missing required variables raise ConfigError."""

    def test_all_required_present(self):
        content, unreplaced = process_template(
            "{{name}} v{{year}}",
            {"name": "pkg", "year": "2026"},
            required_vars={"name", "year"},
        )
        assert content == "pkg v2026"
        assert unreplaced == []

    def test_missing_required_raises_config_error(self):
        with pytest.raises(ConfigError, match="author"):
            process_template(
                "by {{author}}",
                {},
                required_vars={"author"},
            )

    def test_error_message_includes_template_path(self):
        with pytest.raises(ConfigError, match="license.tpl"):
            process_template(
                "License: {{licenseType}}",
                {},
                template_path="license.tpl",
                required_vars={"licenseType"},
            )

    def test_only_required_vars_raise_not_others(self):
        """Non-required unreplaced vars do not cause an error."""
        with pytest.raises(ConfigError, match="critical"):
            process_template(
                "{{critical}} and {{optional}}",
                {},
                required_vars={"critical"},
            )

    def test_required_vars_not_in_template_ignored(self):
        """required_vars that do not appear as placeholders are harmless."""
        content, unreplaced = process_template(
            "Hello {{name}}",
            {"name": "world"},
            required_vars={"name", "nonexistent"},
        )
        assert content == "Hello world"
        assert unreplaced == []

    def test_required_vars_none_allows_unreplaced(self):
        """Default behavior: required_vars=None never raises."""
        content, unreplaced = process_template(
            "{{missing}} text", {}, required_vars=None
        )
        assert content == "{{missing}} text"
        assert unreplaced == ["missing"]

    def test_required_vars_empty_set_allows_unreplaced(self):
        """An empty required_vars set is equivalent to None."""
        content, unreplaced = process_template(
            "{{missing}} text", {}, required_vars=set()
        )
        assert content == "{{missing}} text"
        assert unreplaced == ["missing"]

    def test_multiple_missing_required_all_listed(self):
        """When multiple required vars are missing, all are named in the error."""
        with pytest.raises(ConfigError, match=r"alpha.*beta|beta.*alpha"):
            process_template(
                "{{alpha}} and {{beta}}",
                {},
                required_vars={"alpha", "beta"},
            )


# ---------------------------------------------------------------------------
# 4. Unreplaced variable collection
# ---------------------------------------------------------------------------


class TestUnreplacedCollection:
    """Variables not in vars_dict appear in the unreplaced list and stay
    as {{varName}} in the output."""

    def test_single_unreplaced(self):
        content, unreplaced = process_template("{{missing}}", {})
        assert content == "{{missing}}"
        assert unreplaced == ["missing"]

    def test_multiple_unreplaced(self):
        content, unreplaced = process_template("{{a}} {{b}} {{c}}", {})
        assert content == "{{a}} {{b}} {{c}}"
        assert unreplaced == ["a", "b", "c"]

    def test_mixed_replaced_and_unreplaced(self):
        content, unreplaced = process_template(
            "{{known}} and {{unknown}}", {"known": "yes"}
        )
        assert content == "yes and {{unknown}}"
        assert unreplaced == ["unknown"]

    def test_dotted_var_unreplaced(self):
        content, unreplaced = process_template("{{pypi.minPython}}", {})
        assert content == "{{pypi.minPython}}"
        assert unreplaced == ["pypi.minPython"]

    def test_unreplaced_preserves_order(self):
        """Unreplaced list follows left-to-right template order."""
        content, unreplaced = process_template("{{z}} {{a}} {{m}}", {})
        assert unreplaced == ["z", "a", "m"]

    def test_duplicate_placeholder_collected_twice(self):
        """If the same var appears multiple times and is unreplaced,
        it appears in unreplaced once per occurrence."""
        content, unreplaced = process_template("{{x}} {{x}}", {})
        assert content == "{{x}} {{x}}"
        assert unreplaced == ["x", "x"]

    def test_no_unreplaced_when_all_provided(self):
        content, unreplaced = process_template(
            "{{a}} {{b}}", {"a": "1", "b": "2"}
        )
        assert content == "1 2"
        assert unreplaced == []

    def test_empty_template_no_unreplaced(self):
        content, unreplaced = process_template("", {})
        assert content == ""
        assert unreplaced == []


# ---------------------------------------------------------------------------
# 5. Conditional blocks -- {{#if varName}}...{{/if}}
# ---------------------------------------------------------------------------


class TestConditionalBlocks:
    """{{#if varName}}...{{/if}} conditional blocks."""

    def test_truthy_var_keeps_body(self):
        template = "before\n{{#if feature}}included\n{{/if}}after"
        content, unreplaced = process_template(template, {"feature": "yes"})
        assert "included" in content
        assert unreplaced == []

    def test_falsy_var_removes_block(self):
        """Empty string is falsy -- block is removed."""
        template = "before\n{{#if feature}}removed\n{{/if}}after"
        content, unreplaced = process_template(template, {"feature": ""})
        assert "removed" not in content
        assert unreplaced == []

    def test_missing_var_removes_block(self):
        template = "before\n{{#if feature}}removed\n{{/if}}after"
        content, unreplaced = process_template(template, {})
        assert "removed" not in content
        assert unreplaced == []

    def test_boolean_like_nonempty_string_is_truthy(self):
        """Both 'true' and 'false' are non-empty strings, so truthy."""
        for val in ("true", "false", "0", "anything"):
            content, _ = process_template(
                "{{#if flag}}body{{/if}}", {"flag": val}
            )
            assert content == "body", f"Expected truthy for {val!r}"

    def test_boolean_like_empty_string_is_falsy(self):
        content, _ = process_template("{{#if flag}}body{{/if}}", {"flag": ""})
        assert content == ""

    def test_actions_inside_conditional_resolve(self):
        """Pass 1 (actions) runs before Pass 1.5 (conditionals),
        so actions inside conditional blocks are resolved."""
        template = '{{#if enabled}}uses: {{action "actions/checkout"}}{{/if}}'
        content, unreplaced = process_template(template, {"enabled": "yes"})
        expected = format_action("actions/checkout")
        assert content == f"uses: {expected}"
        assert unreplaced == []

    def test_variables_inside_surviving_block_resolve(self):
        """Pass 2 (variables) runs after Pass 1.5, so variables inside
        surviving blocks are resolved."""
        template = "{{#if enabled}}hello {{name}}{{/if}}"
        content, unreplaced = process_template(
            template, {"enabled": "yes", "name": "world"}
        )
        assert content == "hello world"
        assert unreplaced == []

    def test_variables_inside_removed_block_never_reach_pass2(self):
        """Variables inside removed blocks are gone before Pass 2.
        They should NOT appear in unreplaced."""
        template = "{{#if disabled}}{{missingVar}}{{/if}}after"
        content, unreplaced = process_template(template, {})
        assert "missingVar" not in content
        assert unreplaced == []

    def test_blank_line_cleanup_after_removal(self):
        """Removed blocks don't leave triple-newline gaps."""
        template = "before\n\n{{#if gone}}removed\n{{/if}}\nafter"
        content, _ = process_template(template, {})
        assert "\n\n\n" not in content
        assert "before" in content
        assert "after" in content

    def test_multiple_conditional_blocks(self):
        template = (
            "{{#if a}}A{{/if}}"
            " middle "
            "{{#if b}}B{{/if}}"
        )
        # Both present
        content, _ = process_template(template, {"a": "1", "b": "2"})
        assert content == "A middle B"

        # Only first
        content, _ = process_template(template, {"a": "1"})
        assert content == "A middle "

        # Only second
        content, _ = process_template(template, {"b": "2"})
        assert content == " middle B"

        # Neither
        content, _ = process_template(template, {})
        assert content == " middle "

    def test_conditional_at_start_of_template(self):
        template = "{{#if header}}# Title\n{{/if}}body"
        content, _ = process_template(template, {"header": "yes"})
        assert content == "# Title\nbody"

        content, _ = process_template(template, {})
        assert content == "body"

    def test_conditional_at_end_of_template(self):
        template = "body\n{{#if footer}}---\nThe End{{/if}}"
        content, _ = process_template(template, {"footer": "yes"})
        assert content == "body\n---\nThe End"

        # The \n before the block is not part of the conditional, so it stays.
        content, _ = process_template(template, {})
        assert content == "body\n"

    def test_conditional_at_very_end_no_preceding_newline(self):
        template = "body{{#if footer}}---{{/if}}"
        content, _ = process_template(template, {})
        assert content == "body"

    def test_multiline_body_preserved(self):
        template = "{{#if block}}line1\nline2\nline3{{/if}}"
        content, _ = process_template(template, {"block": "yes"})
        assert content == "line1\nline2\nline3"

    def test_multiline_body_removed(self):
        template = "above\n{{#if block}}line1\nline2\nline3\n{{/if}}below"
        content, _ = process_template(template, {})
        assert "line1" not in content
        assert "line2" not in content
        assert "line3" not in content
