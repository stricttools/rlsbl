name: Publish

on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      tag:
        description: "Release tag to publish (e.g. v1.2.3). Overrides the ref for retry dispatch."
        required: false
        type: string

# One publish run per tag: a workflow_dispatch retry at the same tag
# queues behind the in-flight run instead of racing it. A publish is never
# cancelled mid-flight.
concurrency:
  group: publish-${{ inputs.tag || github.ref_name }}
  cancel-in-progress: false

jobs:
{{publishGate}}
  publish:
    needs: gate
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "actions/setup-java"}}
        with:
          distribution: temurin
          java-version: "25"
      - uses: {{action "gradle/actions/setup-gradle"}}
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - name: Build artifacts
        # Assemble first so the jars exist to be scanned. `./gradlew publish`
        # below reuses the up-to-date build outputs.
        run: ./gradlew assemble
      - name: Scan artifacts for secrets
        # Scoped to the assembled jars. A whole-tree scan flags files that
        # never ship (fixtures, docs, sample configs, public identifiers) and
        # has blocked a release mid-flight. gitleaks only auto-loads
        # .gitleaks.toml from the directory it scans, so the project's
        # allowlist is passed explicitly.
        run: |
          CONFIG_ARGS=""
          if [ -f .gitleaks.toml ]; then
            CONFIG_ARGS="--config ${PWD}/.gitleaks.toml"
          fi
          gitleaks dir build/libs ${CONFIG_ARGS}
      # GitHub Packages allows re-publishing the same version (overwrites),
      # so this step is inherently idempotent -- no pre-check needed.
      - run: ./gradlew publish
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
