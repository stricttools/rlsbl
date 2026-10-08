package workflows

import (
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/pipelines"
)

// The jobs packaging a go binary pipeline's binaries for npm and PyPI.
//
// Both take the binaries from the release archives the go binary pipeline's
// job (goreleaser) attaches to the GitHub Release, one archive per platform
// of pipelines.Platforms, and neither asks a registry about one version: the
// npm jobs read the package document's version list, and PyPI's publish
// skips what it already holds.

// ActionVersions maps a GitHub action ("actions/checkout") to the version
// generated workflows pin it at ("v6"). Scaffold holds the table.
type ActionVersions map[string]string

// ref is the pinned reference of an action, "owner/repo@version". An action
// the table does not hold is refused.
func (v ActionVersions) ref(action string) (string, error) {
	version, ok := v[action]
	if !ok || version == "" {
		return "", fmt.Errorf("the action version table holds no version of %s", action)
	}
	return action + "@" + version, nil
}

// The jobs' keys.
const (
	// NPMPackageJobKey publishes the main npm package.
	NPMPackageJobKey = "npm-package"
	// WheelJobKey assembles and publishes the PyPI binary wheels.
	WheelJobKey = "pypi-wheels"
)

// NPMPlatformJobKey is the key of the job publishing the platform package
// of p.
func NPMPlatformJobKey(p pipelines.Platform) string { return "npm-" + p.Name }

// GoBinaryRelease is what every packaging job needs about the go binary
// pipeline whose binaries it packages.
type GoBinaryRelease struct {
	// BinaryJob is the key of the job attaching the release archives.
	BinaryJob string
	// Binary is the binary's name: the name inside each archive and the
	// archives' name prefix (pipelines.ArchiveName).
	Binary string
	// Tag is the releasable's tag scheme; the jobs read the version from the
	// tag through it.
	Tag TagParts
}

var binaryName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

func (g GoBinaryRelease) check() error {
	if err := validJobKey(g.BinaryJob); err != nil {
		return fmt.Errorf("the job attaching the release archives: %w", err)
	}
	if !binaryName.MatchString(g.Binary) {
		return fmt.Errorf("the binary name %q is not a file name the release archives can carry", g.Binary)
	}
	if g.Tag.Prefix == "" && g.Tag.Suffix == "" {
		return errors.New("the tag scheme is empty around its version, so a job cannot tell a release tag from any other ref")
	}
	return nil
}

// versionScript reads VERSION from RELEASE_TAG through the tag scheme and
// refuses a tag outside it or a version that is not MAJOR.MINOR.PATCH.
func (g GoBinaryRelease) versionScript() string {
	scheme := shellQuote(g.Tag.Prefix) + "*" + shellQuote(g.Tag.Suffix)
	return strings.Join([]string{
		`case "$RELEASE_TAG" in`,
		"  " + scheme + ") ;;",
		`  *) echo "::error::the tag $RELEASE_TAG is not one of this releasable's tags (` + g.Tag.Prefix + `<version>` + g.Tag.Suffix + `)"; exit 1 ;;`,
		"esac",
		`VERSION="${RELEASE_TAG#` + shellQuote(g.Tag.Prefix) + `}"`,
		`VERSION="${VERSION%` + shellQuote(g.Tag.Suffix) + `}"`,
		`if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then`,
		`  echo "::error::the tag $RELEASE_TAG carries the version '$VERSION', which is not MAJOR.MINOR.PATCH"`,
		"  exit 1",
		"fi",
	}, "\n") + "\n"
}

// extractScript downloads p's archive into $work/archive and extracts the
// binary from it.
func (g GoBinaryRelease) extractScript(p pipelines.Platform) string {
	archive := pipelines.ArchiveName(g.Binary, "${VERSION}", p)
	return strings.Join([]string{
		`mkdir -p "$work/archive"`,
		`gh release download "$RELEASE_TAG" --pattern "` + archive + `" --dir "$work/archive"`,
		`tar -xzf "$work/archive/` + archive + `" -C "$work/archive" ` + shellQuote(g.Binary),
	}, "\n") + "\n"
}

// NPMPackaging is the npm side of a go binary pipeline: the main package,
// which the operator installs, and one platform package per platform, which
// the main package selects through optionalDependencies by os and cpu. No
// package runs an install script.
type NPMPackaging struct {
	GoBinaryRelease
	// Package is the main package's name; each platform package is named
	// pipelines.PlatformPackageName(Package, platform).
	Package string
	// Dir is the main package's directory (its package.json and the bin
	// launcher resolving the platform package), relative to the directory
	// the jobs run in: the repository root of a standalone repository, the
	// member's directory in a workspace's publish router.
	Dir string
	// License is the SPDX license the platform packages carry: the
	// releasable's current license in the lifecycle-and-license record.
	License string
	// RepositoryURL is the repository field of the platform packages; empty
	// writes none. Scaffold gives one only when its workflow features allow
	// repository URLs.
	RepositoryURL string
	// Provenance publishes with npm build provenance, which scaffold gives
	// only when its workflow features allow build attestations.
	Provenance bool
	// Actions pins the actions the jobs use.
	Actions ActionVersions
}

var npmName = regexp.MustCompile(`^[a-z0-9][a-z0-9._~-]*$`)

// npmPublishedFunction is the shell function answering whether a package
// version is published, from the package document's version list. A
// package npm has no document for (E404) is unpublished; any other failure
// stops the job.
const npmPublishedFunction = `published() {
  if out="$(npm view "$1" versions --json 2>&1)"; then
    printf '%s' "$out" | node -e 'let s = ""; process.stdin.on("data", (d) => (s += d)).on("end", () => { const v = JSON.parse(s); process.exit((Array.isArray(v) ? v : [v]).includes(process.argv[1]) ? 0 : 1); });' "$2"
    return
  fi
  case "$out" in
    *E404*) return 1 ;;
  esac
  echo "::error::npm could not list the published versions of $1: $out"
  exit 1
}
`

func (n NPMPackaging) check() error {
	if err := n.GoBinaryRelease.check(); err != nil {
		return err
	}
	if !npmName.MatchString(n.Package) {
		return fmt.Errorf("%q is not an unscoped npm package name; the main package and its platform packages are unscoped", n.Package)
	}
	if strings.TrimSpace(n.License) == "" {
		return errors.New("the npm platform packages need the releasable's license from the lifecycle-and-license record")
	}
	if n.Dir == "" || strings.HasPrefix(n.Dir, "/") || strings.Contains(n.Dir, "..") {
		return fmt.Errorf("the main package's directory %q must be a relative path inside the repository", n.Dir)
	}
	return nil
}

// publishFlags are npm publish's flags.
func (n NPMPackaging) publishFlags() string {
	if n.Provenance {
		return "--access public --provenance"
	}
	return "--access public"
}

// permissions are a publishing job's permissions.
func (n NPMPackaging) permissions() string {
	if n.Provenance {
		return "    permissions:\n      contents: read\n      id-token: write\n"
	}
	return "    permissions:\n      contents: read\n"
}

// NPMPackagingJobs are the jobs publishing a go binary pipeline to npm: one
// per platform, each packing the platform's binary from its release archive
// into the platform package (name, version, os, cpu, license, and no
// scripts) and publishing it, then the main package, whose package.json
// must carry the released version and no install script, with its
// optionalDependencies pinned to the platform packages at that version. The
// lines sit under the workflow's jobs key, ending in a newline; every job
// needs wait-for-ci.
func NPMPackagingJobs(n NPMPackaging) (string, error) {
	if err := n.check(); err != nil {
		return "", err
	}
	setupNode, err := n.Actions.ref("actions/setup-node")
	if err != nil {
		return "", err
	}
	checkout, err := n.Actions.ref("actions/checkout")
	if err != nil {
		return "", err
	}
	var b strings.Builder
	var platformJobs []string
	optional := map[string]bool{}
	for _, p := range pipelines.Platforms() {
		pkg := pipelines.PlatformPackageName(n.Package, p)
		key := NPMPlatformJobKey(p)
		platformJobs = append(platformJobs, key)
		optional[pkg] = true
		manifest := map[string]any{
			"name":        pkg,
			"version":     "@VERSION@",
			"description": fmt.Sprintf("The %s binary of %s for %s %s", n.Binary, n.Package, p.OS, p.CPU),
			"os":          []string{p.OS},
			"cpu":         []string{p.CPU},
			"license":     n.License,
			"files":       []string{n.Binary},
		}
		if n.RepositoryURL != "" {
			manifest["repository"] = map[string]string{"type": "git", "url": n.RepositoryURL}
		}
		manifestJSON, err := json.MarshalIndent(manifest, "", "  ")
		if err != nil {
			return "", err
		}
		script := "set -euo pipefail\n" + n.versionScript() +
			`work="$RUNNER_TEMP/` + key + `"` + "\n" +
			n.extractScript(p) +
			`mkdir -p "$work/package"` + "\n" +
			`install -m 0755 "$work/archive/` + n.Binary + `" "$work/package/` + n.Binary + `"` + "\n" +
			`sed "s/@VERSION@/$VERSION/" > "$work/package/package.json" <<'MANIFEST'` + "\n" +
			string(manifestJSON) + "\nMANIFEST\n" +
			npmPublishedFunction +
			`if published ` + shellQuote(pkg) + ` "$VERSION"; then` + "\n" +
			`  echo "` + pkg + `@$VERSION is published already"` + "\n" +
			"else\n" +
			`  (cd "$work/package" && npm publish ` + n.publishFlags() + `)` + "\n" +
			"fi\n"
		fmt.Fprintf(&b, "  %s:\n", key)
		fmt.Fprintf(&b, "    name: Publish the npm package %s\n", pkg)
		fmt.Fprintf(&b, "    needs: [%s, %s]\n", WaitForCIJobKey, n.BinaryJob)
		b.WriteString("    runs-on: ubuntu-latest\n")
		b.WriteString(n.permissions())
		b.WriteString("    steps:\n")
		fmt.Fprintf(&b, "      - uses: %s\n", setupNode)
		b.WriteString("        with:\n          node-version: 24\n          registry-url: https://registry.npmjs.org\n")
		fmt.Fprintf(&b, "      - name: Pack and publish %s\n", pkg)
		b.WriteString("        env:\n")
		b.WriteString("          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n")
		b.WriteString("          GH_REPO: ${{ github.repository }}\n")
		b.WriteString("          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}\n")
		b.WriteString("          RELEASE_TAG: " + TagExpression + "\n")
		b.WriteString("        run: |\n")
		b.WriteString(literalBlock(script, "          "))
	}

	names := make([]string, 0, len(optional))
	for pkg := range optional {
		names = append(names, pkg)
	}
	sort.Strings(names)
	namesJSON, err := json.Marshal(names)
	if err != nil {
		return "", err
	}
	stamp := strings.Join([]string{
		`const fs = require("fs");`,
		`const version = process.argv[1];`,
		`const name = ` + jsString(n.Package) + `;`,
		`const platforms = ` + string(namesJSON) + `;`,
		`const pkg = JSON.parse(fs.readFileSync("package.json", "utf8"));`,
		`const fail = (m) => { console.log("::error::" + m); process.exit(1); };`,
		`if (pkg.name !== name) fail("package.json names the package " + pkg.name + ", not " + name);`,
		`if (pkg.version !== version) fail("package.json carries the version " + pkg.version + ", not the released " + version);`,
		`for (const s of ["preinstall", "install", "postinstall"]) { if (pkg.scripts && pkg.scripts[s]) fail("package.json declares a " + s + " script; the package selects its platform package through optionalDependencies and runs nothing at install"); }`,
		`pkg.optionalDependencies = Object.assign({}, pkg.optionalDependencies);`,
		`for (const p of platforms) pkg.optionalDependencies[p] = version;`,
		`fs.writeFileSync("package.json", JSON.stringify(pkg, null, 2) + "\n");`,
	}, "\n")
	script := "set -euo pipefail\n" + n.versionScript() +
		"cd " + shellQuote(n.Dir) + "\n" +
		`node -e "$(cat <<'STAMP'` + "\n" + stamp + "\nSTAMP\n" + `)" "$VERSION"` + "\n" +
		npmPublishedFunction +
		`if published ` + shellQuote(n.Package) + ` "$VERSION"; then` + "\n" +
		`  echo "` + n.Package + `@$VERSION is published already"` + "\n" +
		"else\n" +
		"  npm publish " + n.publishFlags() + "\n" +
		"fi\n"
	fmt.Fprintf(&b, "  %s:\n", NPMPackageJobKey)
	fmt.Fprintf(&b, "    name: Publish the npm package %s\n", n.Package)
	fmt.Fprintf(&b, "    needs: [%s, %s]\n", WaitForCIJobKey, strings.Join(platformJobs, ", "))
	b.WriteString("    runs-on: ubuntu-latest\n")
	b.WriteString(n.permissions())
	b.WriteString("    steps:\n")
	fmt.Fprintf(&b, "      - uses: %s\n", checkout)
	b.WriteString("        with:\n          ref: " + TagExpression + "\n")
	fmt.Fprintf(&b, "      - uses: %s\n", setupNode)
	b.WriteString("        with:\n          node-version: 24\n          registry-url: https://registry.npmjs.org\n")
	fmt.Fprintf(&b, "      - name: Pin the platform packages and publish %s\n", n.Package)
	b.WriteString("        env:\n")
	b.WriteString("          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}\n")
	b.WriteString("          RELEASE_TAG: " + TagExpression + "\n")
	b.WriteString("        run: |\n")
	b.WriteString(literalBlock(script, "          "))
	return b.String(), nil
}

// jsString is s as a JavaScript string literal.
func jsString(s string) string {
	data, _ := json.Marshal(s)
	return string(data)
}

// WheelPackaging is the PyPI side of a go binary pipeline: one binary wheel
// per platform, py3-none-<platform tag>, with the binary under
// <distribution>-<version>.data/scripts/, published through Trusted
// Publishing.
type WheelPackaging struct {
	GoBinaryRelease
	// Dir is the directory of the pyproject.toml whose [project] table gives
	// the wheels their name, version, description, license, urls,
	// keywords, classifiers, and readme, relative to the directory the job runs in.
	Dir string
	// Attestations publishes with PyPI attestations, which scaffold gives
	// only when its workflow features allow build attestations.
	Attestations bool
	// Actions pins the actions the job uses.
	Actions ActionVersions
}

// wheelMetadataScript reads pyproject.toml's [project] table into the
// wheel's METADATA (with the readme as its description body, which PyPI
// shows on the project page) and its distribution name, refusing a version
// other than the released one.
const wheelMetadataScript = `import os, sys, tomllib
path, version, out = sys.argv[1:4]
with open(path, "rb") as f:
    project = tomllib.load(f).get("project")
if not isinstance(project, dict):
    sys.exit(f"::error::{path} has no [project] table")
name = project.get("name")
if not isinstance(name, str) or not name:
    sys.exit(f"::error::{path} declares no project name")
if project.get("version") != version:
    sys.exit(f"::error::{path} carries the version {project.get('version')!r}, not the released {version}")
lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
description = project.get("description")
if description:
    lines.append(f"Summary: {description}")
license = project.get("license")
if isinstance(license, str):
    lines.append(f"License-Expression: {license}")
elif license is not None:
    sys.exit(f"::error::{path} declares its license as a table; write it as an SPDX expression string")
for label, url in (project.get("urls") or {}).items():
    lines.append(f"Project-URL: {label}, {url}")
keywords = project.get("keywords")
if keywords:
    lines.append("Keywords: " + ",".join(keywords))
for classifier in project.get("classifiers") or []:
    lines.append(f"Classifier: {classifier}")
body = ""
readme = project.get("readme")
if readme is not None:
    types = {".md": "text/markdown", ".rst": "text/x-rst", ".txt": "text/plain"}
    if isinstance(readme, str):
        file, text = readme, None
        content_type = types.get(os.path.splitext(readme)[1].lower())
        if content_type is None:
            sys.exit(f"::error::{path} names the readme {readme}, whose suffix is none of {', '.join(types)}; write readme as a table with its content-type")
    else:
        file, text, content_type = readme.get("file"), readme.get("text"), readme.get("content-type")
        if not content_type:
            sys.exit(f"::error::{path} declares its readme as a table without a content-type")
    if text is None:
        readme_path = os.path.join(os.path.dirname(path), file)
        try:
            with open(readme_path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            sys.exit(f"::error::{path} names the readme {file}, which cannot be read: {e}")
    lines.append(f"Description-Content-Type: {content_type}")
    body = "\n" + text
with open(out + "/METADATA", "w") as f:
    f.write("\n".join(lines) + "\n" + body)
with open(out + "/name", "w") as f:
    f.write(name)
`

// wheelRecordFunction writes a wheel's RECORD: every file with its
// urlsafe-base64 SHA-256 digest (no padding) and its size, then RECORD
// itself.
const wheelRecordFunction = `write_record() {
  record="$1/$2/RECORD"
  : > "$record"
  (cd "$1" && find . -type f ! -path "./$2/RECORD" | sed 's|^\./||' | LC_ALL=C sort) | while IFS= read -r f; do
    hex="$(sha256sum "$1/$f" | cut -d' ' -f1)"
    digest="$(printf '%b' "$(printf '%s' "$hex" | sed 's/../\\x&/g')" | base64 -w0 | tr '+/' '-_' | tr -d '=')"
    size="$(wc -c < "$1/$f" | tr -d ' ')"
    printf '%s,sha256=%s,%s\n' "$f" "$digest" "$size" >> "$record"
  done
  printf '%s/RECORD,,\n' "$2" >> "$record"
}
`

// WheelJob is the job assembling one binary wheel per platform from the
// release archives and publishing them through Trusted Publishing: the
// lines under the workflow's jobs key, ending in a newline. The wheels are
// written to dist/ under the job's directory, which must be empty or
// absent, and published from there (packages-dir dist/).
func WheelJob(w WheelPackaging) (string, error) {
	if err := w.GoBinaryRelease.check(); err != nil {
		return "", err
	}
	if w.Dir == "" || strings.HasPrefix(w.Dir, "/") || strings.Contains(w.Dir, "..") {
		return "", fmt.Errorf("the pyproject.toml directory %q must be a relative path inside the repository", w.Dir)
	}
	checkout, err := w.Actions.ref("actions/checkout")
	if err != nil {
		return "", err
	}
	publish, err := w.Actions.ref("pypa/gh-action-pypi-publish")
	if err != nil {
		return "", err
	}
	var s strings.Builder
	s.WriteString("set -euo pipefail\n")
	s.WriteString(w.versionScript())
	s.WriteString(`if [ -d dist ] && [ -n "$(ls -A dist)" ]; then` + "\n")
	s.WriteString(`  echo "::error::dist/ is not empty at the tag; the wheels are published from it, so it must hold nothing else"` + "\n")
	s.WriteString("  exit 1\nfi\n")
	s.WriteString(`out="$PWD/dist"` + "\n")
	s.WriteString(`meta="$RUNNER_TEMP/wheel-metadata"` + "\n")
	s.WriteString(`mkdir -p "$out" "$meta"` + "\n")
	s.WriteString(`python3 - ` + shellQuote(w.Dir+"/pyproject.toml") + ` "$VERSION" "$meta" <<'METADATA'` + "\n")
	s.WriteString(wheelMetadataScript)
	s.WriteString("METADATA\n")
	s.WriteString(`dist="$(tr 'A-Z' 'a-z' < "$meta/name" | sed -E 's/[-_.]+/_/g')"` + "\n")
	s.WriteString(wheelRecordFunction)
	for _, p := range pipelines.Platforms() {
		// $dist is folded already, and WheelName's folding leaves the
		// variable reference as it is.
		wheel := pipelines.WheelName("${dist}", "${VERSION}", p)
		var tags []string
		for _, tag := range strings.Split(p.WheelTag, ".") {
			tags = append(tags, "Tag: py3-none-"+tag)
		}
		fmt.Fprintf(&s, "# %s\n", p.Name)
		s.WriteString(`work="$RUNNER_TEMP/wheel-` + p.Name + `"` + "\n")
		s.WriteString(w.extractScript(p))
		s.WriteString(`tree="$work/tree"` + "\n")
		s.WriteString(`mkdir -p "$tree/${dist}-${VERSION}.data/scripts" "$tree/${dist}-${VERSION}.dist-info"` + "\n")
		s.WriteString(`install -m 0755 "$work/archive/` + w.Binary + `" "$tree/${dist}-${VERSION}.data/scripts/` + w.Binary + `"` + "\n")
		s.WriteString(`cp "$meta/METADATA" "$tree/${dist}-${VERSION}.dist-info/METADATA"` + "\n")
		s.WriteString(`printf '%s\n' 'Wheel-Version: 1.0' 'Generator: rlsbl' 'Root-Is-Purelib: false'`)
		for _, tag := range tags {
			s.WriteString(" " + shellQuote(tag))
		}
		s.WriteString(` > "$tree/${dist}-${VERSION}.dist-info/WHEEL"` + "\n")
		s.WriteString(`write_record "$tree" "${dist}-${VERSION}.dist-info"` + "\n")
		s.WriteString(`(cd "$tree" && zip -q -X -D -r "$out/` + wheel + `" .)` + "\n")
	}
	s.WriteString(`ls -l "$out"` + "\n")

	var b strings.Builder
	fmt.Fprintf(&b, "  %s:\n", WheelJobKey)
	b.WriteString("    name: Publish the PyPI binary wheels\n")
	fmt.Fprintf(&b, "    needs: [%s, %s]\n", WaitForCIJobKey, w.BinaryJob)
	b.WriteString("    runs-on: ubuntu-latest\n")
	// Trusted Publishing authenticates with the job's OIDC token.
	b.WriteString("    permissions:\n      contents: read\n      id-token: write\n")
	b.WriteString("    steps:\n")
	fmt.Fprintf(&b, "      - uses: %s\n", checkout)
	b.WriteString("        with:\n          ref: " + TagExpression + "\n")
	b.WriteString("      - name: Assemble one binary wheel per platform\n")
	b.WriteString("        env:\n")
	b.WriteString("          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n")
	b.WriteString("          GH_REPO: ${{ github.repository }}\n")
	b.WriteString("          RELEASE_TAG: " + TagExpression + "\n")
	b.WriteString("        run: |\n")
	b.WriteString(literalBlock(s.String(), "          "))
	fmt.Fprintf(&b, "      - uses: %s\n", publish)
	b.WriteString("        with:\n")
	b.WriteString("          packages-dir: dist/\n")
	b.WriteString("          skip-existing: true\n")
	if !w.Attestations {
		b.WriteString("          attestations: false\n")
	}
	return b.String(), nil
}
