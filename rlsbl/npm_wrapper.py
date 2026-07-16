"""Platform models and helpers for npm binary wrapper packages, providing job generation for publish workflows and scaffold template mappings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .action_versions import format_action


@dataclass
class PlatformSpec:
    """Partial platform descriptor (target-agnostic).

    Contains only the npm/OS/CPU identifiers -- no archive or binary
    details, since those depend on the build target (Go, Rust, etc.).
    """

    npm_platform: str
    os_constraint: str
    cpu_constraint: str


@dataclass
class PlatformArtifact:
    """Fully resolved platform artifact ready for package generation.

    Combines platform identifiers with target-specific archive and
    binary information.
    """

    npm_platform: str
    os_constraint: str
    cpu_constraint: str
    asset_pattern: str
    extract_cmd: str | None
    binary_name: str


DEFAULT_PLATFORMS: list[PlatformSpec] = [
    PlatformSpec("linux-x64", "linux", "x64"),
    PlatformSpec("linux-arm64", "linux", "arm64"),
    PlatformSpec("darwin-x64", "darwin", "x64"),
    PlatformSpec("darwin-arm64", "darwin", "arm64"),
    PlatformSpec("win32-x64", "win32", "x64"),
    PlatformSpec("win32-arm64", "win32", "arm64"),
]


def load_platform_config(config: dict) -> list[PlatformSpec]:
    """Return platform specs, optionally filtered by config.

    If ``config`` contains ``npm_wrapper.platforms`` (a list of
    npm_platform strings like ``["linux-x64", "darwin-arm64"]``),
    only matching entries from DEFAULT_PLATFORMS are returned.
    Otherwise all DEFAULT_PLATFORMS are returned.
    """
    wrapper_cfg = config.get("npm_wrapper", {})
    platforms_filter = wrapper_cfg.get("platforms")

    if platforms_filter is None:
        return list(DEFAULT_PLATFORMS)

    allowed = set(platforms_filter)
    return [spec for spec in DEFAULT_PLATFORMS if spec.npm_platform in allowed]


def build_artifacts(
    specs: list[PlatformSpec],
    name: str,
    archive_fn: Callable[[PlatformSpec, str], tuple[str, str | None, str]],
) -> list[PlatformArtifact]:
    """Combine platform specs with target-specific archive details.

    ``archive_fn(spec, name)`` must return a tuple of
    ``(asset_pattern, extract_cmd, binary_name)`` for each platform.
    """
    artifacts: list[PlatformArtifact] = []
    for spec in specs:
        asset_pattern, extract_cmd, binary_name = archive_fn(spec, name)
        artifacts.append(
            PlatformArtifact(
                npm_platform=spec.npm_platform,
                os_constraint=spec.os_constraint,
                cpu_constraint=spec.cpu_constraint,
                asset_pattern=asset_pattern,
                extract_cmd=extract_cmd,
                binary_name=binary_name,
            )
        )
    return artifacts


def build_npm_publish_jobs(
    npm_scope: str,
    bin_command: str,
    artifacts: list[PlatformArtifact],
    depends_on: str = "goreleaser",
    provenance: bool = False,
) -> str:
    """Generate YAML for npm wrapper publish jobs in a publish workflow.

    Returns a multi-line string injected at the ``jobs:`` level via
    the ``{{npmPublishJobs}}`` template variable in publish.yml.tpl.

    ``artifacts`` provides the per-platform archive details (asset pattern,
    extract command, binary name) so this function is target-agnostic.

    ``depends_on`` is the name of the job that must complete before
    npm-publish runs (e.g. ``"goreleaser"`` for Go, ``"build-and-upload"``
    for Zig).

    ``provenance`` threads ``--provenance`` into ``npm publish`` commands
    and adds ``id-token: write`` permission to the job (required for npm
    OIDC build-provenance attestation on GitHub Actions). Mirrors the
    ``id-token: write`` pattern already used by crates_wrapper.py.
    """
    provenance_flag = " --provenance" if provenance else ""

    # Build the extract step script
    extract_lines = []
    for artifact in artifacts:
        # Resolve {name} and {version} in the asset pattern.
        # {version} stays as a shell variable reference for CI.
        asset = artifact.asset_pattern.format(
            name=bin_command, version="${VERSION}"
        )
        npm_dir = artifact.npm_platform
        if artifact.extract_cmd:
            if "unzip" in artifact.extract_cmd:
                extract_lines.append(
                    f"{artifact.extract_cmd} {asset} -d npm-wrapper/{npm_dir}/"
                )
            else:
                extract_lines.append(
                    f"{artifact.extract_cmd} {asset} -C npm-wrapper/{npm_dir}/"
                )
        else:
            # No extraction needed -- copy the raw binary
            extract_lines.append(
                f"cp {asset} npm-wrapper/{npm_dir}/"
            )
    extract_script = "\n          ".join(extract_lines)

    # Build the per-platform probe + publish step script.
    # Each platform package probes its own @scope/name-platform@version
    # before publishing. If already published, the publish is skipped.
    publish_lines = []
    for artifact in artifacts:
        d = artifact.npm_platform
        pkg_name = f"{npm_scope}/{bin_command}-{d}"
        publish_lines.append(
            f'if npm view "{pkg_name}@${{VERSION}}" version 2>/dev/null; then'
        )
        publish_lines.append(
            f'  echo "Already published: {pkg_name}@${{VERSION}}, skipping"'
        )
        publish_lines.append("else")
        publish_lines.append(
            f"  cd npm-wrapper/{d} && npm publish --access public{provenance_flag} && cd ../.."
        )
        publish_lines.append("fi")
    publish_script = "\n          ".join(publish_lines)

    checkout_action = format_action("actions/checkout")
    setup_node_action = format_action("actions/setup-node")

    # When provenance is enabled, the job needs id-token: write for the
    # OIDC attestation flow (same pattern as crates_wrapper.py).
    if provenance:
        permissions_block = """\
    permissions:
      contents: read
      id-token: write"""
    else:
        permissions_block = ""

    # Build the permissions section (indented under the job key)
    permissions_yaml = f"\n{permissions_block}" if permissions_block else ""

    return f"""
  npm-publish:
    needs: [gate, {depends_on}]
    runs-on: ubuntu-latest{permissions_yaml}
    steps:
      - uses: {checkout_action}
      - uses: {setup_node_action}
        with:
          node-version: 20
          registry-url: https://registry.npmjs.org
      - name: Extract version from tag
        run: echo "VERSION=${{GITHUB_REF_NAME#v}}" >> $GITHUB_ENV
      - name: Download release archives
        run: |
          gh release download v${{VERSION}} --dir .
        env:
          GH_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
      - name: Extract binaries to platform dirs
        run: |
          {extract_script}
      - name: Stamp version
        run: find npm-wrapper -name package.json -exec sed -i "s/0.0.0/$VERSION/g" {{}} +
      - name: Publish platform packages
        run: |
          {publish_script}
        env:
          NODE_AUTH_TOKEN: ${{{{ secrets.NPM_TOKEN }}}}
      - name: Check if wrapper already published
        id: check-wrapper
        run: |
          WRAPPER_NAME="{npm_scope}/{bin_command}"
          if npm view "${{WRAPPER_NAME}}@${{VERSION}}" version 2>/dev/null; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${{WRAPPER_NAME}}@${{VERSION}}"
          fi
      - name: Publish wrapper package
        if: steps.check-wrapper.outputs.skip != 'true'
        run: cd npm-wrapper && npm publish --access public{provenance_flag}
        env:
          NODE_AUTH_TOKEN: ${{{{ secrets.NPM_TOKEN }}}}
"""


def npm_wrapper_template_mappings() -> list[dict[str, str]]:
    """Return template mappings for npm wrapper shared templates.

    Each mapping has ``"template"`` (relative to the shared template dir)
    and ``"target"`` (destination path in the project).
    """
    mappings = [
        {
            "template": "npm-wrapper/package.json.tpl",
            "target": "npm-wrapper/package.json",
        },
        {
            "template": "npm-wrapper/bin-index.js.tpl",
            "target": "npm-wrapper/bin/index.js",
        },
    ]
    for spec in DEFAULT_PLATFORMS:
        mappings.append(
            {
                "template": f"npm-wrapper/platform-{spec.npm_platform}.json.tpl",
                "target": f"npm-wrapper/{spec.npm_platform}/package.json",
            }
        )
    return mappings
