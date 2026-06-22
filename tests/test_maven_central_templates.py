"""Tests for Maven Central publish template and updated Maven CI template.

Verifies:
1. publish-central.yml.tpl renders with correct action versions
2. publish-central.yml.tpl has correct env var secrets mapping
3. publish-central.yml.tpl has correct permissions
4. ci.yml.tpl updated to modern actions (Java 25, setup-java v5, setup-gradle v6)
"""

import os

from rlsbl.action_versions import format_action, get_action_version
from rlsbl.commands.init_cmd import process_template
from rlsbl.pipelines.maven import MavenCentralPipeline

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates", "maven"
)


def _read_template(name):
    path = os.path.join(TEMPLATE_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _render_template(name):
    """Read and render a template through process_template."""
    raw = _read_template(name)
    content, unreplaced = process_template(raw, {})
    return content


class TestPublishCentralTemplateExists:
    def test_publish_central_tpl_exists(self):
        assert os.path.isfile(os.path.join(TEMPLATE_DIR, "publish-central.yml.tpl"))


class TestPublishCentralActionVersions:
    """publish-central.yml.tpl renders with correct pinned action versions."""

    def test_renders_checkout_action(self):
        content = _render_template("publish-central.yml.tpl")
        assert format_action("actions/checkout") in content

    def test_renders_setup_java_v5(self):
        content = _render_template("publish-central.yml.tpl")
        assert "actions/setup-java@v5" in content

    def test_renders_setup_gradle_v6(self):
        content = _render_template("publish-central.yml.tpl")
        assert "gradle/actions/setup-gradle@v6" in content

    def test_java_version_25(self):
        content = _render_template("publish-central.yml.tpl")
        assert 'java-version: "25"' in content

    def test_temurin_distribution(self):
        content = _render_template("publish-central.yml.tpl")
        assert "distribution: temurin" in content

    def test_no_unresolved_action_placeholders(self):
        content = _render_template("publish-central.yml.tpl")
        assert "{{action" not in content


class TestPublishCentralSecrets:
    """publish-central.yml.tpl has correct env var secrets mapping."""

    def test_sonatype_username_env(self):
        content = _read_template("publish-central.yml.tpl")
        assert "ORG_GRADLE_PROJECT_mavenCentralUsername: ${{ secrets.SONATYPE_USERNAME }}" in content

    def test_sonatype_password_env(self):
        content = _read_template("publish-central.yml.tpl")
        assert "ORG_GRADLE_PROJECT_mavenCentralPassword: ${{ secrets.SONATYPE_PASSWORD }}" in content

    def test_gpg_signing_key_env(self):
        content = _read_template("publish-central.yml.tpl")
        assert "ORG_GRADLE_PROJECT_signingInMemoryKey: ${{ secrets.GPG_SIGNING_KEY }}" in content

    def test_gpg_signing_key_password_env(self):
        content = _read_template("publish-central.yml.tpl")
        assert "ORG_GRADLE_PROJECT_signingInMemoryKeyPassword: ${{ secrets.GPG_SIGNING_KEY_PASSWORD }}" in content

    def test_exactly_four_org_gradle_env_vars(self):
        content = _read_template("publish-central.yml.tpl")
        count = content.count("ORG_GRADLE_PROJECT_")
        assert count == 4, f"Expected 4 ORG_GRADLE_PROJECT_ env vars, got {count}"

    def test_gradle_command(self):
        content = _read_template("publish-central.yml.tpl")
        assert "publishAndReleaseToMavenCentral" in content


class TestPublishCentralPermissions:
    """publish-central.yml.tpl has correct permissions."""

    def test_contents_read_permission(self):
        content = _read_template("publish-central.yml.tpl")
        assert "contents: read" in content

    def test_no_packages_write_permission(self):
        content = _read_template("publish-central.yml.tpl")
        assert "packages: write" not in content

    def test_no_id_token_permission(self):
        """Maven Central uses secrets, not OIDC -- no id-token needed."""
        content = _read_template("publish-central.yml.tpl")
        assert "id-token" not in content


class TestPublishCentralTriggers:
    def test_triggers_on_release_published(self):
        content = _read_template("publish-central.yml.tpl")
        assert "release:" in content
        assert "published" in content

    def test_triggers_on_workflow_dispatch(self):
        content = _read_template("publish-central.yml.tpl")
        assert "workflow_dispatch" in content


class TestCiTemplateModernActions:
    """ci.yml.tpl updated to modern actions and Java 25."""

    def test_java_version_25(self):
        content = _read_template("ci.yml.tpl")
        assert 'java-version: "25"' in content

    def test_renders_setup_java_v5(self):
        content = _render_template("ci.yml.tpl")
        assert "actions/setup-java@v5" in content

    def test_renders_setup_gradle_v6(self):
        content = _render_template("ci.yml.tpl")
        assert "gradle/actions/setup-gradle@v6" in content

    def test_temurin_distribution(self):
        content = _read_template("ci.yml.tpl")
        assert "distribution: temurin" in content

    def test_no_unresolved_action_placeholders(self):
        content = _render_template("ci.yml.tpl")
        assert "{{action" not in content


class TestMavenCentralPipelineMapping:
    """MavenCentralPipeline.template_mappings() points to the central template."""

    def test_maps_to_publish_central_template(self):
        pipeline = MavenCentralPipeline(
            name="maven-central",
            pipeline_type="maven-central",
            local=False,
            config={"type": "maven-central", "local": False},
        )
        mappings = pipeline.template_mappings(ctx=None)
        templates = [m["template"] for m in mappings]
        assert "publish-central.yml.tpl" in templates

    def test_target_is_publish_workflow(self):
        pipeline = MavenCentralPipeline(
            name="maven-central",
            pipeline_type="maven-central",
            local=False,
            config={"type": "maven-central", "local": False},
        )
        mappings = pipeline.template_mappings(ctx=None)
        targets = [m["target"] for m in mappings]
        assert ".github/workflows/publish.yml" in targets

    def test_template_dir_is_maven(self):
        pipeline = MavenCentralPipeline(
            name="maven-central",
            pipeline_type="maven-central",
            local=False,
            config={"type": "maven-central", "local": False},
        )
        tdir = pipeline.template_dir()
        assert tdir is not None
        assert tdir.endswith(os.path.join("templates", "maven"))


class TestActionVersionsUpdated:
    """Centralized action versions table has modern versions."""

    def test_setup_java_is_v5(self):
        assert get_action_version("actions/setup-java") == "v5"

    def test_setup_gradle_is_v6(self):
        assert get_action_version("gradle/actions/setup-gradle") == "v6"
