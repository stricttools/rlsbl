package workflows

import (
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/pipelines"
)

const memberPublish = `name: Publish
on:
  release:
    types: [published]
permissions:
  contents: read
  id-token: write
jobs:
  wait-for-ci:
    runs-on: ubuntu-latest
    steps:
      - run: echo waiting
  publish:
    needs: wait-for-ci
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: pypa/gh-action-pypi-publish@release/v1
  announce:
    needs: [wait-for-ci, publish]
    if: success()
    runs-on: ubuntu-latest
    steps:
      - run: echo done
`

// withTools adds a releasable publishing nothing, whose member keeps a
// publish workflow from before.
const withTools = threeMembers + `
[[releasables]]
name = "tools"
tag_format = "{name}@v{version}"
publish_mode = "none"

[[members]]
path = "tools"
name = "tools"
releasable = "tools"
`

func publishFixture(extra map[string]string) map[string]string {
	files := map[string]string{
		"packages/core/.github/workflows/ci.yml":      coreCI,
		"packages/core/.github/workflows/publish.yml": memberPublish,
		"apps/web/.github/workflows/ci.yml":           webCI,
		"tools/.github/workflows/publish.yml":         memberPublish,
	}
	for k, v := range extra {
		files[k] = v
	}
	return files
}

func TestThePublishRouterInlinesEachPublishingMembersJobs(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, withTools, publishFixture(nil))
	plan, err := PublishRouter(w, PublishInputs{})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(plan.Suppressed, []string{"tools/.github/workflows/publish.yml"}) {
		t.Fatalf("suppressed: %v", plan.Suppressed)
	}
	if !IsGenerated(plan.Text) {
		t.Fatalf("no header:\n%s", plan.Text)
	}
	doc := parseYAML(t, plan.Text)
	if !reflect.DeepEqual(keysSorted(jobsOf(t, doc)), []string{"core-announce", "core-publish", WaitForCIJobKey}) {
		t.Fatalf("jobs: %v", keysSorted(jobsOf(t, doc)))
	}
	if doc["concurrency"].(map[string]any)["cancel-in-progress"] != false {
		t.Fatalf("concurrency: %v", doc["concurrency"])
	}
	resolver := job(t, doc, WaitForCIJobKey)["steps"].([]any)[0].(map[string]any)["run"].(string)
	if !strings.Contains(resolver, `grep -Eq '^core@v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'`) || !strings.Contains(resolver, "scheme='core@v{version}'") || !strings.Contains(resolver, "pattern='^(core-ci) / '") {
		t.Fatalf("resolver:\n%s", resolver)
	}
	if outputs := job(t, doc, WaitForCIJobKey)["outputs"]; !reflect.DeepEqual(outputs, map[string]any{RouterTagSchemeOutput: "${{ steps.resolve.outputs.tag_scheme }}"}) {
		t.Fatalf("outputs: %v", outputs)
	}
	publish := job(t, doc, "core-publish")
	if publish["if"] != "needs.wait-for-ci.outputs.tag_scheme == 'core@v{version}'" {
		t.Fatalf("if: %v", publish["if"])
	}
	if !reflect.DeepEqual(publish["needs"], []any{WaitForCIJobKey}) {
		t.Fatalf("needs: %v", publish["needs"])
	}
	if !reflect.DeepEqual(publish["permissions"], map[string]any{"contents": "read", "id-token": "write"}) {
		t.Fatalf("permissions: %v", publish["permissions"])
	}
	if publish["defaults"].(map[string]any)["run"].(map[string]any)["working-directory"] != "packages/core" {
		t.Fatalf("defaults: %v", publish["defaults"])
	}
	with := publish["steps"].([]any)[1].(map[string]any)["with"].(map[string]any)
	if with["packages-dir"] != "packages/core/dist/" {
		t.Fatalf("packages-dir: %v", with)
	}
	announce := job(t, doc, "core-announce")
	if !reflect.DeepEqual(announce["needs"], []any{WaitForCIJobKey, "core-publish"}) {
		t.Fatalf("needs: %v", announce["needs"])
	}
	if announce["if"] != "needs.wait-for-ci.outputs.tag_scheme == 'core@v{version}' && (success())" {
		t.Fatalf("if: %v", announce["if"])
	}
}

func TestEachJobRunsOnlyForTheSchemeTheWaitForCIJobJudgedTheTagOf(t *testing.T) {
	hygiene.Isolate(t)
	decls := strings.NewReplacer(`name = "core"
tag_format = "{name}@v{version}"`, `name = "core"
tag_format = "kernel/v{version}"`, `name = "web"
tag_format = "{name}@v{version}"`, `name = "web"
tag_format = "kernel/vulkan/v{version}"`).Replace(threeMembers)
	w := fixtureWorkspace(t, decls, publishFixture(map[string]string{"apps/web/.github/workflows/publish.yml": memberPublish}))
	plan, err := PublishRouter(w, PublishInputs{})
	if err != nil {
		t.Fatal(err)
	}
	doc := parseYAML(t, plan.Text)
	if got := job(t, doc, "core-publish")["if"]; got != "needs.wait-for-ci.outputs.tag_scheme == 'kernel/v{version}'" {
		t.Fatalf("if: %v", got)
	}
	if got := job(t, doc, "web-publish")["if"]; got != "needs.wait-for-ci.outputs.tag_scheme == 'kernel/vulkan/v{version}'" {
		t.Fatalf("if: %v", got)
	}
}

func TestThePublishRouterRefusesWhatCannotPublish(t *testing.T) {
	hygiene.Isolate(t)
	for name, extra := range map[string]map[string]string{
		"the old waiting job": {"packages/core/.github/workflows/publish.yml": strings.Replace(memberPublish, "wait-for-ci", RetiredWaitJobKey, -1)},
		"no CI of its own":    {"packages/core/.github/workflows/ci.yml": Header + "\njobs: {}\n"},
	} {
		w := fixtureWorkspace(t, withTools, publishFixture(extra))
		if _, err := PublishRouter(w, PublishInputs{}); err == nil {
			t.Errorf("%s was published", name)
		}
	}
}

const rootPublisher = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "ci"
publish_ci_check_pattern = '^(test)( \(.*\))?$'

[[members]]
path = "."
name = "root"
releasable = "portal"
targets = [{ name = "npm" }]

[[members.pipelines]]
name = "npm"
type = "npm"
target = "npm"
local = false
artifact = "package"
`

func TestTheRootMembersJobsAreRenderedNotRead(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, rootPublisher, map[string]string{"package.json": `{"name": "portal", "version": "1.0.0"}`, PublishPath: Header + "\njobs: {}\n"})
	if _, err := PublishRouter(w, PublishInputs{}); err == nil {
		t.Fatal("the root member's publish jobs were taken from nowhere")
	}
	var rendered []string
	plan, err := PublishRouter(w, PublishInputs{RootPublishWorkflow: func(m declarations.Member) (string, error) {
		rendered = append(rendered, m.Name)
		return memberPublish, nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(rendered, []string{"root"}) {
		t.Fatalf("rendered for %v", rendered)
	}
	doc := parseYAML(t, plan.Text)
	resolver := job(t, doc, WaitForCIJobKey)["steps"].([]any)[0].(map[string]any)["run"].(string)
	if !strings.Contains(resolver, `pattern='^(test)( \(.*\))?$'`) {
		t.Fatalf("resolver:\n%s", resolver)
	}
	if job(t, doc, "root-publish")["defaults"].(map[string]any)["run"].(map[string]any)["working-directory"] != "." {
		t.Fatalf("root-publish: %v", job(t, doc, "root-publish"))
	}
}

// goBinaryPublish is the publish workflow of a go-binary member: its
// goreleaser job, the npm per-platform and package jobs, and the wheel job.
func goBinaryPublish(t *testing.T) string {
	t.Helper()
	npm, err := NPMPackagingJobs(NPMPackaging{GoBinaryRelease: portalBinary(), Package: "portal", Dir: "npm", License: "MIT", Actions: testActions})
	if err != nil {
		t.Fatal(err)
	}
	wheel, err := WheelJob(WheelPackaging{GoBinaryRelease: portalBinary(), Dir: ".", Actions: testActions})
	if err != nil {
		t.Fatal(err)
	}
	return `name: Publish
on:
  release:
    types: [published]
permissions:
  contents: write
jobs:
  wait-for-ci:
    runs-on: ubuntu-latest
    steps:
      - run: echo waiting
  goreleaser:
    needs: wait-for-ci
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: goreleaser release --clean
` + npm + wheel
}

func TestEveryInlinedJobGivenAWorkingDirectoryChecksTheRepositoryOut(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, withTools, publishFixture(map[string]string{"packages/core/.github/workflows/publish.yml": goBinaryPublish(t)}))
	plan, err := PublishRouter(w, PublishInputs{})
	if err != nil {
		t.Fatal(err)
	}
	doc := parseYAML(t, plan.Text)
	inlinedPlatformJobs := 0
	for key, raw := range jobsOf(t, doc) {
		j := raw.(map[string]any)
		if strings.HasPrefix(key, "core-npm-") && key != "core-"+NPMPackageJobKey {
			inlinedPlatformJobs++
		}
		defaults, _ := j["defaults"].(map[string]any)
		run, _ := defaults["run"].(map[string]any)
		dir, _ := run["working-directory"].(string)
		if dir == "" {
			continue
		}
		steps, _ := j["steps"].([]any)
		checkedOut := false
		for _, s := range steps {
			if uses, _ := s.(map[string]any)["uses"].(string); strings.HasPrefix(uses, "actions/checkout@") {
				checkedOut = true
			}
		}
		if !checkedOut {
			t.Errorf("the job %s runs in %s and checks no repository out, so that directory does not exist on the runner", key, dir)
		}
	}
	if inlinedPlatformJobs != len(pipelines.Platforms()) {
		t.Fatalf("the router inlined %d npm platform jobs, want %d:\n%s", inlinedPlatformJobs, len(pipelines.Platforms()), plan.Text)
	}
	if job(t, doc, "core-goreleaser")["defaults"] == nil {
		t.Error("a job that checks the repository out lost its working directory under the member")
	}
}
