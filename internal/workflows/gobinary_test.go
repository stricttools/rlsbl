package workflows

import (
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/pipelines"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

var testActions = ActionVersions{
	"actions/checkout":            "v6",
	"actions/setup-node":          "v6",
	"pypa/gh-action-pypi-publish": "release/v1",
}

func portalBinary() GoBinaryRelease {
	return GoBinaryRelease{BinaryJob: "goreleaser", Binary: "portal", Tag: TagParts{Prefix: "v"}}
}

func stepRuns(t *testing.T, j map[string]any) string {
	t.Helper()
	var runs []string
	for _, s := range j["steps"].([]any) {
		if r, ok := s.(map[string]any)["run"].(string); ok {
			runs = append(runs, r)
		}
	}
	return strings.Join(runs, "\n")
}

func needsOf(t *testing.T, j map[string]any) []string {
	t.Helper()
	var out []string
	for _, n := range j["needs"].([]any) {
		out = append(out, n.(string))
	}
	return out
}

func TestTheNPMJobsPublishOnePlatformPackagePerPlatformThenTheMainPackage(t *testing.T) {
	hygiene.Isolate(t)
	text, err := NPMPackagingJobs(NPMPackaging{GoBinaryRelease: portalBinary(), Package: "portal", Dir: "npm", License: "MIT", Actions: testActions})
	if err != nil {
		t.Fatal(err)
	}
	doc := parseYAML(t, "jobs:\n"+text)
	var platformKeys []string
	for _, p := range pipelines.Platforms() {
		key := NPMPlatformJobKey(p)
		platformKeys = append(platformKeys, key)
		j := job(t, doc, key)
		if got := needsOf(t, j); !reflect.DeepEqual(got, []string{WaitForCIJobKey, "goreleaser"}) {
			t.Fatalf("%s needs %v", key, got)
		}
		run := stepRuns(t, j)
		pkg := pipelines.PlatformPackageName("portal", p)
		for _, want := range []string{
			pipelines.ArchiveName("portal", "${VERSION}", p),
			fmt.Sprintf(`"os": [`+"\n"+`    %q`, p.OS),
			fmt.Sprintf(`"cpu": [`+"\n"+`    %q`, p.CPU),
			fmt.Sprintf(`"name": %q`, pkg),
			`"license": "MIT"`,
			"published '" + pkg + `' "$VERSION"`,
			"npm publish --access public)",
		} {
			if !strings.Contains(run, want) {
				t.Fatalf("%s does not carry %q:\n%s", key, want, run)
			}
		}
		if strings.Contains(run, "repository") || strings.Contains(run, "--provenance") {
			t.Fatalf("%s carries a repository field or provenance it was not given:\n%s", key, run)
		}
		if _, ok := j["permissions"].(map[string]any)["id-token"]; ok {
			t.Fatalf("%s asks for an OIDC token without provenance", key)
		}
	}
	main := job(t, doc, NPMPackageJobKey)
	if got := needsOf(t, main); !reflect.DeepEqual(got, append([]string{WaitForCIJobKey}, platformKeys...)) {
		t.Fatalf("the main package job needs %v", got)
	}
	run := stepRuns(t, main)
	for _, want := range []string{"cd 'npm'", `"portal-darwin-arm64","portal-darwin-x64","portal-linux-arm64","portal-linux-x64"`, `"preinstall", "install", "postinstall"`} {
		if !strings.Contains(run, want) {
			t.Fatalf("the main package job does not carry %q:\n%s", want, run)
		}
	}
	for _, key := range keysOf(jobsOf(t, doc)) {
		if strings.Contains(key, "win32") {
			t.Fatalf("a win32 job exists: %s", key)
		}
	}
	// Whether a version is published is read from the package document's
	// version list, never from a lookup naming the version.
	if strings.Contains(text, `npm view "${`) || strings.Contains(text, "@${VERSION}\" version") || strings.Contains(text, `@$VERSION" version`) {
		t.Fatalf("a job asks npm about one version:\n%s", text)
	}
}

func TestTheNPMJobsCarryProvenanceAndTheRepositoryOnlyWhenGiven(t *testing.T) {
	hygiene.Isolate(t)
	text, err := NPMPackagingJobs(NPMPackaging{GoBinaryRelease: portalBinary(), Package: "portal", Dir: ".", License: "Apache-2.0", RepositoryURL: "git+https://github.com/acme/portal.git", Provenance: true, Actions: testActions})
	if err != nil {
		t.Fatal(err)
	}
	doc := parseYAML(t, "jobs:\n"+text)
	j := job(t, doc, "npm-linux-x64")
	if j["permissions"].(map[string]any)["id-token"] != "write" {
		t.Fatalf("provenance without an OIDC token: %v", j["permissions"])
	}
	run := stepRuns(t, j)
	if !strings.Contains(run, "--provenance") || !strings.Contains(run, `"url": "git+https://github.com/acme/portal.git"`) {
		t.Fatalf("got:\n%s", run)
	}
}

func TestTheNPMJobsRefuseWhatTheyCannotPublish(t *testing.T) {
	hygiene.Isolate(t)
	good := NPMPackaging{GoBinaryRelease: portalBinary(), Package: "portal", Dir: "npm", License: "MIT", Actions: testActions}
	for name, mutate := range map[string]func(n *NPMPackaging){
		"a scoped package":       func(n *NPMPackaging) { n.Package = "@acme/portal" },
		"no license":             func(n *NPMPackaging) { n.License = "" },
		"a directory outside":    func(n *NPMPackaging) { n.Dir = "../elsewhere" },
		"no archive job":         func(n *NPMPackaging) { n.BinaryJob = "" },
		"an empty tag scheme":    func(n *NPMPackaging) { n.Tag = TagParts{} },
		"an unpinned setup-node": func(n *NPMPackaging) { n.Actions = ActionVersions{"actions/checkout": "v6"} },
	} {
		n := good
		mutate(&n)
		if _, err := NPMPackagingJobs(n); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestTheWheelJobAssemblesOneWheelPerPlatform(t *testing.T) {
	hygiene.Isolate(t)
	text, err := WheelJob(WheelPackaging{GoBinaryRelease: GoBinaryRelease{BinaryJob: "goreleaser", Binary: "portal", Tag: TagParts{Prefix: "portal@v"}}, Dir: ".", Actions: testActions})
	if err != nil {
		t.Fatal(err)
	}
	doc := parseYAML(t, "jobs:\n"+text)
	j := job(t, doc, WheelJobKey)
	if j["permissions"].(map[string]any)["id-token"] != "write" {
		t.Fatalf("Trusted Publishing needs an OIDC token: %v", j["permissions"])
	}
	run := stepRuns(t, j)
	for _, p := range pipelines.Platforms() {
		for _, want := range []string{
			pipelines.ArchiveName("portal", "${VERSION}", p),
			pipelines.WheelName("${dist}", "${VERSION}", p),
			"Tag: py3-none-" + strings.Split(p.WheelTag, ".")[0],
		} {
			if !strings.Contains(run, want) {
				t.Fatalf("the wheel job does not carry %q:\n%s", want, run)
			}
		}
	}
	for _, want := range []string{".data/scripts/portal", "sha256sum", "base64", "zip -q -X -D -r", "VERSION=\"${RELEASE_TAG#'portal@v'}\""} {
		if !strings.Contains(run, want) {
			t.Fatalf("the wheel job does not carry %q:\n%s", want, run)
		}
	}
	steps := j["steps"].([]any)
	publish := steps[len(steps)-1].(map[string]any)["with"].(map[string]any)
	if publish["packages-dir"] != "dist/" || publish["attestations"] != false {
		t.Fatalf("the publish step: %v", publish)
	}

	text, err = WheelJob(WheelPackaging{GoBinaryRelease: portalBinary(), Dir: "py", Attestations: true, Actions: testActions})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(text, "attestations: false") || !strings.Contains(text, "'py/pyproject.toml'") {
		t.Fatalf("got:\n%s", text)
	}
}

// The RECORD the wheel job writes holds each file's urlsafe-base64 SHA-256
// digest without padding and its size, as the wheel format requires.
func TestTheWheelRecordCarriesEachFilesDigestAndSize(t *testing.T) {
	hygiene.Isolate(t)
	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Fatalf("the wheel job's shell is bash: %v", err)
	}
	tree := filepath.Join(t.TempDir(), "tree")
	files := map[string]string{
		"portal-1.2.3.data/scripts/portal": "\x00\x01binary\xff",
		"portal-1.2.3.dist-info/METADATA":  "Metadata-Version: 2.4\nName: portal\nVersion: 1.2.3\n",
		"portal-1.2.3.dist-info/WHEEL":     "Wheel-Version: 1.0\n",
	}
	for rel, content := range files {
		testsupport.WriteFile(t, filepath.Join(tree, rel), content)
	}
	script := wheelRecordFunction + `write_record "$1" portal-1.2.3.dist-info` + "\n"
	cmd := exec.Command(bash, "-c", script, "bash", tree)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("write_record: %v\n%s", err, out)
	}
	data, err := os.ReadFile(filepath.Join(tree, "portal-1.2.3.dist-info", "RECORD"))
	if err != nil {
		t.Fatal(err)
	}
	var want []string
	for _, rel := range []string{"portal-1.2.3.data/scripts/portal", "portal-1.2.3.dist-info/METADATA", "portal-1.2.3.dist-info/WHEEL"} {
		sum := sha256.Sum256([]byte(files[rel]))
		want = append(want, fmt.Sprintf("%s,sha256=%s,%d", rel, base64.RawURLEncoding.EncodeToString(sum[:]), len(files[rel])))
	}
	want = append(want, "portal-1.2.3.dist-info/RECORD,,")
	if got := strings.Split(strings.TrimRight(string(data), "\n"), "\n"); !reflect.DeepEqual(got, want) {
		t.Fatalf("got:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}
