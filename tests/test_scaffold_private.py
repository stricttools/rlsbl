"""Tests for private repository scaffold behavior."""

import os
from pathlib import Path

import pytest

from rlsbl.commands.init_cmd import (
    process_mappings,
)


class TestPrivateHookTemplateRemoved:
    """The old post-release-private.sh.tpl template no longer exists."""

    def test_private_hook_template_deleted(self):
        """post-release-private.sh.tpl must not exist in shared hooks."""
        tpl_path = (
            Path(__file__).resolve().parent.parent
            / "rlsbl"
            / "templates"
            / "shared"
            / "hooks"
            / "post-release-private.sh.tpl"
        )
        assert not tpl_path.exists(), (
            "post-release-private.sh.tpl should have been deleted"
        )

    def test_replace_function_removed(self):
        """_replace_post_release_hook_for_private must no longer exist in init_cmd."""
        import rlsbl.commands.init_cmd as init_cmd

        assert not hasattr(init_cmd, "_replace_post_release_hook_for_private"), (
            "_replace_post_release_hook_for_private should have been removed"
        )


class TestPrivateScaffoldNoHooks:
    """Scaffold with private: true does not create hook scripts (config-driven)."""

    def test_no_hook_mappings_in_shared_templates(self, tmp_project):
        """shared_template_mappings should not include hook scripts."""
        from rlsbl.targets.base import BaseTarget

        base = BaseTarget()
        shared_mappings = base.shared_template_mappings(".")

        hook_targets = [m["target"] for m in shared_mappings if "hooks" in m["target"]]
        assert hook_targets == [], (
            f"Hooks should not be in shared_template_mappings: {hook_targets}"
        )


class TestFilterMappingsForPrivateRemoved:
    """_filter_mappings_for_private has been removed -- targets no longer return publish templates."""

    def test_filter_function_removed(self):
        """_filter_mappings_for_private must no longer exist in init_cmd."""
        import rlsbl.commands.init_cmd as init_cmd
        assert not hasattr(init_cmd, "_filter_mappings_for_private"), (
            "_filter_mappings_for_private should have been removed"
        )

    def test_targets_dont_return_publish_templates(self):
        """Target template_mappings should not contain any publish templates."""
        from rlsbl.targets import TARGETS
        from rlsbl.context import ProjectContext
        from pathlib import Path

        # Use a minimal context for targets that need it
        ctx = ProjectContext(project_root=Path("."), workspace_root=None, config={})
        for name, target in TARGETS.items():
            mappings = target.template_mappings(ctx)
            for m in mappings:
                assert "publish" not in m["template"], (
                    f"Target '{name}' still returns publish template: {m['template']}"
                )
