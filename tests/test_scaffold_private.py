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


class TestPrivateScaffoldUsesStandardHook:
    """Scaffold with private: true uses the standard post-release.sh.tpl."""

    def test_standard_hook_installed_for_private(self, tmp_project):
        """Private scaffold installs the standard post-release.sh.tpl, not the private one."""
        shared_tpl_dir = (
            Path(__file__).resolve().parent.parent
            / "rlsbl"
            / "templates"
            / "shared"
        )

        # Use the real shared template mappings (which include post-release.sh)
        from rlsbl.targets.base import BaseTarget

        base = BaseTarget()
        shared_mappings = base.shared_template_mappings(".")

        # For a private repo, no hook replacement should happen -- just use as-is
        # (previously _replace_post_release_hook_for_private would swap the template)
        created, skipped, warnings, _ = process_mappings(
            str(shared_tpl_dir), shared_mappings, {"year": "2026"},
            force=False,
        )

        # The standard post-release.sh should be created
        created_targets = [t for t, _ in created]
        assert ".rlsbl/hooks/post-release.sh" in created_targets

        # Read the content -- it should be from the standard template, not the private one
        hook_content = (tmp_project / ".rlsbl" / "hooks" / "post-release.sh").read_text()
        assert "Post-release hook for private repositories" not in hook_content
        assert "gh release upload" not in hook_content


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
