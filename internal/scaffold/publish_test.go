package scaffold

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
	"gopkg.in/yaml.v3"

	"github.com/stricttools/rlsbl/internal/declarations"
)

var (
	public       = Features{BuildAttestations: true, GoProxyNotification: true, RepositoryURLs: true}
	confidential = Features{}
)

func goBinary() PublishTarget {
	return PublishTarget{Pipeline: declarations.Pipeline{Name: "go", Type: "go", Target: "go", Artifact: "binary"}, Dir: ".", ModulePath: "github.com/acme/portal", BinaryName: "portal", License: "MIT"}
}

func npmGoBinary() PublishTarget {
	return PublishTarget{Pipeline: declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "go-binary", BinaryPipeline: "go"}, Dir: "npm", BinaryName: "portal", License: "MIT"}
}

func pypiGoBinary() PublishTarget {
	return PublishTarget{Pipeline: declarations.Pipeline{Name: "pypi", Type: "pypi", Target: "pypi", Artifact: "go-binary", BinaryPipeline: "go"}, Dir: "python", BinaryName: "portal", License: "MIT"}
}

// workflow assembles a publish workflow and checks it parses as YAML.
func workflow(t *testing.T, f Features, targets ...PublishTarget) (string, map[string]any) {
	t.Helper()
	pattern, err := CICheckPattern([]string{"go", "npm"})
	if err != nil {
		t.Fatal(err)
	}
	wait, err := WaitForCIJob(WaitForCI{CheckPattern: pattern})
	if err != nil {
		t.Fatal(err)
	}
	var jobs []PublishJob
	for _, target := range targets {
		job, err := RenderPublishJob(target, f)
		if err != nil {
			t.Fatal(err)
		}
		jobs = append(jobs, job)
	}
	text, err := PublishWorkflow(wait, jobs)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := yaml.Unmarshal([]byte(text), &doc); err != nil {
		t.Fatalf("the workflow is not YAML: %v\n%s", err, text)
	}
	return text, doc
}

func TestTheCheckPatternMatchesTheCIJobsAndTheirMatrices(t *testing.T) {
	hygiene.Isolate(t)
	pattern, err := CICheckPattern([]string{"npm", "go", "pypi"})
	if err != nil || pattern != `^(test)( \(.*\))?$` {
		t.Fatalf("pattern = %q, %v", pattern, err)
	}
	if _, err := CICheckPattern([]string{"zig"}); err == nil {
		t.Fatal("a target without CI was accepted")
	}
}

func TestAPublishWorkflowWaitsForCIBeforeEveryJob(t *testing.T) {
	hygiene.Isolate(t)
	text, doc := workflow(t, public, goBinary(), npmGoBinary(), pypiGoBinary())
	jobs := doc["jobs"].(map[string]any)
	if _, ok := jobs[WaitForCIJobKey]; !ok {
		t.Fatalf("no wait-for-ci job:\n%s", text)
	}
	if needs := jobs["go"].(map[string]any)["needs"]; needs != WaitForCIJobKey {
		t.Fatalf("the go job needs %v", needs)
	}
	needs := jobs["npm"].(map[string]any)["needs"].([]any)
	if len(needs) != 2 || needs[0] != WaitForCIJobKey || needs[1] != "go" {
		t.Fatalf("the npm job needs %v", needs)
	}
	perms := doc["permissions"].(map[string]any)
	if perms["contents"] != "write" || perms["id-token"] != "write" {
		t.Fatalf("permissions = %v", perms)
	}
	if len(jobs) != 4 {
		t.Fatalf("jobs = %v", jobs)
	}
	if !strings.Contains(text, `CI_CHECK_REGEX: '^(test)( \(.*\))?$'`) {
		t.Fatalf("the check-run pattern is not in the job:\n%s", text)
	}
}

func TestGoBinaryJobsCoverThePlatformTableAndNoWindows(t *testing.T) {
	hygiene.Isolate(t)
	text, doc := workflow(t, public, goBinary(), npmGoBinary(), pypiGoBinary())
	for _, platform := range []string{"linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64"} {
		if !strings.Contains(text, "platform_package "+platform+" ") {
			t.Errorf("no platform package for %s", platform)
		}
	}
	for _, tag := range []string{"manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64", "macosx_11_0_arm64"} {
		if !strings.Contains(text, "wheel "+tag+" ") {
			t.Errorf("no wheel for %s", tag)
		}
	}
	if strings.Contains(text, "win32") || strings.Contains(text, ".exe") {
		t.Fatalf("a windows platform is rendered:\n%s", text)
	}
	npm := doc["jobs"].(map[string]any)["npm"].(map[string]any)
	if dir := npm["defaults"].(map[string]any)["run"].(map[string]any)["working-directory"]; dir != "npm" {
		t.Fatalf("the npm job runs in %v", dir)
	}
	if !strings.Contains(text, "--provenance") || !strings.Contains(text, `"MIT"`) {
		t.Fatalf("a public repository's packages are published without provenance or the license:\n%s", text)
	}
	if strings.Contains(text, "postinstall") {
		t.Fatal("the platform packages run something at install time")
	}
}

func TestAConfidentialRepositoryPublishesNothingNamingIt(t *testing.T) {
	hygiene.Isolate(t)
	npmPackage := PublishTarget{Pipeline: declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "package"}, Dir: ".", RegistryURL: "https://registry.npmjs.org", PackageManager: "npm"}
	pypiPackage := PublishTarget{Pipeline: declarations.Pipeline{Name: "pypi", Type: "pypi", Target: "pypi", Artifact: "package"}, Dir: "."}
	text, _ := workflow(t, confidential, npmPackage, pypiPackage, goBinary(), npmGoBinary())
	if strings.Contains(text, "--provenance") {
		t.Fatalf("a confidential repository's npm publish records provenance:\n%s", text)
	}
	if !strings.Contains(text, "attestations: false") {
		t.Fatalf("a confidential repository's PyPI publish attests:\n%s", text)
	}
	if strings.Contains(text, "proxy.golang.org") || strings.Contains(text, "homepage") || strings.Contains(text, "repository:") {
		t.Fatalf("a confidential repository's workflow names it or asks the proxy:\n%s", text)
	}
	library := PublishTarget{Pipeline: declarations.Pipeline{Name: "go", Type: "go", Target: "go", Artifact: "library"}, Dir: ".", ModulePath: "github.com/acme/portal"}
	if _, err := RenderPublishJob(library, confidential); err == nil || !strings.Contains(err.Error(), "private-repository-publishing") {
		t.Fatalf("a go library in a confidential repository: %v", err)
	}
	tap := goBinary()
	tap.HomebrewTap = true
	if _, err := RenderPublishJob(tap, confidential); err == nil || !strings.Contains(err.Error(), "homebrew_tap") {
		t.Fatalf("a Homebrew tap in a confidential repository: %v", err)
	}
	publicText, _ := workflow(t, public, npmPackage, pypiPackage, library)
	if !strings.Contains(publicText, "--provenance") || strings.Contains(publicText, "attestations: false") || !strings.Contains(publicText, "GOPROXY=proxy.golang.org go list -m") {
		t.Fatalf("a public repository's workflow:\n%s", publicText)
	}
}

func TestAnNpmPublishNeverAsksForOneVersion(t *testing.T) {
	hygiene.Isolate(t)
	text, _ := workflow(t, public, PublishTarget{Pipeline: declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "package"}, Dir: ".", RegistryURL: "https://registry.npmjs.org", PackageManager: "pnpm"}, npmGoBinary(), goBinary())
	if strings.Contains(text, `@${PKG_VERSION}" version`) || strings.Contains(text, `@${VERSION}" version`) {
		t.Fatalf("a version-specific registry read:\n%s", text)
	}
	if !strings.Contains(text, "pnpm/action-setup@") || !strings.Contains(text, "pnpm publish") {
		t.Fatalf("pnpm is not used:\n%s", text)
	}
}

func TestTwoJobsWithOneKeyAreRefused(t *testing.T) {
	hygiene.Isolate(t)
	job := PublishJob{Key: "go", Text: "  go:\n    runs-on: ubuntu-latest", Permissions: map[string]string{"contents": "read"}}
	if _, err := PublishWorkflow("  wait-for-ci:\n    runs-on: ubuntu-latest", []PublishJob{job, job}); err == nil {
		t.Fatal("a duplicate job key was accepted")
	}
	clash := PublishJob{Key: WaitForCIJobKey, Text: "  wait-for-ci:\n    runs-on: x"}
	if _, err := PublishWorkflow("  wait-for-ci:\n    runs-on: ubuntu-latest", []PublishJob{clash}); err == nil {
		t.Fatal("a pipeline named like the wait job was accepted")
	}
}

func TestJobsInADirectoryRunThereAndReadTheirVersionFileThere(t *testing.T) {
	hygiene.Isolate(t)
	ci := "name: CI\non:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/setup-go@v6\n        with:\n          go-version-file: go.mod\n"
	got := workflowInDirectory(ci, "tools/portal")
	want := "name: CI\non:\n  push:\n    branches: [main]\njobs:\n  test:\n    defaults:\n      run:\n        working-directory: tools/portal\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/setup-go@v6\n        with:\n          go-version-file: tools/portal/go.mod\n"
	if got != want {
		t.Fatalf("got:\n%s\nwant:\n%s", got, want)
	}
	if again := workflowInDirectory(want, "tools/portal"); !strings.Contains(again, "go-version-file: tools/portal/go.mod\n") {
		t.Fatalf("the version file was prefixed twice:\n%s", again)
	}
}

func TestConflictRegionsAreNamedByLine(t *testing.T) {
	hygiene.Isolate(t)
	text := "a\n<<<<<<< ours\nb\n=======\nc\n>>>>>>> theirs\nd\n<<<<<<< ours\ne\n"
	regions := ConflictRegions(text)
	if len(regions) != 2 || regions[0] != (ConflictRegion{2, 6}) || regions[1] != (ConflictRegion{8, 9}) {
		t.Fatalf("regions = %v", regions)
	}
	if got := DescribeConflicts("ci.yml", regions); got != "ci.yml: lines 2-6, lines 8-9" {
		t.Fatalf("described as %q", got)
	}
	if len(ConflictRegions("clean\n")) != 0 {
		t.Fatal("a clean file has conflicts")
	}
}
