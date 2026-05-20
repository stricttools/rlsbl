"""Tests for rlsbl.npm_wrapper platform models and helpers."""

from rlsbl.npm_wrapper import (
    DEFAULT_PLATFORMS,
    PlatformArtifact,
    PlatformSpec,
    build_artifacts,
    build_npm_publish_jobs,
    load_platform_config,
    npm_wrapper_template_mappings,
)


class TestDefaultPlatforms:
    """Tests for the DEFAULT_PLATFORMS constant."""

    def test_has_six_entries(self):
        assert len(DEFAULT_PLATFORMS) == 6

    def test_correct_npm_platform_values(self):
        names = [spec.npm_platform for spec in DEFAULT_PLATFORMS]
        assert names == [
            "linux-x64",
            "linux-arm64",
            "darwin-x64",
            "darwin-arm64",
            "win32-x64",
            "win32-arm64",
        ]

    def test_all_are_platform_spec_instances(self):
        for spec in DEFAULT_PLATFORMS:
            assert isinstance(spec, PlatformSpec)


class TestPlatformArtifact:
    """Tests for PlatformArtifact construction."""

    def test_construction(self):
        artifact = PlatformArtifact(
            npm_platform="linux-x64",
            os_constraint="linux",
            cpu_constraint="x64",
            asset_pattern="myapp_1.0.0_linux_amd64.tar.gz",
            extract_cmd="tar xzf",
            binary_name="myapp",
        )
        assert artifact.npm_platform == "linux-x64"
        assert artifact.os_constraint == "linux"
        assert artifact.cpu_constraint == "x64"
        assert artifact.asset_pattern == "myapp_1.0.0_linux_amd64.tar.gz"
        assert artifact.extract_cmd == "tar xzf"
        assert artifact.binary_name == "myapp"

    def test_extract_cmd_can_be_none(self):
        artifact = PlatformArtifact(
            npm_platform="win32-x64",
            os_constraint="win32",
            cpu_constraint="x64",
            asset_pattern="myapp.exe",
            extract_cmd=None,
            binary_name="myapp.exe",
        )
        assert artifact.extract_cmd is None


class TestLoadPlatformConfig:
    """Tests for load_platform_config."""

    def test_no_config_returns_all_six(self):
        result = load_platform_config({})
        assert len(result) == 6

    def test_empty_npm_wrapper_returns_all_six(self):
        result = load_platform_config({"npm_wrapper": {}})
        assert len(result) == 6

    def test_filter_to_subset(self):
        config = {"npm_wrapper": {"platforms": ["linux-x64", "darwin-arm64"]}}
        result = load_platform_config(config)
        assert len(result) == 2
        names = [spec.npm_platform for spec in result]
        assert names == ["linux-x64", "darwin-arm64"]

    def test_filter_preserves_order(self):
        # Even if the config lists them in a different order, the result
        # follows DEFAULT_PLATFORMS ordering.
        config = {"npm_wrapper": {"platforms": ["darwin-arm64", "linux-x64"]}}
        result = load_platform_config(config)
        names = [spec.npm_platform for spec in result]
        assert names == ["linux-x64", "darwin-arm64"]

    def test_filter_with_unknown_platform_ignored(self):
        config = {"npm_wrapper": {"platforms": ["linux-x64", "freebsd-x64"]}}
        result = load_platform_config(config)
        assert len(result) == 1
        assert result[0].npm_platform == "linux-x64"

    def test_returns_copies_not_originals(self):
        result = load_platform_config({})
        assert result is not DEFAULT_PLATFORMS


class TestBuildArtifacts:
    """Tests for build_artifacts."""

    def test_combines_specs_with_archive_fn(self):
        specs = [
            PlatformSpec("linux-x64", "linux", "x64"),
            PlatformSpec("darwin-arm64", "darwin", "arm64"),
        ]

        def archive_fn(spec, name):
            ext = ".exe" if spec.os_constraint == "win32" else ""
            return (
                f"{name}_1.0.0_{spec.os_constraint}_{spec.cpu_constraint}.tar.gz",
                "tar xzf",
                f"{name}{ext}",
            )

        artifacts = build_artifacts(specs, "myapp", archive_fn)

        assert len(artifacts) == 2
        assert all(isinstance(a, PlatformArtifact) for a in artifacts)

        linux = artifacts[0]
        assert linux.npm_platform == "linux-x64"
        assert linux.os_constraint == "linux"
        assert linux.cpu_constraint == "x64"
        assert linux.asset_pattern == "myapp_1.0.0_linux_x64.tar.gz"
        assert linux.extract_cmd == "tar xzf"
        assert linux.binary_name == "myapp"

        darwin = artifacts[1]
        assert darwin.npm_platform == "darwin-arm64"
        assert darwin.asset_pattern == "myapp_1.0.0_darwin_arm64.tar.gz"

    def test_archive_fn_can_return_none_extract_cmd(self):
        specs = [PlatformSpec("win32-x64", "win32", "x64")]

        def archive_fn(spec, name):
            return (f"{name}.exe", None, f"{name}.exe")

        artifacts = build_artifacts(specs, "tool", archive_fn)
        assert len(artifacts) == 1
        assert artifacts[0].extract_cmd is None
        assert artifacts[0].binary_name == "tool.exe"

    def test_empty_specs_returns_empty(self):
        artifacts = build_artifacts([], "myapp", lambda s, n: ("", None, ""))
        assert artifacts == []


class TestBuildNpmPublishJobs:
    """Tests for build_npm_publish_jobs."""

    def _make_go_artifacts(self, name="mycli"):
        """Helper: build Go-style artifacts for all 6 default platforms."""
        goreleaser_map = {
            "linux-x64": ("linux_amd64", "tar.gz"),
            "linux-arm64": ("linux_arm64", "tar.gz"),
            "darwin-x64": ("darwin_amd64", "tar.gz"),
            "darwin-arm64": ("darwin_arm64", "tar.gz"),
            "win32-x64": ("windows_amd64", "zip"),
            "win32-arm64": ("windows_arm64", "zip"),
        }

        def archive_fn(spec, n):
            suffix, ext = goreleaser_map[spec.npm_platform]
            asset = f"{{name}}_{{version}}_{suffix}.{ext}"
            extract = "tar xzf" if ext == "tar.gz" else "unzip"
            binary = n + (".exe" if "win32" in spec.npm_platform else "")
            return (asset, extract, binary)

        return build_artifacts(DEFAULT_PLATFORMS, name, archive_fn)

    def test_returns_string(self):
        artifacts = self._make_go_artifacts()
        result = build_npm_publish_jobs("@testuser", "mycli", artifacts)
        assert isinstance(result, str)

    def test_contains_npm_publish_job(self):
        artifacts = self._make_go_artifacts()
        result = build_npm_publish_jobs("@testuser", "mycli", artifacts)
        assert "npm-publish:" in result
        assert "needs: [goreleaser]" in result

    def test_contains_extract_steps_for_all_platforms(self):
        artifacts = self._make_go_artifacts()
        result = build_npm_publish_jobs("@testuser", "mycli", artifacts)
        assert "tar xzf mycli_${VERSION}_linux_amd64.tar.gz -C npm-wrapper/linux-x64/" in result
        assert "tar xzf mycli_${VERSION}_linux_arm64.tar.gz -C npm-wrapper/linux-arm64/" in result
        assert "tar xzf mycli_${VERSION}_darwin_amd64.tar.gz -C npm-wrapper/darwin-x64/" in result
        assert "tar xzf mycli_${VERSION}_darwin_arm64.tar.gz -C npm-wrapper/darwin-arm64/" in result
        assert "unzip mycli_${VERSION}_windows_amd64.zip -d npm-wrapper/win32-x64/" in result
        assert "unzip mycli_${VERSION}_windows_arm64.zip -d npm-wrapper/win32-arm64/" in result

    def test_contains_publish_steps_for_all_platforms(self):
        artifacts = self._make_go_artifacts()
        result = build_npm_publish_jobs("@testuser", "mycli", artifacts)
        for spec in DEFAULT_PLATFORMS:
            assert f"cd npm-wrapper/{spec.npm_platform} && npm publish --access public" in result

    def test_contains_version_stamp_step(self):
        artifacts = self._make_go_artifacts()
        result = build_npm_publish_jobs("@testuser", "mycli", artifacts)
        assert "Stamp version" in result
        assert "0.0.0/$VERSION" in result

    def test_contains_wrapper_publish_step(self):
        artifacts = self._make_go_artifacts()
        result = build_npm_publish_jobs("@testuser", "mycli", artifacts)
        assert "cd npm-wrapper && npm publish --access public" in result

    def test_contains_npm_token_env(self):
        artifacts = self._make_go_artifacts()
        result = build_npm_publish_jobs("@testuser", "mycli", artifacts)
        assert "NODE_AUTH_TOKEN" in result
        assert "NPM_TOKEN" in result

    def test_subset_of_platforms(self):
        """Only listed platforms appear when artifacts are filtered."""
        specs = [
            PlatformSpec("linux-x64", "linux", "x64"),
            PlatformSpec("darwin-arm64", "darwin", "arm64"),
        ]

        def archive_fn(spec, name):
            return (f"{{name}}_{{version}}_{spec.npm_platform}.tar.gz", "tar xzf", name)

        artifacts = build_artifacts(specs, "tool", archive_fn)
        result = build_npm_publish_jobs("@scope", "tool", artifacts)
        assert "linux-x64" in result
        assert "darwin-arm64" in result
        assert "win32-x64" not in result
        assert "linux-arm64" not in result

    def test_empty_artifacts_still_valid(self):
        result = build_npm_publish_jobs("@scope", "tool", [])
        assert "npm-publish:" in result
        assert "npm publish" in result

    def test_default_depends_on_goreleaser(self):
        """Default depends_on produces needs: [goreleaser] (Go convention)."""
        artifacts = self._make_go_artifacts()
        result = build_npm_publish_jobs("@testuser", "mycli", artifacts)
        assert "needs: [goreleaser]" in result

    def test_custom_depends_on(self):
        """Custom depends_on overrides the job dependency (e.g. Zig)."""
        artifacts = self._make_go_artifacts()
        result = build_npm_publish_jobs(
            "@testuser", "mycli", artifacts, depends_on="build-and-upload"
        )
        assert "needs: [build-and-upload]" in result
        assert "goreleaser" not in result


class TestNpmWrapperTemplateMappings:
    """Tests for npm_wrapper_template_mappings."""

    def test_returns_list_of_dicts(self):
        mappings = npm_wrapper_template_mappings()
        assert isinstance(mappings, list)
        for m in mappings:
            assert isinstance(m, dict)
            assert "template" in m
            assert "target" in m

    def test_has_wrapper_package_json(self):
        mappings = npm_wrapper_template_mappings()
        targets = [m["target"] for m in mappings]
        assert "npm-wrapper/package.json" in targets

    def test_has_bin_index_js(self):
        mappings = npm_wrapper_template_mappings()
        targets = [m["target"] for m in mappings]
        assert "npm-wrapper/bin/index.js" in targets

    def test_has_all_six_platform_packages(self):
        mappings = npm_wrapper_template_mappings()
        targets = [m["target"] for m in mappings]
        for spec in DEFAULT_PLATFORMS:
            expected = f"npm-wrapper/{spec.npm_platform}/package.json"
            assert expected in targets, f"Missing {expected}"

    def test_total_count(self):
        """2 common files + 6 platform files = 8 mappings."""
        mappings = npm_wrapper_template_mappings()
        assert len(mappings) == 8

    def test_templates_point_to_shared_dir(self):
        mappings = npm_wrapper_template_mappings()
        for m in mappings:
            assert m["template"].startswith("npm-wrapper/")
            assert m["template"].endswith(".tpl")
