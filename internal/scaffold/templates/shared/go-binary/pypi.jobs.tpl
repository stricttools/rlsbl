  {{jobKey}}:
    needs: {{needs}}
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      # One wheel per platform carries the {{binaryName}} binary from the
      # release archives the {{binaryJob}} job attached, as a script pip
      # installs onto PATH.
      - name: Build the platform wheels
        env:
          RELEASE_TAG: ${{ inputs.tag || github.ref_name }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          TAG="${RELEASE_TAG}"
          TAG="${TAG##*@}"
          TAG="${TAG##*/}"
          VERSION="${TAG#v}"
          NAME=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["name"])')
          SUMMARY=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"].get("description", ""))')
          DECLARED=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
          if [ "${DECLARED}" != "${VERSION}" ]; then
            echo "::error::pyproject.toml carries version ${DECLARED}, but the release is ${VERSION}"
            exit 1
          fi
          DIST=$(printf '%s' "${NAME}" | tr 'A-Z' 'a-z' | sed -E 's/[-_.]+/_/g')
          OUT="${GITHUB_WORKSPACE}/{{wheelsDir}}"
          mkdir -p "${OUT}" "$RUNNER_TEMP/archives"
          record_line() {
            hex=$(sha256sum "$1" | cut -d' ' -f1)
            digest=$(printf "$(printf '%s' "${hex}" | sed 's/../\\x&/g')" | base64 -w0 | tr '+/' '-_' | tr -d '=')
            printf '%s,sha256=%s,%s\n' "$1" "${digest}" "$(wc -c < "$1" | tr -d ' ')"
          }
          wheel() {
            root="$RUNNER_TEMP/wheel-$2-$3"
            scripts="${root}/${DIST}-${VERSION}.data/scripts"
            info="${root}/${DIST}-${VERSION}.dist-info"
            mkdir -p "${scripts}" "${info}"
            archive="{{binaryName}}_${VERSION}_$2_$3.tar.gz"
            gh release download "${RELEASE_TAG}" --pattern "${archive}" --dir "$RUNNER_TEMP/archives" --clobber
            tar -xzf "$RUNNER_TEMP/archives/${archive}" -C "${scripts}" {{binaryName}}
            chmod 755 "${scripts}/{{binaryName}}"
            printf 'Metadata-Version: 2.1\nName: %s\nVersion: %s\nSummary: %s\nLicense: %s\n' "${NAME}" "${VERSION}" "${SUMMARY}" "{{license}}" > "${info}/METADATA"
            printf 'Wheel-Version: 1.0\nGenerator: rlsbl\nRoot-Is-Purelib: false\n' > "${info}/WHEEL"
            for platform in $(printf '%s' "$1" | tr '.' ' '); do
              printf 'Tag: py3-none-%s\n' "${platform}" >> "${info}/WHEEL"
            done
            (
              cd "${root}"
              record="${DIST}-${VERSION}.dist-info/RECORD"
              for f in "${DIST}-${VERSION}.data/scripts/{{binaryName}}" "${DIST}-${VERSION}.dist-info/METADATA" "${DIST}-${VERSION}.dist-info/WHEEL"; do
                record_line "$f" >> "${record}"
              done
              printf '%s,,\n' "${record}" >> "${record}"
              zip -q -r -X "${OUT}/${DIST}-${VERSION}-py3-none-$1.whl" .
            )
          }
{{wheelCalls}}
          ls -l "${OUT}"
      - uses: {{action "pypa/gh-action-pypi-publish"}}
        with:
          skip-existing: true
          packages-dir: {{wheelsDir}}/{{#if pypi.noAttestations}}
          # This repository may not attest: attestations would record its
          # name, workflow, and commit in a public transparency log.
          attestations: false{{/if}}
