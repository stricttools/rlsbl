  {{jobKey}}:
    needs: {{needs}}
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
{{npm.setupPackageManager}}      - uses: {{action "actions/setup-node"}}
        with:
          node-version: 24
          registry-url: {{registryUrl}}
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - name: Install dependencies
        # Full install (devDependencies included): publishing runs the
        # package's prepack script, which for a TypeScript package compiles
        # with the dev toolchain. A bare checkout has none of it.
        run: {{npm.installCommand}}
      - name: Pack the publishable artifact
        # Packing runs the same prepack build as publishing, so the tarball
        # scanned below carries what would be published. It is packed
        # outside the checkout: a scratch directory inside the repository
        # would be swept into the published tarball by any package without a
        # `files` field.
        run: |
          mkdir -p "$RUNNER_TEMP/artifacts"
          {{npm.packCommand}}
      - name: Scan artifacts for secrets
        # Scoped to what ships. A whole-tree scan flags files that never
        # reach a registry (fixtures, docs, sample configs, public
        # identifiers) and has blocked a release mid-flight. gitleaks only
        # auto-loads .gitleaks.toml from the directory it scans, so the
        # project's allowlist is passed explicitly.
        run: |
          CONFIG_ARGS=""
          if [ -f .gitleaks.toml ]; then
            CONFIG_ARGS="--config ${PWD}/.gitleaks.toml"
          fi
          gitleaks dir "$RUNNER_TEMP/artifacts" ${CONFIG_ARGS}
      - name: Check if already published
        id: check-npm
        # Asks for the package's whole version list, never for one version.
        run: |
          PKG_NAME=$(node -p "require('./package.json').name")
          PKG_VERSION=$(node -p "require('./package.json').version")
          if npm view "${PKG_NAME}" versions --json 2>/dev/null | node -e 'const v=JSON.parse(require("fs").readFileSync(0,"utf8")||"[]");process.exit([].concat(v).includes(process.argv[1])?0:1)' "${PKG_VERSION}"; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${PKG_NAME}@${PKG_VERSION}"
          fi
      # --access public is written out because scoped packages are not used
      # here and an unscoped public package requires it.{{#if npm.provenance}}
      # --provenance records the build in npm's public attestation log; it is
      # rendered only for a repository that may publish one.{{/if}}
      - run: {{npm.publishCommand}} {{#if npm.provenance}}--provenance {{/if}}--access public
        if: steps.check-npm.outputs.skip != 'true'
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
