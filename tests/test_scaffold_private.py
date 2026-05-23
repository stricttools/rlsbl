"""Tests for private repository scaffold behavior."""

import os
from pathlib import Path

import pytest

from rlsbl.commands.init_cmd import (
    _filter_mappings_for_private,
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
        shared_mappings = base.shared_template_mappings()

        # For a private repo, no hook replacement should happen -- just use as-is
        # (previously _replace_post_release_hook_for_private would swap the template)
        created, skipped, warnings, _ = process_mappings(
            str(shared_tpl_dir), shared_mappings, {"year": "2026"},
            force=False, update=False,
        )

        # The standard post-release.sh should be created
        created_targets = [t for t, _ in created]
        assert ".rlsbl/hooks/post-release.sh" in created_targets

        # Read the content -- it should be from the standard template, not the private one
        hook_content = (tmp_project / ".rlsbl" / "hooks" / "post-release.sh").read_text()
        assert "Post-release hook for private repositories" not in hook_content
        assert "gh release upload" not in hook_content


class TestFilterMappingsForPrivate:
    """_filter_mappings_for_private removes publish workflows but keeps others."""

    def test_removes_publish_workflows(self):
        """Mappings with 'publish' in template name are removed."""
        mappings = [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
            {"template": "VERSION.tpl", "target": "VERSION"},
        ]
        filtered = _filter_mappings_for_private(mappings)
        templates = [m["template"] for m in filtered]
        assert "publish.yml.tpl" not in templates
        assert "ci.yml.tpl" in templates
        assert "VERSION.tpl" in templates

    def test_keeps_goreleaser_for_go(self):
        """goreleaser.yml.tpl does not contain 'publish' so it survives filtering."""
        mappings = [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
            {"template": "goreleaser.yml.tpl", "target": ".goreleaser.yml"},
            {"template": "VERSION.tpl", "target": "VERSION"},
        ]
        filtered = _filter_mappings_for_private(mappings)
        templates = [m["template"] for m in filtered]
        assert "goreleaser.yml.tpl" in templates
        assert "publish.yml.tpl" not in templates
