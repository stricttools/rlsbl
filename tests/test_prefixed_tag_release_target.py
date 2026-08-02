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
