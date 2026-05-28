"""Tests for the {{action "..."}} placeholder in process_template.

Covers the two-pass renderer added in Phase 3 of the template-action
substitution work:

  1. Pass 1 resolves ``{{action "owner/name"}}`` against the central
     version table in ``rlsbl/data/action_versions.toml``.
  2. Pass 2 resolves the existing ``{{varName}}`` placeholders.

Action misses raise :class:`UnknownActionError` immediately so missing
entries fail loudly (no implicit defaults). Variable misses keep their
existing soft behavior (returned in the ``unreplaced`` list).
"""

from __future__ import annotations

import os
import re

import pytest

from rlsbl.action_versions import (
    UnknownActionError,
    format_action,
    get_all_versions,
)
from rlsbl.commands.init_cmd import process_template


TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)

_PLACEHOLDER_RE = re.compile(r'\{\{action\s+"([^"]+)"\}\}')


class TestActionPlaceholderResolution:
    def test_action_placeholder_resolves(self):
        content, unreplaced = process_template(
            '- uses: {{action "actions/checkout"}}', {}
        )
        expected = format_action("actions/checkout")
        assert content == f"- uses: {expected}"
        assert unreplaced == []

    def test_unknown_action_raises(self):
        with pytest.raises(UnknownActionError) as exc:
            process_template('{{action "fake/nothing"}}', {})
        assert "fake/nothing" in str(exc.value)

    def test_unknown_action_includes_template_path(self):
        with pytest.raises(UnknownActionError) as exc:
            process_template(
                '{{action "fake/nothing"}}', {}, template_path="some/file.tpl"
            )
        assert "some/file.tpl" in str(exc.value)

    def test_variable_placeholder_still_works(self):
        content, unreplaced = process_template("Hello {{name}}", {"name": "foo"})
        assert content == "Hello foo"
        assert unreplaced == []

    def test_action_and_variable_mixed(self):
        template = (
            "name: {{projectName}}\n"
            '- uses: {{action "actions/checkout"}}\n'
            '- uses: {{action "actions/setup-node"}}\n'
            "node-version: {{nodeVersion}}\n"
        )
        content, unreplaced = process_template(
            template,
            {"projectName": "myproj", "nodeVersion": "20"},
        )
        assert "name: myproj" in content
        assert f"- uses: {format_action('actions/checkout')}" in content
        assert f"- uses: {format_action('actions/setup-node')}" in content
        assert "node-version: 20" in content
        assert unreplaced == []
        # No leftover action placeholders.
        assert "{{action" not in content

    def test_no_placeholders_passthrough(self):
        content, unreplaced = process_template("plain text", {})
        assert content == "plain text"
        assert unreplaced == []

    def test_smoke_render_with_real_action_versions(self):
        """End-to-end check: feed a fake template with several action
        placeholders through process_template and confirm the output
        contains literal pinned versions, not placeholders."""
        template = """\
jobs:
  build:
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "actions/setup-python"}}
      - uses: {{action "astral-sh/setup-uv"}}
"""
        content, unreplaced = process_template(template, {})
        assert "{{action" not in content
        # Each action must appear with its pinned version.
        table = get_all_versions()
        for name in (
            "actions/checkout",
            "actions/setup-python",
            "astral-sh/setup-uv",
        ):
            assert f"{name}@{table[name]}" in content
        assert unreplaced == []


class TestEscapedPlaceholders:
    r"""Tests for the \{{ escape syntax that emits literal {{ in output."""

    def test_escaped_placeholder_emits_literal_braces(self):
        r"""``\{{version}}`` should produce ``{{version}}`` literally."""
        content, unreplaced = process_template(
            r"pattern=\{{version}}", {"version": "1.2.3"}
        )
        assert content == "pattern={{version}}"
        assert unreplaced == []

    def test_normal_placeholder_still_replaced(self):
        """Unescaped ``{{key}}`` is still substituted when key is in vars_dict."""
        content, unreplaced = process_template(
            "v={{version}}", {"version": "1.2.3"}
        )
        assert content == "v=1.2.3"
        assert unreplaced == []

    def test_github_expression_passthrough(self):
        """``${{ github.something }}`` passes through untouched (spaces
        inside prevent the variable regex from matching)."""
        template = "ref: ${{ github.ref }}"
        content, unreplaced = process_template(template, {})
        assert content == "ref: ${{ github.ref }}"
        assert unreplaced == []

    def test_mixed_escaped_and_normal(self):
        """Escaped and normal placeholders coexist in the same template."""
        template = r"name={{name}} tag=\{{version}}"
        content, unreplaced = process_template(template, {"name": "myimg"})
        assert content == "name=myimg tag={{version}}"
        assert unreplaced == []

    def test_multiple_escaped_placeholders(self):
        r"""Multiple \{{ escapes in a single template all resolve."""
        template = r"a=\{{major}}.b=\{{minor}}"
        content, unreplaced = process_template(template, {})
        assert content == "a={{major}}.b={{minor}}"
        assert unreplaced == []

    def test_escaped_action_placeholder_not_resolved(self):
        r"""``\{{action "..."}}`` should NOT be treated as an action
        placeholder -- the escape protects it."""
        template = r'literal: \{{action "actions/checkout"}}'
        content, unreplaced = process_template(template, {})
        assert content == 'literal: {{action "actions/checkout"}}'
        assert unreplaced == []

    def test_docker_template_pattern(self):
        r"""Reproduce the exact Docker metadata-action pattern to ensure
        ``\{{version}}`` survives even when ``version`` is in vars_dict."""
        template = (
            "tags: |\n"
            r"  type=semver,pattern=\{{version}}" "\n"
            "  type=raw,value=latest\n"
        )
        content, unreplaced = process_template(
            template, {"version": "2.0.0"}
        )
        assert "type=semver,pattern={{version}}" in content
        assert "2.0.0" not in content
        assert unreplaced == []
