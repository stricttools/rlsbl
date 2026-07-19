"""Tests for crates.io wrapper scaffolding and publish job generation."""

import os
from unittest.mock import MagicMock

from rlsbl.crates_wrapper import (
    CRATES_PLATFORMS,
    build_crates_publish_jobs,
    crates_wrapper_template_mappings,
)


class TestCratesWrapperTemplateMappings:
    """Tests for crates_wrapper_template_mappings()."""

    def test_returns_three_mappings(self):
        mappings = crates_wrapper_template_mappings()
        assert len(mappings) == 3

    def test_cargo_toml_mapping(self):
        mappings = crates_wrapper_template_mappings()
        targets = {m["target"] for m in mappings}
        assert "crates-wrapper/Cargo.toml" in targets

    def test_main_rs_mapping(self):
        mappings = crates_wrapper_template_mappings()
        targets = {m["target"] for m in mappings}
        assert "crates-wrapper/src/main.rs" in targets

    def test_build_rs_mapping(self):
        mappings = crates_wrapper_template_mappings()
        targets = {m["target"] for m in mappings}
        assert "crates-wrapper/build.rs" in targets

    def test_template_paths_use_crates_wrapper_prefix(self):
        mappings = crates_wrapper_template_mappings()
        for m in mappings:
            assert m["template"].startswith("crates-wrapper/")


class TestBuildCratesPublishJobs:
    """Tests for build_crates_publish_jobs()."""

    def test_generates_yaml_string(self):
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_job_name(self):
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "crates-publish:" in result

    def test_depends_on_gate_and_goreleaser(self):
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "needs: [gate, goreleaser]" in result

    def test_custom_depends_on(self):
        result = build_crates_publish_jobs(
            "mytool", "smm-h/mytool", depends_on="build-and-upload",
        )
        assert "needs: [gate, build-and-upload]" in result

    def test_contains_version_extraction(self):
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "GITHUB_REF_NAME" in result
        assert "VERSION" in result

    def test_contains_version_stamp(self):
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "0.0.0" in result
        assert "crates-wrapper/Cargo.toml" in result

    def test_contains_skip_check(self):
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "check-crate" in result
        assert "Already published" in result

    def test_contains_cargo_publish(self):
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "cargo publish" in result

    def test_contains_oidc_auth(self):
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "crates-io-auth-action" in result
        assert "id-token: write" in result

    def test_working_directory_is_crates_wrapper(self):
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "working-directory: crates-wrapper" in result


class TestCratesPlatforms:
    """Tests for CRATES_PLATFORMS constant."""

    def test_six_platforms(self):
        assert len(CRATES_PLATFORMS) == 6

    def test_linux_x64_included(self):
        targets = [p[0] for p in CRATES_PLATFORMS]
        assert "x86_64-unknown-linux-gnu" in targets

    def test_darwin_arm64_included(self):
        targets = [p[0] for p in CRATES_PLATFORMS]
        assert "aarch64-apple-darwin" in targets

    def test_windows_uses_zip(self):
        for target, suffix, ext in CRATES_PLATFORMS:
            if "windows" in target:
                assert ext == "zip"

    def test_unix_uses_tar_gz(self):
        for target, suffix, ext in CRATES_PLATFORMS:
            if "linux" in target or "darwin" in target:
                assert ext == "tar.gz"


class TestGoTargetCratesWrapper:
    """Tests for crates wrapper integration in GoTarget."""

    def test_shared_template_mappings_with_crates_enabled(self, tmp_path):
        """When crates_wrapper.enabled is true, mappings include crates wrapper."""
        from rlsbl.targets.go import GoTarget

        # Set up a minimal Go project
        (tmp_path / "go.mod").write_text("module github.com/test/tool\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
        (tmp_path / "VERSION").write_text("0.1.0")

        target = GoTarget()
        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.config = {"crates_wrapper": {"enabled": True}}

        mappings = target.shared_template_mappings(ctx)
        targets = [m["target"] for m in mappings]
        assert "crates-wrapper/Cargo.toml" in targets
        assert "crates-wrapper/src/main.rs" in targets
        assert "crates-wrapper/build.rs" in targets

    def test_shared_template_mappings_without_crates_disabled(self, tmp_path):
        """When crates_wrapper is not configured, no crates mappings."""
        from rlsbl.targets.go import GoTarget

        (tmp_path / "go.mod").write_text("module github.com/test/tool\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
        (tmp_path / "VERSION").write_text("0.1.0")

        target = GoTarget()
        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.config = {}

        mappings = target.shared_template_mappings(ctx)
        targets = [m["target"] for m in mappings]
        assert "crates-wrapper/Cargo.toml" not in targets

    def test_template_vars_include_crates_publish_jobs(self, tmp_path):
        """When crates_wrapper.enabled is true, template vars include cratesPublishJobs."""
        from rlsbl.targets.go import GoTarget

        (tmp_path / "go.mod").write_text("module github.com/smm-h/tool\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
        (tmp_path / "VERSION").write_text("0.1.0")

        target = GoTarget()
        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.config = {"crates_wrapper": {"enabled": True}}

        tvars = target.template_vars(str(tmp_path), ctx)
        assert "cratesPublishJobs" in tvars
        assert "crates-publish:" in tvars["cratesPublishJobs"]

    def test_template_vars_crates_disabled_empty(self, tmp_path):
        """When crates_wrapper is not enabled, cratesPublishJobs is empty."""
        from rlsbl.targets.go import GoTarget

        (tmp_path / "go.mod").write_text("module github.com/smm-h/tool\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
        (tmp_path / "VERSION").write_text("0.1.0")

        target = GoTarget()
        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.config = {}

        tvars = target.template_vars(str(tmp_path), ctx)
        assert tvars.get("cratesPublishJobs", "") == ""

    def test_publish_setup_mentions_trusted_publishing(self, tmp_path):
        """When crates is enabled, publishSetup mentions Trusted Publishing."""
        from rlsbl.targets.go import GoTarget

        (tmp_path / "go.mod").write_text("module github.com/smm-h/tool\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
        (tmp_path / "VERSION").write_text("0.1.0")

        target = GoTarget()
        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.config = {"crates_wrapper": {"enabled": True}}

        tvars = target.template_vars(str(tmp_path), ctx)
        assert "Trusted Publishing" in tvars["publishSetup"]


class TestTemplateFilesExist:
    """Verify that all referenced template files actually exist on disk."""

    def test_cargo_toml_template_exists(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates", "shared", "crates-wrapper", "Cargo.toml.tpl",
        )
        assert os.path.isfile(path), f"Missing template: {path}"

    def test_build_rs_template_exists(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates", "shared", "crates-wrapper", "build.rs.tpl",
        )
        assert os.path.isfile(path), f"Missing template: {path}"

    def test_main_rs_template_exists(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "rlsbl", "templates", "shared", "crates-wrapper", "src", "main.rs.tpl",
        )
        assert os.path.isfile(path), f"Missing template: {path}"


def _crates_template(*parts):
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "rlsbl", "templates", "shared", "crates-wrapper", *parts,
    )
    with open(path) as f:
        return f.read()


class TestBuildRsChecksumVerification:
    """build.rs.tpl must sha256-verify the downloaded archive before extracting.

    Mirrors the good/corrupted assertion style of the npm launcher shim tests:
    goreleaser publishes checksums.txt to the same release, so the wrapper must
    fetch it, match the archive's line, compute sha256, and hard-fail on any
    mismatch, missing entry, or missing checksums.txt -- never extract an
    unverified binary.
    """

    def test_uses_sha256_via_sha2_crate(self):
        c = _crates_template("build.rs.tpl")
        assert "use sha2::" in c
        assert "Sha256" in c
        assert "hasher.update" in c

    def test_fetches_checksums_txt_from_same_release(self):
        c = _crates_template("build.rs.tpl")
        assert "checksums.txt" in c
        assert "releases/download/v{version}/checksums.txt" in c

    def test_verification_sits_between_download_and_extract(self):
        c = _crates_template("build.rs.tpl")
        download = c.index("Failed to download {url}")
        verify = c.index("Checksum mismatch")
        extract = c.index("Extract the binary")
        assert download < verify < extract

    def test_hard_fails_on_hash_mismatch(self):
        c = _crates_template("build.rs.tpl")
        assert "Checksum mismatch for {archive_name}" in c
        assert "expected {expected}, got {actual}" in c

    def test_hard_fails_on_missing_checksum_entry(self):
        c = _crates_template("build.rs.tpl")
        assert "checksums.txt has no entry for {archive_name}" in c

    def test_hard_fails_when_checksums_download_fails(self):
        c = _crates_template("build.rs.tpl")
        assert "Failed to download release checksums" in c

    def test_hashes_the_downloaded_archive(self):
        c = _crates_template("build.rs.tpl")
        assert "fs::read(&archive_path)" in c

    def test_cargo_toml_declares_sha2_build_dependency(self):
        c = _crates_template("Cargo.toml.tpl")
        assert "[build-dependencies]" in c
        assert "sha2" in c
