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

permissions:
  contents: write

jobs:
{{publishGate}}
  goreleaser:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
          fetch-depth: 0
      - uses: {{action "actions/setup-go"}}
        with:
          go-version-file: go.mod
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - name: Check if already published
        id: check-go
        run: |
          TAG="${RELEASE_TAG}"
          if git ls-remote --tags origin "${TAG}" | grep -q "${TAG}"; then
            # Tag already pushed and goreleaser assets likely exist
            RELEASE_ASSETS=$(gh release view "${TAG}" --json assets -q '.assets | length' 2>/dev/null || echo "0")
            if [ "${RELEASE_ASSETS}" -gt 0 ]; then
              echo "skip=true" >> "$GITHUB_OUTPUT"
              echo "Already published: ${TAG} (${RELEASE_ASSETS} assets)"
            fi
          fi
        env:
          RELEASE_TAG: ${{ inputs.tag || github.ref_name }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      # rlsbl owns the release, goreleaser only BUILDS.
      #
      # goreleaser's publisher cannot target a prefixed monorepo tag
      # ("<name>@v1.2.3"): its tag validation rejects the prefix, and with
      # --skip=validate its release lookup still resolves the stripped bare
      # name and would create/append to a `v1.2.3` Release that no launcher
      # shim ever reads. (`monorepo:` is goreleaser-Pro.) So the artifacts are
      # built here and attached to the prefixed Release rlsbl already created.
      - name: Derive the bare semver tag for goreleaser
        id: bare-tag
        if: steps.check-go.outputs.skip != 'true'
        run: |
          # Strip any "<name>@" / "<path>/" prefix: keep the trailing vX.Y.Z[-pre].
          BARE=$(printf '%s' "${RELEASE_TAG}" | sed -E 's/^.*(v[0-9]+\.[0-9]+\.[0-9]+.*)$/\1/')
          echo "tag=${BARE}" >> "$GITHUB_OUTPUT"
          # goreleaser reads GORELEASER_CURRENT_TAG's tag contents while getting
          # git state, BEFORE --skip=validate can excuse anything, so a name no
          # tag carries aborts the run: `couldn't get tag contents: unexpected
          # git tag output for "v0.2.4": ""`. A monorepo repository carries only
          # the prefixed tags, so create the bare one locally at the released
          # commit. It is a lightweight tag (an annotated one needs a tagger
          # identity no CI checkout sets) and nothing pushes it: goreleaser runs
          # --skip=publish, and the upload step below attaches the archives to
          # the prefixed tag. In a repository whose tags are already bare this
          # is a no-op.
          if ! git rev-parse -q --verify "refs/tags/${BARE}" >/dev/null; then
            git tag "${BARE}" "$(git rev-parse HEAD)"
          fi
        env:
          RELEASE_TAG: ${{ inputs.tag || github.ref_name }}
      - uses: {{action "goreleaser/goreleaser-action"}}
        if: steps.check-go.outputs.skip != 'true'
        with:
          # Pinned exactly, from rlsbl's action-version table. A floating
          # "~> v2" silently adopts each new goreleaser on the next release:
          # 2.18.1 arrived that way and broke every prefixed-tag repository.
          version: {{actionVersion "goreleaser/goreleaser"}}
          args: release --clean --skip=publish,announce,validate
        env:
          # A prefixed tag is not parseable as semver, and validation would
          # compare the stripped tag against a git tag that does not exist.
          GORELEASER_CURRENT_TAG: ${{ steps.bare-tag.outputs.tag }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      - name: Scan artifacts for secrets
        # Scoped to what goreleaser just built and is about to upload. A
        # whole-tree scan flags files that never ship (fixtures, docs, sample
        # configs, public identifiers) and has blocked a release mid-flight.
        # gitleaks only auto-loads .gitleaks.toml from the directory it scans,
        # so the project's allowlist is passed explicitly.
        if: steps.check-go.outputs.skip != 'true'
        run: |
          CONFIG_ARGS=""
          if [ -f .gitleaks.toml ]; then
            CONFIG_ARGS="--config ${PWD}/.gitleaks.toml"
          fi
          gitleaks dir dist/ ${CONFIG_ARGS}
      - name: Upload release assets to the release tag
        if: steps.check-go.outputs.skip != 'true'
        run: |
          # dist/ holds the archives plus checksums.txt (the literal filename
          # the launcher shims verify against). --clobber keeps a re-dispatch
          # at the same tag idempotent.
          shopt -s nullglob
          ASSETS=(dist/*.tar.gz dist/*.zip dist/checksums.txt)
          if [ ${#ASSETS[@]} -eq 0 ]; then
            echo "::error::goreleaser produced no assets in dist/"
            exit 1
          fi
          gh release upload "${RELEASE_TAG}" "${ASSETS[@]}" --clobber
        env:
          RELEASE_TAG: ${{ inputs.tag || github.ref_name }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
{{homebrewEnv}}
{{npmPublishJobs}}
