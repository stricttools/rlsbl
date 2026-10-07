  {{jobKey}}:
    needs: {{needs}}
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "actions/setup-go"}}
        with:
          go-version-file: go.mod
      - name: Verify module is available on proxy
        env:
          RELEASE_TAG: ${{ inputs.tag || github.ref_name }}
        run: |
          # The module path is the one this module's go.mod declared when it
          # was scaffolded: the full import path, the subdirectory of a nested
          # module included, so `go list -m` asks the proxy for the right
          # module. The proxy resolves a nested module through its own
          # <subdirectory>/vX.Y.Z tag.
          MODULE="{{modulePath}}"

          # RELEASE_TAG is read from the step's environment (never inlined
          # into the script: inputs.tag is user-controlled). Every tag shape
          # (v0.22.0, portal@v0.22.0, cmd/portal/v0.22.0) reduces to its
          # trailing version.
          TAG="${RELEASE_TAG}"
          TAG="${TAG##*@}"
          TAG="${TAG##*/}"
          VERSION="${TAG#v}"

          GOPROXY=proxy.golang.org go list -m "${MODULE}@v${VERSION}"
