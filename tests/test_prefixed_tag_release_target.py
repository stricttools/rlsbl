"""Prefixed monorepo release tags must be the release target everywhere.

rlsbl tags a monorepo releasable `<name>@vX.Y.Z` and creates the GitHub Release
on that tag. Two generated artifacts assumed the bare `vX.Y.Z` scheme:

- the Go publish job delegated to goreleaser's own publisher, whose tag
  validation rejects the prefix and whose release lookup resolves the stripped
  bare name -- so assets landed on a wrong (or newly created) `vX.Y.Z` Release
  while the prefixed one that launcher shims read stayed empty;
- the launcher shims reconstructed asset URLs as `.../download/v${version}`,
  a 404 in every prefixed-tag repo.

Both were observed on a real first release. rlsbl now BUILDS with goreleaser
and uploads to the prefixed tag itself, and the shims carry a tag-prefix
template var.
"""

import os
import re
import subprocess

import pytest

from rlsbl.commands.init_cmd import process_template, resolve_tag_prefix


def _templates_root():
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates",
    )


def _read(*parts):
    with open(os.path.join(_templates_root(), *parts), "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Go publish template: build locally, upload to the PREFIXED tag
# ---------------------------------------------------------------------------


GO_PUBLISH = "go/publish.yml.tpl"


class TestGoreleaserBuildsAndRlsblUploads:
    def test_goreleaser_does_not_publish(self):
        content = _read(GO_PUBLISH)
        args = next(
            line for line in content.splitlines() if "args: release" in line
        )
        assert "--skip=publish,announce,validate" in args

    def test_bare_tag_is_handed_to_goreleaser(self):
        content = _read(GO_PUBLISH)
        assert "GORELEASER_CURRENT_TAG: ${{ steps.bare-tag.outputs.tag }}" in content

    def test_upload_targets_the_unstripped_release_tag(self):
        """`gh release upload` must use the ref tag, prefix intact."""
        content = _read(GO_PUBLISH)
        assert 'gh release upload "${RELEASE_TAG}"' in content
        upload_block = content[content.index("Upload release assets"):]
        assert "RELEASE_TAG: ${{ inputs.tag || github.ref_name }}" in upload_block
        # The bare tag is goreleaser's input ONLY -- never the upload target.
        assert "gh release upload \"${BARE" not in content
        assert "steps.bare-tag.outputs.tag }}\"" not in upload_block.split("env:")[0]

    def test_checksums_are_uploaded(self):
        """The shims verify downloads against checksums.txt."""
        assert "dist/checksums.txt" in _read(GO_PUBLISH)

    def test_upload_runs_after_the_build(self):
        content = _read(GO_PUBLISH)
        assert content.index("goreleaser/goreleaser-action") < content.index(
            "Upload release assets"
        )

    def test_missing_assets_is_a_hard_error(self):
        """A silent no-op upload would leave the Release empty."""
        content = _read(GO_PUBLISH)
        assert "goreleaser produced no assets" in content


class TestBareTagDerivation:
    """The shell derivation, exercised as shell."""

    SED = r"sed -E 's/^.*(v[0-9]+\.[0-9]+\.[0-9]+.*)$/\1/'"

    @pytest.mark.parametrize("tag,expected", [
        ("alpha@v1.2.3", "v1.2.3"),
        ("v1.2.3", "v1.2.3"),
        ("cmd/tool/v0.4.0", "v0.4.0"),
        ("alpha@v1.0.0-rc.1", "v1.0.0-rc.1"),
        ("veliu-dev@v2.0.0", "v2.0.0"),
    ])
    def test_strips_only_the_prefix(self, tag, expected):
        out = subprocess.run(
            f"printf '%s' '{tag}' | {self.SED}",
            shell=True, capture_output=True, text=True, check=True,
        )
        assert out.stdout == expected

    def test_the_template_uses_this_exact_expression(self):
        content = _read(GO_PUBLISH)
        assert r"sed -E 's/^.*(v[0-9]+\.[0-9]+\.[0-9]+.*)$/\1/'" in content


# ---------------------------------------------------------------------------
# Launcher shims: the tag prefix is a template var, not a hardcoded "v"
# ---------------------------------------------------------------------------


SHIMS = [
    ("npm", "shim-postinstall.cjs.tpl"),
    ("npm", "shim-firstrun.cjs.tpl"),
    ("pypi", "shim-launcher.py.tpl"),
]


class TestLauncherShimsArePrefixAware:
    @pytest.mark.parametrize("target,name", SHIMS)
    def test_no_hardcoded_v_in_the_release_url(self, target, name):
        content = _read(target, name)
        assert "releases/download/v" not in content

    @pytest.mark.parametrize("target,name", SHIMS)
    def test_declares_the_tag_prefix_var(self, target, name):
        assert "{{tagPrefix}}" in _read(target, name)

    @pytest.mark.parametrize("target,name", SHIMS)
    def test_renders_a_prefixed_url(self, target, name):
        rendered, _unreplaced = process_template(
            _read(target, name),
            {
                "githubRepo": "smm-h/mono",
                "assetProject": "alpha",
                "binaryName": "alpha",
                "tagPrefix": "alpha@v",
                "distName": "alpha",
            },
        )
        assert "releases/download/${TAG_PREFIX}${version}" in rendered or (
            "releases/download/{TAG_PREFIX}{version}" in rendered
        )
        assert re.search(r'TAG_PREFIX\s*=\s*"alpha@v"', rendered)

    @pytest.mark.parametrize("target,name", SHIMS)
    def test_standalone_renders_the_plain_v(self, target, name):
        rendered, _unreplaced = process_template(
            _read(target, name),
            {
                "githubRepo": "smm-h/tool",
                "assetProject": "tool",
                "binaryName": "tool",
                "tagPrefix": "v",
                "distName": "tool",
            },
        )
        assert re.search(r'TAG_PREFIX\s*=\s*"v"', rendered)


class TestResolveTagPrefix:
    def test_standalone_is_plain_v(self, tmp_path):
        assert resolve_tag_prefix(str(tmp_path)) == "v"

    def test_none_is_plain_v(self):
        assert resolve_tag_prefix(None) == "v"

    def test_releasable_member_gets_the_releasable_prefix(self, mock_git_repo):
        (mock_git_repo / ".rlsbl-monorepo").mkdir(exist_ok=True)
        (mock_git_repo / ".rlsbl-monorepo" / "workspace.toml").write_text(
            '[[projects]]\n'
            'name = "core"\n'
            'path = "core"\n'
            'releasable = "alpha"\n'
            '\n'
            '[[releasables]]\n'
            'name = "alpha"\n'
            'tag_format = "{name}@v{version}"\n'
        )
        pkg = mock_git_repo / "core"
        pkg.mkdir(exist_ok=True)
        assert resolve_tag_prefix(str(pkg)) == "alpha@v"


# ---------------------------------------------------------------------------
# Launcher PUBLISH templates: the asset-verification probe is prefix-aware
# ---------------------------------------------------------------------------


LAUNCHER_PUBLISH = [
    ("npm", "publish-launcher.yml.tpl"),
    ("pypi", "publish-launcher.yml.tpl"),
]

_PUBLISH_RENDER_VARS = {
    "githubRepo": "smm-h/mono",
    "assetProject": "alpha",
    "binaryName": "alpha",
    "tagPrefix": "alpha@v",
    "distName": "alpha",
    "registryUrl": "https://registry.npmjs.org",
    "publishGate": "",
    "npm.provenance": "",
}


class TestLauncherPublishProbeIsPrefixAware:
    """The publish-time 404 probe must build the SAME URL the shim downloads.

    It used to build ``${OWNER_REPO##*/}_${TAG#v}_...``: the repo basename
    (not the producer's asset project name) and a bare-``v`` strip that leaves
    a prefixed tag like ``alpha@v1.2.3`` completely intact.
    """

    @pytest.mark.parametrize("target,name", LAUNCHER_PUBLISH)
    def test_no_bare_v_strip(self, target, name):
        assert "${TAG#v}" not in _read(target, name)

    @pytest.mark.parametrize("target,name", LAUNCHER_PUBLISH)
    def test_no_repo_basename_as_asset_project(self, target, name):
        assert "${OWNER_REPO##*/}" not in _read(target, name)

    @pytest.mark.parametrize("target,name", LAUNCHER_PUBLISH)
    def test_declares_the_shim_vars(self, target, name):
        content = _read(target, name)
        assert "{{assetProject}}" in content
        assert "{{tagPrefix}}" in content

    @pytest.mark.parametrize("target,name", LAUNCHER_PUBLISH)
    def test_renders_the_asset_project_and_prefix(self, target, name):
        rendered, _unreplaced = process_template(
            _read(target, name), dict(_PUBLISH_RENDER_VARS),
        )
        assert 'ASSET_PROJECT="alpha"' in rendered
        assert 'TAG_PREFIX="alpha@v"' in rendered
        assert "${ASSET_PROJECT}_${VERSION}_linux_amd64.tar.gz" in rendered
        # GitHub's own ${{ }} expressions survive; rlsbl's do not.
        assert "{{assetProject}}" not in rendered
        assert "{{tagPrefix}}" not in rendered


class TestLauncherPublishVersionDerivation:
    """The version-stripping expression, exercised as real shell."""

    EXPR = 'TAG="%s"; TAG_PREFIX="%s"; printf "%%s" "${TAG#"${TAG_PREFIX}"}"'

    @pytest.mark.parametrize("tag,prefix,expected", [
        ("alpha@v1.2.3", "alpha@v", "1.2.3"),
        ("v1.2.3", "v", "1.2.3"),
        ("cmd/tool/v0.4.0", "cmd/tool/v", "0.4.0"),
        ("alpha@v1.0.0-rc.1", "alpha@v", "1.0.0-rc.1"),
    ])
    def test_strips_exactly_the_prefix(self, tag, prefix, expected):
        out = subprocess.run(
            ["bash", "-c", self.EXPR % (tag, prefix)],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout == expected

    @pytest.mark.parametrize("target,name", LAUNCHER_PUBLISH)
    def test_the_template_uses_this_exact_expression(self, target, name):
        assert '${TAG#"${TAG_PREFIX}"}' in _read(target, name)


class TestMergedPublishFeedsTheLauncherVars:
    """The scaffold's publish render path resolves the same vars the shims get.

    The bug was a plumbing gap, not a template typo: the shim render path
    resolved ``assetProject``/``tagPrefix`` from the wrapped producer, the
    publish render path did not, so the probe fell back to guesswork.
    """

    def _fixture(self, root):
        import json

        (root / "go.mod").write_text("module github.com/acme/alpha\n\ngo 1.23\n")
        (root / "main.go").write_text("package main\n\nfunc main() {}\n")
        (root / "VERSION").write_text("0.1.0\n")
        (root / "package.json").write_text(
            json.dumps({"name": "alpha", "version": "0.1.0"}, indent=2) + "\n"
        )
        (root / ".rlsbl-monorepo").mkdir(exist_ok=True)
        (root / ".rlsbl-monorepo" / "workspace.toml").write_text(
            '[[projects]]\n'
            'name = "alpha"\n'
            'path = "."\n'
            'releasable = "alpha"\n'
            '\n'
            '[[releasables]]\n'
            'name = "alpha"\n'
            'tag_format = "{name}@v{version}"\n'
        )
        rlsbl_dir = root / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps({
            "publish_mode": "ci",
            "targets": [{"name": "go", "path": "."},
                        {"name": "npm", "path": "."}],
            "pipelines": {
                "go": {"type": "go", "local": False, "target": "go",
                       "artifact": "binary"},
                "npm": {"type": "npm", "local": False, "target": "npm",
                        "artifact": "launcher", "wraps": "go",
                        "binary_source": "github-release", "provenance": True,
                        "download": "first-run"},
            },
        }, indent=2) + "\n")

    def test_prefixed_releasable_probe_uses_the_real_asset_url(
            self, mock_git_repo, monkeypatch):
        from pathlib import Path

        from rlsbl.commands.init_cmd import _generate_merged_publish
        from rlsbl.config import read_project_config
        from rlsbl.context import ProjectContext
        from rlsbl.pipelines import load_pipelines

        root = mock_git_repo
        self._fixture(root)
        monkeypatch.chdir(root)

        ctx = ProjectContext(project_root=Path("."), workspace_root=None,
                             config=read_project_config("."))
        pipelines = load_pipelines(ctx.config)
        content = _generate_merged_publish(
            ["go", "npm"],
            {"name": "alpha", "registryUrl": "https://registry.npmjs.org",
             "publishGate": "", "npm.provenance": "",
             "modulePath": "github.com/acme/alpha"},
            {"go": ".", "npm": "."},
            pipelines=pipelines,
            ctx=ctx,
        )
        assert 'ASSET_PROJECT="alpha"' in content
        assert 'TAG_PREFIX="alpha@v"' in content
        # No placeholder survived into the generated workflow.
        assert "__UNRESOLVED__assetProject__" not in content
        assert "__UNRESOLVED__tagPrefix__" not in content
