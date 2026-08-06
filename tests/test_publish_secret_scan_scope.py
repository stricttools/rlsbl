"""The publish-workflow secret scan is scoped to built artifacts, never the tree.

Every publish template used to run ``gitleaks dir .`` -- a scan of the whole
checkout. That flags files which never reach a registry (fixtures, docs, sample
configs, public identifiers committed on purpose) and it blocked a real release
mid-flight, leaving a permanently scarred version with zero assets attached.

The pypi template was already correct and is the model: build first, then
``gitleaks dir dist/``. Every other ecosystem now follows the same shape:

* npm / yarn / pnpm -- pack the publishable tarball into a scratch directory
  outside the checkout, scan that directory, then publish from the tree.
* go -- scan goreleaser's ``dist/`` after the build, before the assets upload.
* maven -- assemble the jars, scan the build output, then publish.
* deno / docker / hex / zig -- these publish straight from source (or upload
  raw compiled binaries) with no packed archive to scan before the publish
  call, so the scan step and its now-dead gitleaks install are gone rather
  than left scanning the tree.

Assertions are render-level and ordering-sensitive: the scan must name an
artifact path, must come after the step that produces the artifact, and must
come before the publish step. The monorepo publish router inlines each
project's publish job verbatim, so the inlined job inherits the change --
proven here rather than assumed.
"""

import os
import re
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from rlsbl.commands.init_cmd import process_template
from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router
from rlsbl.publish_gate import gate_job_template_snippet

TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)

# Shared render vars: enough to turn any publish template into valid YAML.
_RENDER_VARS = {
    "publishGate": gate_job_template_snippet("CI"),
    "registryUrl": "https://registry.npmjs.org",
    "npmPublishJobs": "",
    "homebrewEnv": "",
    "importName": "demo",
    "modulePath": "github.com/acme/demo",
    "npm.provenance": "",
    "pypi.hasPytest": "1",
    "pypi.minRequiredPython": "",
    "zig.minRequiredZig": "0.14.0",
    "zig.projectName": "demo",
}


def read_template(rel_path):
    with open(os.path.join(TEMPLATES_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def render_template(rel_path):
    content, _unreplaced = process_template(read_template(rel_path), _RENDER_VARS)
    return content


def executable_lines(text):
    """Template text with comment-only lines removed.

    The templates explain in comments why a scan was dropped or rescoped, and
    those comments name the old `gitleaks dir .` command. Assertions are about
    steps that RUN, so comments are stripped before matching.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def all_template_paths():
    paths = []
    for dirpath, _dirnames, filenames in os.walk(TEMPLATES_ROOT):
        for name in filenames:
            full = os.path.join(dirpath, name)
            paths.append(os.path.relpath(full, TEMPLATES_ROOT))
    return sorted(paths)


# rel path -> (artifact-producing step marker, scanned path, publish marker)
ARTIFACT_SCAN_TEMPLATES = {
    "pypi/publish.yml.tpl": ("uv build --out-dir dist", "dist/", "pypa/gh-action-pypi-publish"),
    "npm/publish.yml.tpl": (
        "npm pack --pack-destination",
        '"$RUNNER_TEMP/artifacts"',
        "- run: npm publish",
    ),
    "npm/publish-yarn.yml.tpl": (
        "yarn pack --out",
        '"$RUNNER_TEMP/artifacts"',
        "- run: yarn npm publish",
    ),
    "npm/publish-pnpm.yml.tpl": (
        "pnpm pack --pack-destination",
        '"$RUNNER_TEMP/artifacts"',
        "- run: pnpm publish",
    ),
    "go/publish.yml.tpl": (
        "goreleaser/goreleaser-action",
        "dist/",
        "gh release upload",
    ),
    "maven/publish.yml.tpl": (
        "./gradlew assemble",
        "build/libs",
        "- run: ./gradlew publish",
    ),
    "maven/publish-central.yml.tpl": (
        "./gradlew assemble",
        "build/libs",
        "- run: ./gradlew publishAndReleaseToMavenCentral",
    ),
}

# Publish templates that must carry no secret scan at all: nothing is built
# before the publish call, so the only thing left to scan would be the tree.
NO_SCAN_TEMPLATES = [
    "deno/publish.yml.tpl",
    "docker/publish.yml.tpl",
    "hex/publish.yml.tpl",
    "zig/publish.yml.tpl",
    "go/publish-library.yml.tpl",
    "npm/publish-launcher.yml.tpl",
    "pypi/publish-launcher.yml.tpl",
    # CI installs gitleaks and never scans anything -- pure dead weight.
    "pypi/ci.yml.tpl",
]


class TestNoWholeTreeScans:
    def test_no_template_scans_the_working_tree(self):
        offenders = [
            path for path in all_template_paths()
            if "gitleaks dir ." in executable_lines(read_template(path))
        ]
        assert offenders == []

    def test_no_step_is_named_scan_source(self):
        offenders = [
            path for path in all_template_paths()
            if "Scan source for secrets" in read_template(path)
        ]
        assert offenders == []


class TestArtifactScopedScans:
    @pytest.mark.parametrize("rel_path", sorted(ARTIFACT_SCAN_TEMPLATES))
    def test_scan_step_is_named_for_artifacts(self, rel_path):
        content = render_template(rel_path)
        assert "name: Scan artifacts for secrets" in content

    @pytest.mark.parametrize("rel_path", sorted(ARTIFACT_SCAN_TEMPLATES))
    def test_scan_targets_the_artifact_path(self, rel_path):
        _producer, artifact_path, _publish = ARTIFACT_SCAN_TEMPLATES[rel_path]
        content = render_template(rel_path)
        scan_lines = [
            line for line in content.splitlines()
            if "gitleaks dir" in line
        ]
        assert len(scan_lines) == 1, scan_lines
        assert artifact_path in scan_lines[0]

    @pytest.mark.parametrize("rel_path", sorted(ARTIFACT_SCAN_TEMPLATES))
    def test_scan_follows_the_build_and_precedes_the_publish(self, rel_path):
        producer, _artifact_path, publish = ARTIFACT_SCAN_TEMPLATES[rel_path]
        content = render_template(rel_path)
        producer_idx = content.index(producer)
        scan_idx = content.index("gitleaks dir")
        publish_idx = content.index(publish)
        assert producer_idx < scan_idx < publish_idx

    @pytest.mark.parametrize("rel_path", sorted(ARTIFACT_SCAN_TEMPLATES))
    def test_gitleaks_is_installed_before_the_scan(self, rel_path):
        content = render_template(rel_path)
        assert content.index("Install gitleaks") < content.index("gitleaks dir")

    @pytest.mark.parametrize("rel_path", sorted(ARTIFACT_SCAN_TEMPLATES))
    def test_project_gitleaks_config_is_honored(self, rel_path):
        """gitleaks only auto-loads .gitleaks.toml from the directory it scans.

        Artifact directories never hold the project's allowlist, so the config
        has to be passed explicitly -- otherwise the documented remedy for a
        false positive silently stops working in CI.
        """
        content = render_template(rel_path)
        assert "--config" in content
        assert ".gitleaks.toml" in content

    @pytest.mark.parametrize("rel_path", sorted(ARTIFACT_SCAN_TEMPLATES))
    def test_renders_as_yaml_with_the_scan_step(self, rel_path):
        content = render_template(rel_path)
        parsed = YAML(typ="safe").load(content)
        scan_steps = [
            step
            for job in parsed["jobs"].values()
            for step in job.get("steps", [])
            if "gitleaks dir" in (step.get("run") or "")
        ]
        assert len(scan_steps) == 1
        assert scan_steps[0].get("name") == "Scan artifacts for secrets"


class TestScanDroppedWhereNoArtifactExists:
    @pytest.mark.parametrize("rel_path", NO_SCAN_TEMPLATES)
    def test_no_gitleaks_reference_at_all(self, rel_path):
        """Dropping the scan must drop its install step too (no dead weight)."""
        assert "gitleaks" not in executable_lines(read_template(rel_path)).lower()

    @pytest.mark.parametrize("rel_path", NO_SCAN_TEMPLATES)
    def test_template_still_renders_as_valid_yaml(self, rel_path):
        """Removing steps must not leave a dangling list item or bad indent."""
        parsed = YAML(typ="safe").load(render_template(rel_path))
        assert parsed["jobs"]
        for job in parsed["jobs"].values():
            for step in job.get("steps", []):
                assert "gitleaks" not in (step.get("run") or "")


class TestNpmPackScopedToScratchDir:
    """The pack destination must sit outside the checkout.

    A scratch directory inside the repo would be swept into the published
    tarball by `npm publish` for any package without a `files` field.
    """

    @pytest.mark.parametrize("rel_path", [
        "npm/publish.yml.tpl",
        "npm/publish-yarn.yml.tpl",
        "npm/publish-pnpm.yml.tpl",
    ])
    def test_pack_destination_is_runner_temp(self, rel_path):
        content = read_template(rel_path)
        pack_line = next(
            line for line in content.splitlines() if " pack " in line
        )
        assert "$RUNNER_TEMP" in pack_line

    @pytest.mark.parametrize("rel_path", [
        "npm/publish.yml.tpl",
        "npm/publish-yarn.yml.tpl",
        "npm/publish-pnpm.yml.tpl",
    ])
    def test_pack_runs_after_the_dependency_install(self, rel_path):
        """`* pack` runs prepack, which needs the dev toolchain installed."""
        content = read_template(rel_path)
        install_idx = content.index("Install dependencies")
        pack_idx = content.index("gitleaks dir")
        assert install_idx < pack_idx


class TestRouterInheritsArtifactScopedScan:
    """`rlsbl monorepo sync` inlines the job, so the fix propagates on re-sync."""

    def test_inlined_npm_job_scans_artifacts_between_pack_and_publish(self, tmp_path):
        root = str(tmp_path)
        wf_dir = os.path.join(root, "packages", "mylib", ".github", "workflows")
        os.makedirs(wf_dir)
        with open(os.path.join(wf_dir, "publish.yml"), "w", encoding="utf-8") as f:
            f.write(render_template("npm/publish.yml.tpl"))

        projects = [{"name": "mylib", "path": "packages/mylib"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="mylib@v",
        ):
            router = generate_inline_publish_router(projects, root)

        parsed = YAML(typ="safe").load(router)
        steps = parsed["jobs"]["mylib-publish"]["steps"]
        runs = [step.get("run") or "" for step in steps]

        pack_idx = next(i for i, r in enumerate(runs) if "npm pack" in r)
        scan_idx = next(i for i, r in enumerate(runs) if "gitleaks dir" in r)
        publish_idx = next(i for i, r in enumerate(runs) if "npm publish" in r)

        assert pack_idx < scan_idx < publish_idx
        assert "$RUNNER_TEMP/artifacts" in runs[scan_idx]
        assert not re.search(r"gitleaks dir \.\s*$", runs[scan_idx].strip())
