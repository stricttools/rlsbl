"""Tests for Phase 7.1: idempotent publish with probe-first pattern.

Tests that each pipeline's publish() calls probe_before_publish() and
handles already-published errors as success. Also tests per-pipeline
resume via published_targets in release state.
"""

import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

from rlsbl.pipelines.base import BasePipeline, _ALREADY_EXISTS_SIGNATURES
from rlsbl.publication_probe import PublicationProbeResult, PublicationStatus


class TestProbeBeforePublish(unittest.TestCase):
    """Tests for BasePipeline.probe_before_publish()."""

    def _make_pipeline(self, target_name=None):
        """Create a BasePipeline instance with optional target link."""
        pl = BasePipeline("test", "test", local=True, config={})
        pl.target = target_name
        return pl

    def test_no_target_link_proceeds(self):
        """When pipeline has no target link, probe returns True (proceed)."""
        pl = self._make_pipeline(target_name=None)
        self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))

    def test_unknown_target_proceeds(self):
        """When target name not in TARGETS registry, probe returns True."""
        pl = self._make_pipeline(target_name="nonexistent")
        self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))

    def test_target_without_probe_capability_proceeds(self):
        """When target lacks publication_probe capability, proceed."""
        target = MagicMock()
        target.capabilities = frozenset({"read_name"})
        mock_targets = {"npm": target}
        pl = self._make_pipeline(target_name="npm")
        with patch("rlsbl.targets.TARGETS", mock_targets):
            self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))

    def test_published_skips(self):
        """When probe returns PUBLISHED, probe_before_publish returns False."""
        target = MagicMock()
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            status=PublicationStatus.PUBLISHED,
            registry="npm",
            version="1.0.0",
            message="pkg@1.0.0 found on npm",
        )
        mock_targets = {"npm": target}
        pl = self._make_pipeline(target_name="npm")

        with patch("rlsbl.targets.TARGETS", mock_targets), \
             patch("sys.stdout", new_callable=StringIO) as out:
            result = pl.probe_before_publish("/dir", "1.0.0", ctx=None)

        self.assertFalse(result)
        self.assertIn("already published", out.getvalue())

    def test_unpublished_proceeds(self):
        """When probe returns UNPUBLISHED, probe_before_publish returns True."""
        target = MagicMock()
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            status=PublicationStatus.UNPUBLISHED,
            registry="npm",
            version="1.0.0",
            message="not found",
        )
        mock_targets = {"npm": target}
        pl = self._make_pipeline(target_name="npm")
        with patch("rlsbl.targets.TARGETS", mock_targets):
            self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))

    def test_unprobeable_proceeds(self):
        """When probe returns UNPROBEABLE, probe_before_publish returns True."""
        target = MagicMock()
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            status=PublicationStatus.UNPROBEABLE,
            registry="npm",
            version="1.0.0",
            message="API error",
        )
        mock_targets = {"npm": target}
        pl = self._make_pipeline(target_name="npm")
        with patch("rlsbl.targets.TARGETS", mock_targets):
            self.assertTrue(pl.probe_before_publish("/dir", "1.0.0", ctx=None))


class TestIsAlreadyPublishedError(unittest.TestCase):
    """Tests for BasePipeline.is_already_published_error()."""

    def test_npm_e403_signature(self):
        exc = RuntimeError("npm ERR! previously published version 1.0.0")
        self.assertTrue(BasePipeline.is_already_published_error(exc))

    def test_npm_epublishconflict(self):
        exc = RuntimeError("EPUBLISHCONFLICT")
        self.assertTrue(BasePipeline.is_already_published_error(exc))

    def test_pypi_file_already_exists(self):
        exc = RuntimeError("File already exists: pkg-1.0.0.tar.gz")
        self.assertTrue(BasePipeline.is_already_published_error(exc))

    def test_crates_already_uploaded(self):
        exc = RuntimeError("crate version 1.0.0 already uploaded")
        self.assertTrue(BasePipeline.is_already_published_error(exc))

    def test_unrelated_error(self):
        exc = RuntimeError("network timeout")
        self.assertFalse(BasePipeline.is_already_published_error(exc))


class TestPipelineProbeIntegration(unittest.TestCase):
    """Tests that pipeline publish() methods call probe and handle errors."""

    @patch("rlsbl.pipelines.pypi.run")
    def test_pypi_publish_calls_probe(self, mock_run):
        """PypiPipeline.publish() calls probe_before_publish."""
        from rlsbl.pipelines.pypi import PypiPipeline
        import os

        pl = PypiPipeline("pypi", "pypi", local=True, config={})
        pl.target = "pypi"

        with patch.object(pl, "probe_before_publish", return_value=False) as mock_probe:
            pl.publish("/dir", "1.0.0", ctx=None)

        mock_probe.assert_called_once_with("/dir", "1.0.0", None)
        # publish should NOT have called run since probe said skip
        mock_run.assert_not_called()

    @patch("rlsbl.pipelines.npm.run")
    def test_npm_publish_calls_probe(self, mock_run):
        """NpmPipeline.publish() calls probe_before_publish."""
        from rlsbl.pipelines.npm import NpmPipeline

        pl = NpmPipeline("npm", "npm", local=True, config={})
        pl.target = "npm"

        with patch.object(pl, "probe_before_publish", return_value=False) as mock_probe:
            pl.publish("/dir", "1.0.0", ctx=None)

        mock_probe.assert_called_once_with("/dir", "1.0.0", None)
        mock_run.assert_not_called()

    @patch("rlsbl.pipelines.cargo.run")
    def test_cargo_publish_calls_probe(self, mock_run):
        """CargoPipeline.publish() calls probe_before_publish."""
        from rlsbl.pipelines.cargo import CargoPipeline

        pl = CargoPipeline("cargo", "cargo", local=True, config={})
        pl.target = "cargo"

        with patch.object(pl, "probe_before_publish", return_value=False) as mock_probe:
            pl.publish("/dir", "1.0.0", ctx=None)

        mock_probe.assert_called_once_with("/dir", "1.0.0", None)
        mock_run.assert_not_called()

    @patch("rlsbl.pipelines.go.read_go_module_path", return_value="example.com/mod")
    @patch("rlsbl.pipelines.go.run")
    def test_go_publish_calls_probe(self, mock_run, mock_modpath):
        """GoPipeline.publish() calls probe_before_publish."""
        from rlsbl.pipelines.go import GoPipeline

        pl = GoPipeline("go", "go", local=True, config={"install_paths": ["./cmd/x"]})
        pl.target = "go"

        with patch.object(pl, "probe_before_publish", return_value=False) as mock_probe:
            pl.publish("/dir", "1.0.0", ctx=None)

        mock_probe.assert_called_once_with("/dir", "1.0.0", None)
        mock_run.assert_not_called()

    @patch("rlsbl.pipelines.pypi.run")
    def test_pypi_already_exists_treated_as_success(self, mock_run):
        """PypiPipeline treats 'File already exists' error as success."""
        import subprocess
        from rlsbl.pipelines.pypi import PypiPipeline

        pl = PypiPipeline("pypi", "pypi", local=True, config={})
        pl.target = "pypi"

        # First call (build) succeeds, second call (publish) raises
        call_count = {"n": 0}
        def run_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                exc = subprocess.CalledProcessError(
                    1, "uv publish",
                )
                exc.stderr = "File already exists: pkg-1.0.0.tar.gz"
                raise exc

        mock_run.side_effect = run_effect

        with patch.object(pl, "probe_before_publish", return_value=True), \
             patch.dict("os.environ", {"PYPI_TOKEN": "tok"}), \
             patch("sys.stdout", new_callable=StringIO) as out:
            # Should not raise
            pl.publish("/dir", "1.0.0", ctx=None)

        self.assertIn("already exists", out.getvalue())


class TestPublishedTargetsResume(unittest.TestCase):
    """Tests for published_targets tracking in release state."""

    def test_published_targets_skips_done_pipeline(self):
        """When a pipeline name is in published_targets, it is skipped."""
        from rlsbl.commands.release.release_state import (
            load_release_state, save_release_state,
        )
        import tempfile, os, json

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "in-progress.json")
            state = {
                "completed_steps": ["TAGGED", "PUSHED", "GITHUB_RELEASE"],
                "published_targets": ["pypi-pl"],
            }
            save_release_state(state_path, state)

            loaded = load_release_state(state_path)
            self.assertIn("pypi-pl", loaded["published_targets"])


class TestPypiCheckUrl(unittest.TestCase):
    """Test that PyPI pipeline passes --check-url to uv publish."""

    @patch("rlsbl.pipelines.pypi.run")
    def test_publish_passes_check_url(self, mock_run):
        from rlsbl.pipelines.pypi import PypiPipeline

        pl = PypiPipeline("pypi", "pypi", local=True, config={})
        pl.target = "pypi"

        with patch.object(pl, "probe_before_publish", return_value=True), \
             patch.dict("os.environ", {"PYPI_TOKEN": "tok"}):
            pl.publish("/dir", "1.0.0", ctx=None)

        # Find the publish call (second call, after build)
        publish_call = mock_run.call_args_list[1]
        args = publish_call[0]
        self.assertEqual(args[0], "uv")
        self.assertIn("--check-url", args[1])
        self.assertIn("https://pypi.org/simple/", args[1])


class TestCITemplateProbes(unittest.TestCase):
    """Tests that publish templates contain probe steps."""

    def _read_template(self, *path_parts):
        import os
        tpl_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates",
        )
        tpl_path = os.path.join(tpl_dir, *path_parts)
        with open(tpl_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_npm_publish_has_probe(self):
        content = self._read_template("npm", "publish.yml.tpl")
        self.assertIn("Check if already published", content)
        self.assertIn("check-npm", content)

    def test_pypi_publish_has_skip_existing(self):
        content = self._read_template("pypi", "publish.yml.tpl")
        self.assertIn("skip-existing: true", content)

    def test_cargo_publish_has_probe(self):
        content = self._read_template("cargo", "publish.yml.tpl")
        self.assertIn("Check if already published", content)
        self.assertIn("check-cargo", content)

    def test_hex_publish_has_probe(self):
        content = self._read_template("hex", "publish.yml.tpl")
        self.assertIn("Check if already published", content)
        self.assertIn("check-hex", content)

    def test_deno_publish_has_probe(self):
        content = self._read_template("deno", "publish.yml.tpl")
        self.assertIn("Check if already published", content)
        self.assertIn("check-deno", content)

    def test_go_publish_has_probe(self):
        content = self._read_template("go", "publish.yml.tpl")
        self.assertIn("Check if already published", content)
        self.assertIn("check-go", content)

    def test_docker_publish_has_probe(self):
        content = self._read_template("docker", "publish.yml.tpl")
        self.assertIn("Check if already published", content)
        self.assertIn("check-docker", content)

    def test_maven_central_publish_has_probe(self):
        content = self._read_template("maven", "publish-central.yml.tpl")
        self.assertIn("Check if already published", content)
        self.assertIn("check-maven-central", content)

    def test_zig_publish_has_probe(self):
        content = self._read_template("zig", "publish.yml.tpl")
        self.assertIn("Check if already published", content)
        self.assertIn("check-zig", content)

    def test_maven_gp_inherently_idempotent(self):
        """Maven (GitHub Packages) template is inherently idempotent (overwrites)."""
        content = self._read_template("maven", "publish.yml.tpl")
        self.assertIn("inherently idempotent", content)


class TestNpmWrapperPerPackageProbes(unittest.TestCase):
    """Tests for npm wrapper per-package probes in publish jobs."""

    def test_platform_packages_have_probes(self):
        """Each platform package publish line includes an npm view probe."""
        from rlsbl.npm_wrapper import (
            build_npm_publish_jobs, PlatformArtifact,
        )

        artifacts = [
            PlatformArtifact("linux-x64", "linux", "x64", "a.tar.gz", "tar xzf", "mycli"),
            PlatformArtifact("darwin-arm64", "darwin", "arm64", "b.tar.gz", "tar xzf", "mycli"),
        ]

        result = build_npm_publish_jobs("mycli", artifacts)

        # Each platform package should have an npm view probe (bare names)
        self.assertIn('npm view "mycli-linux-x64@${VERSION}"', result)
        self.assertIn('npm view "mycli-darwin-arm64@${VERSION}"', result)
        self.assertIn("Already published:", result)

    def test_wrapper_package_has_probe(self):
        """The meta wrapper package publish is gated on a probe step."""
        from rlsbl.npm_wrapper import (
            build_npm_publish_jobs, PlatformArtifact,
        )

        artifacts = [
            PlatformArtifact("linux-x64", "linux", "x64", "a.tar.gz", "tar xzf", "mycli"),
        ]

        result = build_npm_publish_jobs("mycli", artifacts)

        self.assertIn("Check if wrapper already published", result)
        self.assertIn("check-wrapper", result)
        self.assertIn("steps.check-wrapper.outputs.skip != 'true'", result)


class TestGoProbeRework(unittest.TestCase):
    """Tests for Go target publication_probe using git ls-remote."""

    @patch("subprocess.run")
    def test_tag_exists_returns_published(self, mock_run):
        """When git ls-remote returns output for the tag, it is PUBLISHED."""
        from rlsbl.targets.go import GoTarget

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123\trefs/tags/v1.0.0\n",
            stderr="",
        )

        target = GoTarget()
        result = target.publication_probe("/dir", "1.0.0")

        self.assertEqual(result.status, PublicationStatus.PUBLISHED)
        self.assertIn("tag v1.0.0 exists", result.message)

        # Verify git ls-remote was called correctly
        call_args = mock_run.call_args
        self.assertEqual(
            call_args[0][0],
            ["git", "ls-remote", "--tags", "origin", "v1.0.0"],
        )

    @patch("subprocess.run")
    def test_tag_missing_returns_unpublished(self, mock_run):
        """When git ls-remote returns empty output, it is UNPUBLISHED."""
        from rlsbl.targets.go import GoTarget

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )

        target = GoTarget()
        result = target.publication_probe("/dir", "2.0.0")

        self.assertEqual(result.status, PublicationStatus.UNPUBLISHED)
        self.assertIn("not found", result.message)

    @patch("subprocess.run")
    def test_ls_remote_failure_returns_unprobeable(self, mock_run):
        """When git ls-remote fails, returns UNPROBEABLE."""
        from rlsbl.targets.go import GoTarget

        mock_run.return_value = MagicMock(
            returncode=128,
            stdout="",
            stderr="fatal: could not read from remote repository",
        )

        target = GoTarget()
        result = target.publication_probe("/dir", "1.0.0")

        self.assertEqual(result.status, PublicationStatus.UNPROBEABLE)
        self.assertIn("git ls-remote failed", result.message)

    @patch("subprocess.run")
    def test_timeout_returns_unprobeable(self, mock_run):
        """When git ls-remote times out, returns UNPROBEABLE."""
        import subprocess
        from rlsbl.targets.go import GoTarget

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        target = GoTarget()
        result = target.publication_probe("/dir", "1.0.0")

        self.assertEqual(result.status, PublicationStatus.UNPROBEABLE)
        self.assertIn("error", result.message)


class TestRecoveryDispatch(unittest.TestCase):
    """Tests for Phase 7.5: recovery dispatch with tag input."""

    def test_all_publish_templates_have_tag_input(self):
        """All 13 publish templates have workflow_dispatch.inputs.tag."""
        import os, glob

        tpl_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates",
        )
        templates = sorted(glob.glob(os.path.join(tpl_dir, "*/publish*.yml.tpl")))
        self.assertEqual(len(templates), 13)

        for tpl_path in templates:
            with open(tpl_path) as f:
                content = f.read()
            name = os.path.basename(os.path.dirname(tpl_path)) + "/" + os.path.basename(tpl_path)
            self.assertIn("inputs:", content, f"{name} missing inputs block")
            self.assertIn("tag:", content, f"{name} missing tag input")

    def test_all_publish_templates_have_tag_concurrency(self):
        """All 13 publish templates use tag-based concurrency."""
        import os, glob

        tpl_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates",
        )
        templates = sorted(glob.glob(os.path.join(tpl_dir, "*/publish*.yml.tpl")))

        for tpl_path in templates:
            with open(tpl_path) as f:
                content = f.read()
            name = os.path.basename(os.path.dirname(tpl_path)) + "/" + os.path.basename(tpl_path)
            self.assertIn("inputs.tag || github.ref_name", content,
                          f"{name} missing tag-based concurrency")

    def test_all_publish_templates_checkout_with_tag_ref(self):
        """All 13 publish templates checkout with inputs.tag fallback."""
        import os, glob

        tpl_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates",
        )
        templates = sorted(glob.glob(os.path.join(tpl_dir, "*/publish*.yml.tpl")))

        for tpl_path in templates:
            with open(tpl_path) as f:
                content = f.read()
            name = os.path.basename(os.path.dirname(tpl_path)) + "/" + os.path.basename(tpl_path)
            self.assertIn("inputs.tag || github.event.release.tag_name", content,
                          f"{name} missing tag-based checkout ref")

    def test_retry_config_has_tag_field(self):
        """RetryConfig has a tag field."""
        from rlsbl.release_file import RetryConfig
        config = RetryConfig(
            version="1.0.0",
            dispatch=["publish.yml"],
            ref="main",
            tag="v1.0.0",
        )
        self.assertEqual(config.tag, "v1.0.0")

    def test_read_retry_file_tag_defaults_to_ref(self):
        """When tag is absent from retry.toml, it defaults to ref."""
        import tempfile, os
        import tomlkit as tk

        doc = tk.document()
        doc.add("version", "1.0.0")
        doc.add("dispatch", ["publish.yml"])
        doc.add("ref", "v1.0.0")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            tk.dump(doc, f)
            path = f.name

        try:
            from rlsbl.release_file import read_retry_file
            config = read_retry_file(path)
            self.assertEqual(config.tag, "v1.0.0")
        finally:
            os.unlink(path)

    def test_read_retry_file_explicit_tag(self):
        """When tag is set explicitly, it overrides the ref default."""
        import tempfile, os
        import tomlkit as tk

        doc = tk.document()
        doc.add("version", "1.0.0")
        doc.add("dispatch", ["publish.yml"])
        doc.add("ref", "main")
        doc.add("tag", "v1.0.0")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            tk.dump(doc, f)
            path = f.name

        try:
            from rlsbl.release_file import read_retry_file
            config = read_retry_file(path)
            self.assertEqual(config.ref, "main")
            self.assertEqual(config.tag, "v1.0.0")
        finally:
            os.unlink(path)

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run_gh", return_value="")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.resolve_member_context")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_retry_dispatch_passes_tag_input(self, _gh_inst, _gh_auth, _ws_root,
                                              mock_targets_dict, mock_detect,
                                              _exists, mock_run, mock_run_gh,
                                              mock_cleanup):
        """retry dispatch passes -f tag=<tag> to gh workflow run."""
        from rlsbl.commands.release_retry import run_cmd
        from rlsbl.release_file import RetryConfig

        target = MagicMock()
        target.read_version.return_value = "1.0.0"
        target.tag_format.side_effect = lambda v: f"v{v}"
        entry = MagicMock()
        entry.name = "pypi"
        entry.path = "."
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        def run_effect(*args, **kwargs):
            cmd, cmd_args = args[0], args[1] if len(args) > 1 else []
            if cmd == "git" and cmd_args[:2] == ["rev-list", "-1"]:
                return "abc123def456789012345678901234567890abcd"
            return ""

        mock_run.side_effect = run_effect

        config = RetryConfig(
            version="1.0.0",
            dispatch=["publish.yml"],
            ref="main",
            tag="v1.0.0",
        )

        from io import StringIO
        import time
        with patch("rlsbl.commands.release_retry.time.sleep"):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(config, {"yes": True}, project_root=".")

        # Find the workflow run call via run_gh
        workflow_calls = [c for c in mock_run_gh.call_args_list
                          if len(c[0]) >= 1 and c[0][0][:2] == ["workflow", "run"]]
        self.assertEqual(len(workflow_calls), 1)

        dispatch_args = workflow_calls[0][0][0]
        self.assertIn("-f", dispatch_args)
        tag_idx = dispatch_args.index("-f")
        self.assertEqual(dispatch_args[tag_idx + 1], "tag=v1.0.0")


if __name__ == "__main__":
    unittest.main()
