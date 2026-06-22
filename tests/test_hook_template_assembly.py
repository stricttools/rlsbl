"""Tests for hook template removal (hooks are now config-driven).

The hook template files (pre-checks.sh.tpl, pre-release.sh.tpl,
post-release.sh.tpl) have been removed. Hooks are now configured via
the ``hooks`` key in config.json, not scaffolded as shell scripts.
"""

from __future__ import annotations

import os

TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)


class TestHookTemplatesRemoved:
    """Verify hook template files no longer exist."""

    def test_hook_templates_directory_does_not_exist(self):
        hooks_dir = os.path.join(TEMPLATES_ROOT, "shared", "hooks")
        assert not os.path.isdir(hooks_dir), (
            "Hook templates directory should not exist. "
            "Hooks are now config-driven."
        )

    def test_no_hook_tpl_files(self):
        """No .tpl files for hooks should exist in the shared templates."""
        shared_dir = os.path.join(TEMPLATES_ROOT, "shared")
        if not os.path.isdir(shared_dir):
            return
        for root, dirs, files in os.walk(shared_dir):
            for f in files:
                assert "hook" not in f.lower() or not f.endswith(".tpl"), (
                    f"Found hook template {os.path.join(root, f)} -- "
                    "hook templates should be removed"
                )
