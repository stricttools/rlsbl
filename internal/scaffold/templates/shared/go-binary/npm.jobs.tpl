  {{jobKey}}:
    needs: {{needs}}
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "actions/setup-node"}}
        with:
          node-version: 24
          registry-url: https://registry.npmjs.org
      # One package per platform carries the {{binaryName}} binary from the
      # release archives the {{binaryJob}} job attached; the main package
      # selects one through optionalDependencies (npm installs only the one
      # whose os and cpu match) and runs it from bin/index.js. Nothing runs
      # at install time.
      - name: Publish the platform packages and the main package
        env:
          RELEASE_TAG: ${{ inputs.tag || github.ref_name }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
        run: |
          TAG="${RELEASE_TAG}"
          TAG="${TAG##*@}"
          TAG="${TAG##*/}"
          VERSION="${TAG#v}"
          MAIN=$(node -p "require('./package.json').name")
          # Asks for the package's whole version list, never for one version.
          published() {
            npm view "$1" versions --json 2>/dev/null | node -e 'const v=JSON.parse(require("fs").readFileSync(0,"utf8")||"[]");process.exit([].concat(v).includes(process.argv[1])?0:1)' "${VERSION}"
          }
          platform_package() {
            dir="$RUNNER_TEMP/platform-packages/$1"
            archive="{{binaryName}}_${VERSION}_$4_$5.tar.gz"
            mkdir -p "$dir/bin" "$RUNNER_TEMP/archives"
            gh release download "${RELEASE_TAG}" --pattern "${archive}" --dir "$RUNNER_TEMP/archives" --clobber
            tar -xzf "$RUNNER_TEMP/archives/${archive}" -C "$dir/bin" {{binaryName}}
            node -e 'const [file,name,version,os,cpu,license,binary]=process.argv.slice(1);require("fs").writeFileSync(file,JSON.stringify({name,version,description:"The "+os+" "+cpu+" binary of "+binary,os:[os],cpu:[cpu],license,files:["bin"]},null,2)+"\n")' "$dir/package.json" "${MAIN}-$1" "${VERSION}" "$2" "$3" "{{license}}" {{binaryName}}
            if published "${MAIN}-$1"; then
              echo "Already published: ${MAIN}-$1@${VERSION}"
            else
              (cd "$dir" && npm publish --access public{{npm.provenanceFlag}})
            fi
          }
{{platformCalls}}
          node -e 'const fs=require("fs");const [version,binary,...platforms]=process.argv.slice(1);const p=JSON.parse(fs.readFileSync("package.json","utf8"));if(p.version!==version){console.error("::error::package.json carries version "+p.version+", but the release is "+version);process.exit(1)}p.bin={[binary]:"bin/index.js"};p.optionalDependencies=Object.fromEntries(platforms.map(n=>[p.name+"-"+n,version]));fs.writeFileSync("package.json",JSON.stringify(p,null,2)+"\n")' "${VERSION}" {{binaryName}} {{platformNames}}
          if published "${MAIN}"; then
            echo "Already published: ${MAIN}@${VERSION}"
          else
            npm publish --access public{{npm.provenanceFlag}}
          fi
