"""Helpers for crates.io binary wrapper packages (shims for Go tools).

Parallel to npm_wrapper.py: provides template mappings and publish job
generation for cargo-binstall + build.rs shim crates that wrap prebuilt
Go binaries distributed via GitHub Releases.
"""

from __future__ import annotations

from .action_versions import format_action


# Rust target triples that map to goreleaser's archive naming convention.
# Each tuple: (rust_target, goreleaser_suffix, archive_ext)
CRATES_PLATFORMS: list[tuple[str, str, str]] = [
    ("x86_64-unknown-linux-gnu", "linux_amd64", "tar.gz"),
    ("aarch64-unknown-linux-gnu", "linux_arm64", "tar.gz"),
    ("x86_64-apple-darwin", "darwin_amd64", "tar.gz"),
    ("aarch64-apple-darwin", "darwin_arm64", "tar.gz"),
    ("x86_64-pc-windows-msvc", "windows_amd64", "zip"),
    ("aarch64-pc-windows-msvc", "windows_arm64", "zip"),
]


def crates_wrapper_template_mappings() -> list[dict[str, str]]:
    """Return template mappings for the crates wrapper directory.

    Templates live in ``templates/shared/crates-wrapper/`` and are
    scaffolded into ``crates-wrapper/`` in the project root.
    """
    return [
        {
            "template": "crates-wrapper/Cargo.toml.tpl",
            "target": "crates-wrapper/Cargo.toml",
        },
        {
            "template": "crates-wrapper/src/main.rs.tpl",
            "target": "crates-wrapper/src/main.rs",
        },
        {
            "template": "crates-wrapper/build.rs.tpl",
            "target": "crates-wrapper/build.rs",
        },
    ]


def build_crates_publish_jobs(
    bin_command: str,
    repo_name: str,
    depends_on: str = "goreleaser",
) -> str:
    """Generate YAML for crates.io wrapper publish job in a publish workflow.

    Returns a multi-line string injected at the ``jobs:`` level via
    the ``{{cratesPublishJobs}}`` template variable in publish.yml.tpl.

    The job runs AFTER the goreleaser job (GitHub Release assets must exist
    first, since the shim's build.rs downloads from there).
    """
    checkout_action = format_action("actions/checkout")
    rust_toolchain_action = format_action("dtolnay/rust-toolchain")
    crates_auth_action = format_action("rust-lang/crates-io-auth-action")

    return f"""
  crates-publish:
    needs: [gate, {depends_on}]
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: {checkout_action}
      - uses: {rust_toolchain_action}
      - name: Extract version from tag
        run: echo "VERSION=${{GITHUB_REF_NAME#v}}" >> $GITHUB_ENV
      - name: Stamp version in crates wrapper
        run: sed -i "s/0.0.0/$VERSION/g" crates-wrapper/Cargo.toml
      - name: Check if already published
        id: check-crate
        run: |
          CRATE_NAME=$(grep '^name' crates-wrapper/Cargo.toml | head -1 | sed 's/.*"\\(.*\\)".*/\\1/')
          if curl -sf "https://crates.io/api/v1/crates/${{CRATE_NAME}}/${{VERSION}}" -H "User-Agent: rlsbl-ci" > /dev/null 2>&1; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${{CRATE_NAME}}@${{VERSION}}"
          fi
      - name: Authenticate with crates.io
        if: steps.check-crate.outputs.skip != 'true'
        uses: {crates_auth_action}
        id: crates-auth
      - name: Publish to crates.io
        if: steps.check-crate.outputs.skip != 'true'
        run: cargo publish --allow-dirty
        working-directory: crates-wrapper
        env:
          CARGO_REGISTRY_TOKEN: ${{{{ steps.crates-auth.outputs.token }}}}
"""
