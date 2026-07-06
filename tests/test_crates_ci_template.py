"""Tests for the crates.io CI publish workflow template and OIDC auth."""

import os

from rlsbl.targets.cargo import CargoTarget


class TestCargoPublishTemplate:
    """Tests for the cargo publish.yml.tpl template."""

    def test_template_contains_oidc_auth(self):
        """The publish template uses OIDC trusted publishing."""
        target = CargoTarget()
        template_dir = target.template_dir()
        template_path = os.path.join(template_dir, "publish.yml.tpl")
        with open(template_path) as f:
            content = f.read()
        assert "crates-io-auth-action" in content

    def test_template_has_id_token_permission(self):
        """The publish template requests id-token: write for OIDC."""
        target = CargoTarget()
        template_dir = target.template_dir()
        template_path = os.path.join(template_dir, "publish.yml.tpl")
        with open(template_path) as f:
            content = f.read()
        assert "id-token: write" in content

    def test_template_has_publish_gate(self):
        """The publish template includes the publish gate placeholder."""
        target = CargoTarget()
        template_dir = target.template_dir()
        template_path = os.path.join(template_dir, "publish.yml.tpl")
        with open(template_path) as f:
            content = f.read()
        assert "{{publishGate}}" in content

    def test_template_has_skip_check(self):
        """The publish template checks if already published before publishing."""
        target = CargoTarget()
        template_dir = target.template_dir()
        template_path = os.path.join(template_dir, "publish.yml.tpl")
        with open(template_path) as f:
            content = f.read()
        assert "Already published" in content

    def test_template_no_stored_token_secret(self):
        """The publish template does NOT use secrets.CARGO_REGISTRY_TOKEN directly.

        The token comes from the crates-io-auth-action output instead.
        """
        target = CargoTarget()
        template_dir = target.template_dir()
        template_path = os.path.join(template_dir, "publish.yml.tpl")
        with open(template_path) as f:
            content = f.read()
        # Should NOT have the old secrets.CARGO_REGISTRY_TOKEN reference
        assert "secrets.CARGO_REGISTRY_TOKEN" not in content
        # Should use the action output instead
        assert "steps.crates-auth.outputs.token" in content

    def test_template_has_gitleaks(self):
        """The publish template scans for secrets before publishing."""
        target = CargoTarget()
        template_dir = target.template_dir()
        template_path = os.path.join(template_dir, "publish.yml.tpl")
        with open(template_path) as f:
            content = f.read()
        assert "gitleaks" in content

    def test_template_has_user_agent(self):
        """The skip-check curl call includes a User-Agent header."""
        target = CargoTarget()
        template_dir = target.template_dir()
        template_path = os.path.join(template_dir, "publish.yml.tpl")
        with open(template_path) as f:
            content = f.read()
        assert "User-Agent" in content


class TestCargoPublishSetup:
    """Tests for the publishSetup hint in CargoTarget."""

    def test_publish_setup_mentions_trusted_publishing(self, tmp_path):
        """publishSetup should mention Trusted Publishing, not a stored secret."""
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "test-crate"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        target = CargoTarget()
        tvars = target.template_vars(str(tmp_path), None)
        assert "Trusted Publishing" in tvars["publishSetup"]
        assert "CARGO_REGISTRY_TOKEN" not in tvars["publishSetup"]


class TestCratesPublishGate:
    """Tests for publish gate integration with crates.io."""

    def test_cargo_ci_check_job_names(self):
        """The publish gate recognizes cargo's CI job name."""
        from rlsbl.publish_gate import CI_CHECK_JOB_NAMES
        assert "cargo" in CI_CHECK_JOB_NAMES
        assert "test" in CI_CHECK_JOB_NAMES["cargo"]

    def test_ci_check_regex_includes_cargo(self):
        """The check regex for cargo targets matches 'test'."""
        from rlsbl.publish_gate import ci_check_regex_for_targets
        import re
        regex = ci_check_regex_for_targets(["cargo"])
        assert re.match(regex, "test")
        assert re.match(regex, "test (ubuntu)")
