"""Tests for crates.io release pipeline integration.

Verifies that the crates wrapper publish job has correct sequencing,
version stamping, and publish gate integration.
"""

from rlsbl.crates_wrapper import build_crates_publish_jobs
from rlsbl.publish_gate import ci_check_regex_for_targets, CI_CHECK_JOB_NAMES


class TestCratesPublishSequencing:
    """Tests for crates.io publish job sequencing."""

    def test_default_depends_on_goreleaser(self):
        """Crates publish runs AFTER goreleaser (GitHub Release assets must exist)."""
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "needs: [gate, goreleaser]" in result

    def test_custom_depends_on_respected(self):
        """Custom dependency for non-goreleaser builds."""
        result = build_crates_publish_jobs(
            "mytool", "smm-h/mytool", depends_on="build-and-upload",
        )
        assert "needs: [gate, build-and-upload]" in result

    def test_publish_gate_required(self):
        """Crates publish requires the gate job to pass."""
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "gate" in result


class TestCratesVersionBump:
    """Tests for version stamping in crates wrapper."""

    def test_ci_stamps_version(self):
        """The CI job stamps version in crates-wrapper/Cargo.toml."""
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        # The sed command replaces 0.0.0 with the version
        assert "0.0.0" in result
        assert "crates-wrapper/Cargo.toml" in result

    def test_version_extracted_from_tag(self):
        """Version is extracted from the git tag."""
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "GITHUB_REF_NAME" in result
        assert "VERSION" in result


class TestCratesPublishSetupHints:
    """Tests for crates.io publish setup hints."""

    def test_go_target_includes_crates_hint(self, tmp_path):
        """When crates_wrapper is enabled, publishSetup includes crates instructions."""
        from unittest.mock import MagicMock
        from rlsbl.targets.go import GoTarget

        (tmp_path / "go.mod").write_text("module github.com/smm-h/tool\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
        (tmp_path / "VERSION").write_text("0.1.0")

        target = GoTarget()
        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.config = {"crates_wrapper": {"enabled": True}}

        tvars = target.template_vars(str(tmp_path), ctx)
        # Should mention Trusted Publishing setup on crates.io
        assert "Trusted Publishing" in tvars["publishSetup"]
        assert "crates.io" in tvars["publishSetup"]

    def test_go_target_without_crates_no_hint(self, tmp_path):
        """When crates_wrapper is disabled, no crates.io hint."""
        from unittest.mock import MagicMock
        from rlsbl.targets.go import GoTarget

        (tmp_path / "go.mod").write_text("module github.com/smm-h/tool\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
        (tmp_path / "VERSION").write_text("0.1.0")

        target = GoTarget()
        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.config = {}

        tvars = target.template_vars(str(tmp_path), ctx)
        assert "crates.io" not in tvars.get("publishSetup", "")


class TestCratesPublishGateExtension:
    """Tests for publish gate coverage of crates.io check runs."""

    def test_cargo_in_ci_check_names(self):
        """CI_CHECK_JOB_NAMES includes an entry for cargo."""
        assert "cargo" in CI_CHECK_JOB_NAMES

    def test_cargo_check_regex_matches_test_job(self):
        """The check regex for cargo matches the 'test' job."""
        import re
        regex = ci_check_regex_for_targets(["cargo"])
        assert re.match(regex, "test")

    def test_cargo_check_regex_matches_matrix_variant(self):
        """The check regex matches matrix expansion like 'test (stable)'."""
        import re
        regex = ci_check_regex_for_targets(["cargo"])
        assert re.match(regex, "test (stable)")

    def test_combined_go_and_cargo_regex(self):
        """When both go and cargo targets exist, both are covered."""
        import re
        regex = ci_check_regex_for_targets(["go", "cargo"])
        assert re.match(regex, "test")
        assert re.match(regex, "test (ubuntu-latest)")


class TestCratesSkipCheck:
    """Tests for the 'already published' skip check in CI."""

    def test_skip_check_queries_crates_api(self):
        """The publish job checks crates.io API before publishing."""
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "crates.io/api/v1/crates" in result

    def test_skip_check_uses_user_agent(self):
        """The API call includes a User-Agent header (required by crates.io)."""
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "User-Agent" in result

    def test_skip_check_gates_publish(self):
        """When already published, the publish step is skipped."""
        result = build_crates_publish_jobs("mytool", "smm-h/mytool")
        assert "check-crate.outputs.skip" in result
