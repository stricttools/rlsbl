"""Tests for Phase 12 checks: private-publish-workflow, scaffold-conflict-markers,
npm-private-mismatch, target-version-readable, selfdoc-version-drift."""

import json
import os

from conftest import make_ctx

from rlsbl import app


# ------------------------------------------------------------------
# private-publish-workflow
# ------------------------------------------------------------------


class TestPrivatePublishWorkflow:
    """Tests for the private-publish-workflow check."""

    def test_public_repo_passes(self, tmp_project):
        """Non-private repo always passes regardless of workflows."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"private": False}))
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("name: Publish\non: push\n")
        ctx = make_ctx(tmp_project)
        result = app._check_defs["private-publish-workflow"].impl(ctx)
        assert result.status == "pass"
        assert "not a private repo" in result.message

    def test_private_repo_with_publish_filename_fails(self, tmp_project):
        """Private repo with a file named 'publish.yml' fails."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"private": True}))
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("name: Publish\non: push\n")
        ctx = make_ctx(tmp_project)
        result = app._check_defs["private-publish-workflow"].impl(ctx)
        assert result.status == "fail"
        assert "publish.yml" in result.message

    def test_private_repo_with_release_published_trigger_fails(self, tmp_project):
        """Private repo with 'release: [published]' trigger fails."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"private": True}))
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "deploy.yml").write_text(
            "name: Deploy\n"
            "on:\n"
            "  release:\n"
            "    types: [published]\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["private-publish-workflow"].impl(ctx)
        assert result.status == "fail"
        assert "deploy.yml" in result.message

    def test_private_repo_no_workflows_passes(self, tmp_project):
        """Private repo with no .github/workflows/ directory passes."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"private": True}))
        ctx = make_ctx(tmp_project)
        result = app._check_defs["private-publish-workflow"].impl(ctx)
        assert result.status == "pass"

    def test_private_repo_clean_workflows_passes(self, tmp_project):
        """Private repo with only CI workflows (no publish) passes."""
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"private": True}))
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n")
        ctx = make_ctx(tmp_project)
        result = app._check_defs["private-publish-workflow"].impl(ctx)
        assert result.status == "pass"
        assert "no publish workflows" in result.message


# ------------------------------------------------------------------
# scaffold-conflict-markers
# ------------------------------------------------------------------


class TestScaffoldConflictMarkers:
    """Tests for the scaffold-conflict-markers check."""

    def test_clean_files_pass(self, tmp_project):
        """Files without conflict markers pass."""
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text('{"private": false}\n')
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("name: CI\non: push\n")
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-conflict-markers"].impl(ctx)
        assert result.status == "pass"

    def test_conflict_in_rlsbl_dir_fails(self, tmp_project):
        """Conflict markers in .rlsbl/ files are detected."""
        hooks_dir = tmp_project / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-release.sh").write_text(
            '#!/bin/bash\n'
            '<<<<<<< HEAD\n'
            'echo "old"\n'
            '=======\n'
            'echo "new"\n'
            '>>>>>>> feature\n'
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-conflict-markers"].impl(ctx)
        assert result.status == "fail"
        assert "conflict marker" in result.message

    def test_conflict_in_workflow_fails(self, tmp_project):
        """Conflict markers in .github/workflows/ are detected."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(
            "name: CI\n"
            "<<<<<<< HEAD\n"
            "on: push\n"
            "=======\n"
            "on: pull_request\n"
            ">>>>>>> other\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-conflict-markers"].impl(ctx)
        assert result.status == "fail"
        assert any("ci.yml" in d for d in result.details)

    def test_no_scaffold_files_passes(self, tmp_project):
        """Project with no .rlsbl or workflow files passes."""
        ctx = make_ctx(tmp_project)
        result = app._check_defs["scaffold-conflict-markers"].impl(ctx)
        assert result.status == "pass"


# ------------------------------------------------------------------
# npm-private-mismatch
# ------------------------------------------------------------------


class TestNpmPrivateMismatch:
    """Tests for the npm-private-mismatch check."""

    def test_no_package_json_skips(self, tmp_project):
        """No package.json returns skip."""
        ctx = make_ctx(tmp_project)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "skip"

    def test_mismatch_fails(self, tmp_project):
        """package.json private:true + config private:false fails."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0", "private": True})
        )
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"private": False}))
        ctx = make_ctx(tmp_project)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "fail"
        assert "private" in result.message

    def test_both_private_passes(self, tmp_project):
        """package.json private:true + config private:true passes."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0", "private": True})
        )
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"private": True}))
        ctx = make_ctx(tmp_project)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "pass"

    def test_both_public_passes(self, tmp_project):
        """package.json without private + config private:false passes."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"private": False}))
        ctx = make_ctx(tmp_project)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "pass"

    def test_npm_public_config_private_passes(self, tmp_project):
        """package.json without private + config private:true is fine (not a mismatch)."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        cfg_dir = tmp_project / ".rlsbl"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"private": True}))
        ctx = make_ctx(tmp_project)
        result = app._check_defs["npm-private-mismatch"].impl(ctx)
        assert result.status == "pass"


# ------------------------------------------------------------------
# target-version-readable
# ------------------------------------------------------------------


class TestTargetVersionReadable:
    """Tests for the target-version-readable check."""

    def test_valid_target_passes(self, tmp_project):
        """A valid package.json with a version passes."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["target-version-readable"].impl(ctx)
        assert result.status == "pass"
        assert "readable" in result.message

    def test_corrupt_target_fails(self, tmp_project):
        """A corrupt package.json that can't be parsed fails."""
        (tmp_project / "package.json").write_text("{invalid json!!!")
        ctx = make_ctx(tmp_project)
        result = app._check_defs["target-version-readable"].impl(ctx)
        assert result.status == "fail"
        assert "cannot read version" in result.message

    def test_no_targets_skips(self, tmp_project):
        """No detected targets returns skip."""
        ctx = make_ctx(tmp_project)
        result = app._check_defs["target-version-readable"].impl(ctx)
        assert result.status == "skip"


# ------------------------------------------------------------------
# selfdoc-version-drift
# ------------------------------------------------------------------


class TestSelfdocVersionDrift:
    """Tests for the selfdoc-version-drift check."""

    def test_no_selfdoc_skips(self, tmp_project):
        """No selfdoc.json returns skip."""
        ctx = make_ctx(tmp_project)
        result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "skip"

    def test_matching_versions_passes(self, tmp_project):
        """selfdoc.json version matching the target version passes."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.2.3"})
        )
        (tmp_project / "selfdoc.json").write_text(
            json.dumps({"version": "1.2.3"})
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "pass"
        assert "1.2.3" in result.message

    def test_mismatched_versions_fails(self, tmp_project):
        """selfdoc.json version not matching the target version fails."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.2.3"})
        )
        (tmp_project / "selfdoc.json").write_text(
            json.dumps({"version": "1.0.0"})
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "fail"
        assert "1.0.0" in result.message
        assert "1.2.3" in result.message

    def test_no_version_field_skips(self, tmp_project):
        """selfdoc.json without a version field skips."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        (tmp_project / "selfdoc.json").write_text(
            json.dumps({"name": "test"})
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["selfdoc-version-drift"].impl(ctx)
        assert result.status == "skip"
        assert "no version field" in result.message
