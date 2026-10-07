package scaffold

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
	"gopkg.in/yaml.v3"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/workflows"
)

var (
	public       = Features{BuildAttestations: true, GoProxyNotification: true, RepositoryURLs: true}
	confidential = Features{}
	standardTag  = workflows.TagParts{Prefix: "v"}
)

func goBinary() PublishTarget {
	return PublishTarget{Pipeline: declarations.Pipeline{Name: "go", Type: "go", Target: "go", Artifact: "binary"}, Dir: ".", ModulePath: "github.com/acme/portal", BinaryName: "portal", License: "MIT", Tag: standardTag}
}

func npmGoBinary() PublishTarget {
	return PublishTarget{Pipeline: declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "go-binary", BinaryPipeline: "go"}, Dir: "npm", PackageName: "portal", BinaryName: "portal", License: "MIT", Tag: standardTag}
}

func pypiGoBinary() PublishTarget {
	return PublishTarget{Pipeline: declarations.Pipeline{Name: "pypi", Type: "pypi", Target: "pypi", Artifact: "go-binary", BinaryPipeline: "go"}, Dir: "python", BinaryName: "portal", License: "MIT", Tag: standardTag}
}

// workflow assembles a publish workflow and checks it parses as YAML.
func workflow(t *testing.T, f Features, targets ...PublishTarget) (string, map[string]any) {
	t.Helper()
	pattern, err := workflows.CheckPatternForTargets([]string{"go", "npm"})
	if err != nil {
		t.Fatal(err)
	}
	wait, err := workflows.WaitForCIJob(pattern)
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

func TestAPublishWorkflowWaitsForCIBeforeEveryJob(t *testing.T) {
	hygiene.Isolate(t)
	text, doc := workflow(t, public, goBinary(), npmGoBinary(), pypiGoBinary())
	jobs := doc["jobs"].(map[string]any)
	if _, ok := jobs[workflows.WaitForCIJobKey]; !ok {
		t.Fatalf("no wait-for-ci job:\n%s", text)
	}
	if needs := jobs["go"].(map[string]any)["needs"]; needs != workflows.WaitForCIJobKey {
		t.Fatalf("the go job needs %v", needs)
	}
	for key, job := range jobs {
		if key == workflows.WaitForCIJobKey {
			continue
		}
		needs := job.(map[string]any)["needs"]
		ok := needs == workflows.WaitForCIJobKey
		if list, isList := needs.([]any); isList && len(list) > 0 && list[0] == workflows.WaitForCIJobKey {
			ok = true
		}
		if !ok {
			t.Errorf("the %s job does not wait for CI: needs %v", key, needs)
		}
	}
	perms := doc["permissions"].(map[string]any)
	if perms["contents"] != "write" || perms["id-token"] != "write" {
		t.Fatalf("permissions = %v", perms)
	}
	if strings.Contains(text, "win32") || strings.Contains(text, ".exe") {
		t.Fatalf("a windows platform is rendered:\n%s", text)
	}
}

func TestAConfidentialRepositoryPublishesNothingNamingIt(t *testing.T) {
	hygiene.Isolate(t)
	npmPackage := PublishTarget{Pipeline: declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "package"}, Dir: ".", RegistryURL: "https://registry.npmjs.org", PackageManager: "npm"}
	pypiPackage := PublishTarget{Pipeline: declarations.Pipeline{Name: "pypi", Type: "pypi", Target: "pypi", Artifact: "package"}, Dir: "."}
	wheels := pypiGoBinary()
	wheels.Pipeline.Name = "wheels"
	text, _ := workflow(t, confidential, npmPackage, pypiPackage, goBinary(), wheels)
	if strings.Contains(text, "--provenance") {
		t.Fatalf("a confidential repository's npm publish records provenance:\n%s", text)
	}
	if strings.Count(text, "attestations: false") != 2 {
		t.Fatalf("a confidential repository's PyPI publishes attest:\n%s", text)
	}
	if strings.Contains(text, "proxy.golang.org") || strings.Contains(text, "homepage") || strings.Contains(text, `"repository"`) {
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
	text, _ := workflow(t, public, PublishTarget{Pipeline: declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "package"}, Dir: ".", RegistryURL: "https://registry.npmjs.org", PackageManager: "pnpm"})
	if strings.Contains(text, `@${PKG_VERSION}" version`) {
		t.Fatalf("a version-specific registry read:\n%s", text)
	}
	if !strings.Contains(text, "pnpm/action-setup@") || !strings.Contains(text, "pnpm publish") {
		t.Fatalf("pnpm is not used:\n%s", text)
	}
}

func TestTwoJobsWithOneKeyAreRefused(t *testing.T) {
	hygiene.Isolate(t)
	job := PublishJob{Keys: []string{"go"}, Text: "  go:\n    runs-on: ubuntu-latest", Permissions: map[string]string{"contents": "read"}}
	if _, err := PublishWorkflow("  wait-for-ci:\n    runs-on: ubuntu-latest", []PublishJob{job, job}); err == nil {
		t.Fatal("a duplicate job key was accepted")
	}
	clash := PublishJob{Keys: []string{workflows.WaitForCIJobKey}, Text: "  wait-for-ci:\n    runs-on: x"}
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
	if again := jobsInDirectory("      go-version-file: tools/portal/go.mod", "tools/portal"); again != "      go-version-file: tools/portal/go.mod" {
		t.Fatalf("the version file was prefixed twice: %q", again)
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
