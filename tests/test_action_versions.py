"""Tests for the centralized GitHub Actions version table.

Covers the loader (rlsbl.action_versions) and a structural check that
every ``{{action "..."}}`` placeholder in shipped templates resolves
against ``rlsbl/data/action_versions.toml``. After Phase 3 of the
template-action substitution work, templates no longer embed literal
``name@version`` strings -- the placeholder is the only form -- so drift
between templates and the table is impossible by construction.
"""

from __future__ import annotations

import os
import re

import pytest

from rlsbl.action_versions import (
    UnknownActionError,
    format_action,
    get_action_version,
    get_all_versions,
)


TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)

# Matches the ``{{action "owner/name"}}`` placeholder. Captures the action
# name only.
_PLACEHOLDER_RE = re.compile(r'\{\{action\s+"([^"]+)"\}\}')


class TestLoader:
    """Smoke tests for the TOML loader."""

    def test_table_loads(self):
        table = get_all_versions()
        assert isinstance(table, dict)
        assert len(table) > 0

    def test_get_known_action(self):
        assert get_action_version("actions/checkout") == "v6"

    def test_format_known_action(self):
        assert format_action("actions/checkout") == "actions/checkout@v6"

    def test_setup_node_pinned_to_v6(self):
        # Regression: npm_wrapper.py used to hard-code v4 here.
        assert get_action_version("actions/setup-node") == "v6"

    def test_paths_filter_pinned_to_v4(self):
        # Regression: monorepo router used to hard-code v3 here.
        assert get_action_version("dorny/paths-filter") == "v4"

    def test_unknown_action_raises(self):
        with pytest.raises(UnknownActionError) as exc:
            get_action_version("nonexistent/action")
        # The error message must mention both the missing action and the
        # data file so the operator knows where to add the entry.
        assert "nonexistent/action" in str(exc.value)
        assert "action_versions.toml" in str(exc.value)

    def test_unknown_action_is_keyerror(self):
        # UnknownActionError must remain a KeyError subclass so existing
        # ``except KeyError`` callers keep working.
        with pytest.raises(KeyError):
            get_action_version("nonexistent/action")

    def test_get_all_versions_is_copy(self):
        # Mutating the returned dict must not corrupt the cached table.
        snap = get_all_versions()
        snap["foo/bar"] = "vX"
        assert "foo/bar" not in get_all_versions()


class TestTemplatePlaceholders:
    """Every ``{{action "..."}}`` placeholder in shipped templates must
    resolve via :func:`format_action`. After Phase 3 templates contain no
    literal ``name@version`` strings, so resolution at scaffold time is
    the only way a version reaches a rendered workflow.
    """

    def _iter_template_files(self):
        for root, _dirs, files in os.walk(TEMPLATES_ROOT):
            for name in files:
                if name.endswith(".tpl"):
                    yield os.path.join(root, name)

    def _iter_placeholders(self):
        for path in self._iter_template_files():
            with open(path) as f:
                for lineno, line in enumerate(f, start=1):
                    for m in _PLACEHOLDER_RE.finditer(line):
                        yield path, lineno, m.group(1)

    def test_all_action_placeholders_resolve(self):
        unresolved: list[str] = []
        for path, lineno, action in self._iter_placeholders():
            try:
                format_action(action)
            except UnknownActionError as exc:
                unresolved.append(f"{path}:{lineno}: {action} ({exc})")
        assert not unresolved, (
            "Templates reference actions not in action_versions.toml:\n  "
            + "\n  ".join(unresolved)
        )

    def test_at_least_one_placeholder_found(self):
        # Guard: if the regex breaks, test_all_action_placeholders_resolve
        # would pass vacuously. Ensure the iterator actually finds many.
        found = list(self._iter_placeholders())
        assert len(found) > 10, (
            "expected to find many {{action ...}} placeholders in templates"
        )
