  {{jobKey}}:
    needs: {{needs}}
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "astral-sh/setup-uv"}}
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - run: uv build --out-dir dist
      - name: Scan artifacts for secrets
        # gitleaks only auto-loads .gitleaks.toml from the directory it scans,
        # and dist/ never holds the project's allowlist, so the config is
        # passed explicitly.
        run: |
          CONFIG_ARGS=""
          if [ -f .gitleaks.toml ]; then
            CONFIG_ARGS="--config ${PWD}/.gitleaks.toml"
          fi
          gitleaks dir dist/ ${CONFIG_ARGS}
      - uses: {{action "pypa/gh-action-pypi-publish"}}
        with:
          skip-existing: true
          packages-dir: {{pypi.packagesDir}}{{#if pypi.noAttestations}}
          # This repository may not attest: attestations would record its
          # name, workflow, and commit in a public transparency log.
          attestations: false{{/if}}
