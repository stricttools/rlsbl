"""Tests for pre-release awareness in CI publish templates.

Verifies that npm/pnpm/yarn templates include the dist-tag detection step,
and that the Docker template conditionally skips :latest for pre-releases.
Also verifies the shell-level grep/sed logic for extracting pre-release
identifiers from version strings.
"""

import os
import subprocess

import pytest

from rlsbl.targets.docker import DockerTarget
from rlsbl.targets.npm import NpmTarget

NPM_TEMPLATE_DIR = NpmTarget().template_dir()
DOCKER_TEMPLATE_DIR = DockerTarget().template_dir()


def _read_template(directory, name):
    path = os.path.join(directory, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# npm publish template -- dist-tag step
# ---------------------------------------------------------------------------


class TestNpmPublishDistTag:
    """npm publish.yml.tpl includes pre-release dist-tag detection."""

    def test_has_dist_tag_step(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish.yml.tpl")
        assert "Determine dist-tag" in content

    def test_dist_tag_step_has_id(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish.yml.tpl")
        assert "id: dist-tag" in content

    def test_dist_tag_output_used_in_publish(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish.yml.tpl")
        assert "steps.dist-tag.outputs.tag" in content

    def test_publish_command_includes_tag(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish.yml.tpl")
        assert "npm publish --provenance --access public ${{ steps.dist-tag.outputs.tag }}" in content

    def test_dist_tag_step_before_publish(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish.yml.tpl")
        dist_tag_pos = content.index("Determine dist-tag")
        publish_pos = content.index("npm publish")
        assert dist_tag_pos < publish_pos


# ---------------------------------------------------------------------------
# pnpm publish template -- dist-tag step
# ---------------------------------------------------------------------------


class TestPnpmPublishDistTag:
    """pnpm publish-pnpm.yml.tpl includes pre-release dist-tag detection."""

    def test_has_dist_tag_step(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-pnpm.yml.tpl")
        assert "Determine dist-tag" in content

    def test_dist_tag_step_has_id(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-pnpm.yml.tpl")
        assert "id: dist-tag" in content

    def test_dist_tag_output_used_in_publish(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-pnpm.yml.tpl")
        assert "steps.dist-tag.outputs.tag" in content

    def test_publish_command_includes_tag(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-pnpm.yml.tpl")
        assert "pnpm publish --provenance --access public ${{ steps.dist-tag.outputs.tag }}" in content

    def test_dist_tag_step_before_publish(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-pnpm.yml.tpl")
        dist_tag_pos = content.index("Determine dist-tag")
        publish_pos = content.index("pnpm publish")
        assert dist_tag_pos < publish_pos


# ---------------------------------------------------------------------------
# yarn publish template -- dist-tag step
# ---------------------------------------------------------------------------


class TestYarnPublishDistTag:
    """yarn publish-yarn.yml.tpl includes pre-release dist-tag detection."""

    def test_has_dist_tag_step(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-yarn.yml.tpl")
        assert "Determine dist-tag" in content

    def test_dist_tag_step_has_id(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-yarn.yml.tpl")
        assert "id: dist-tag" in content

    def test_dist_tag_output_used_in_publish(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-yarn.yml.tpl")
        assert "steps.dist-tag.outputs.tag" in content

    def test_publish_command_includes_tag(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-yarn.yml.tpl")
        assert "yarn npm publish --access public ${{ steps.dist-tag.outputs.tag }}" in content

    def test_dist_tag_step_before_publish(self):
        content = _read_template(NPM_TEMPLATE_DIR, "publish-yarn.yml.tpl")
        dist_tag_pos = content.index("Determine dist-tag")
        publish_pos = content.index("yarn npm publish")
        assert dist_tag_pos < publish_pos


# ---------------------------------------------------------------------------
# Docker publish template -- conditional :latest
# ---------------------------------------------------------------------------


class TestDockerPublishConditionalLatest:
    """Docker publish.yml.tpl skips :latest for pre-release tags."""

    def test_latest_tag_has_enable_condition(self):
        content = _read_template(DOCKER_TEMPLATE_DIR, "publish.yml.tpl")
        assert "type=raw,value=latest,enable=" in content

    def test_enable_uses_contains_check(self):
        content = _read_template(DOCKER_TEMPLATE_DIR, "publish.yml.tpl")
        assert "!contains(github.event.release.tag_name, '-')" in content

    def test_semver_tag_still_present(self):
        content = _read_template(DOCKER_TEMPLATE_DIR, "publish.yml.tpl")
        # The escaped template var becomes literal {{version}} in output
        assert r"type=semver,pattern=\{{version}}" in content

    def test_no_unconditional_latest(self):
        """There must not be a bare 'type=raw,value=latest' without enable."""
        content = _read_template(DOCKER_TEMPLATE_DIR, "publish.yml.tpl")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("type=raw,value=latest"):
                assert "enable=" in stripped, (
                    f"Found unconditional :latest line: {stripped!r}"
                )


# ---------------------------------------------------------------------------
# Shell-level grep/sed logic for extracting pre-release identifiers
# ---------------------------------------------------------------------------


def _run_dist_tag_script(version):
    """Run the grep/sed logic from the template and return the tag output."""
    script = f"""
PKG_VERSION="{version}"
if echo "$PKG_VERSION" | grep -q '-'; then
    PREID=$(echo "$PKG_VERSION" | sed 's/[^-]*-//' | sed 's/\\.[^.]*$//')
    echo "tag=--tag $PREID"
else
    echo "tag="
fi
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    return result.stdout.strip()


class TestDistTagShellLogic:
    """Verify the grep/sed logic extracts the correct dist-tag."""

    def test_alpha_prerelease(self):
        output = _run_dist_tag_script("1.2.3-alpha.0")
        assert output == "tag=--tag alpha"

    def test_beta_prerelease(self):
        output = _run_dist_tag_script("1.2.3-beta.1")
        assert output == "tag=--tag beta"

    def test_rc_prerelease(self):
        output = _run_dist_tag_script("2.0.0-rc.5")
        assert output == "tag=--tag rc"

    def test_stable_no_tag(self):
        output = _run_dist_tag_script("1.2.3")
        assert output == "tag="

    def test_zero_stable_no_tag(self):
        output = _run_dist_tag_script("0.1.0")
        assert output == "tag="

    def test_alpha_zero_counter(self):
        output = _run_dist_tag_script("0.43.0-alpha.0")
        assert output == "tag=--tag alpha"

    def test_high_counter(self):
        output = _run_dist_tag_script("1.0.0-beta.15")
        assert output == "tag=--tag beta"
