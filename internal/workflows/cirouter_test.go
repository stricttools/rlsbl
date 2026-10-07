package workflows

import (
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

var routerActions = ActionVersions{"actions/checkout": "v6", "dorny/paths-filter": "v4"}

const coreCI = `name: CI
on:
  push:
    branches: [main]
permissions:
  contents: read
env:
  GOFLAGS: -mod=readonly
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-go@v6
        with:
          go-version-file: go.mod
      - run: go test ./...
  lint:
    needs: test
    if: ${{ github.event_name == 'push' }}
    runs-on: ubuntu-latest
    env:
      GOFLAGS: -mod=mod
    steps:
      - run: go vet ./...
`

const webCI = `name: CI
on: [push]
jobs:
  test:
    name: unit
    runs-on: ubuntu-latest
    steps:
      - run: npm test
`

func routerWorkspace(t *testing.T, extra map[string]string) map[string]string {
	t.Helper()
	files := map[string]string{
		"packages/core/.github/workflows/ci.yml":   coreCI,
		"apps/web/.github/workflows/ci-npm.yml":    webCI,
		"apps/web/.github/workflows/publish.yml":   "jobs: {}\n",
		"apps/web/.github/workflows/ci-router.yml": "jobs: {}\n",
		"apps/web/.github/workflows/ci-old.yml":    Header + "\njobs: {}\n",
	}
	for k, v := range extra {
		files[k] = v
	}
	return files
}

func TestTheRouterInlinesEveryMembersCIJobs(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, threeMembers, routerWorkspace(t, nil))
	text, ok, err := CIRouter(w, RouterInputs{Actions: routerActions})
	if err != nil || !ok {
		t.Fatalf("ok %v, err %v", ok, err)
	}
	if !IsGenerated(text) {
		t.Fatalf("the router does not start with the header:\n%s", text)
	}
	doc := parseYAML(t, text)
	if !reflect.DeepEqual(keysSorted(jobsOf(t, doc)), []string{"core-ci-lint", "core-ci-test", "detect", "web-ci-npm-test"}) {
		t.Fatalf("jobs: %v", keysSorted(jobsOf(t, doc)))
	}
	on := doc["on"].(map[string]any)
	if !reflect.DeepEqual(on["push"].(map[string]any)["branches"], []any{"main"}) {
		t.Fatalf("push branches: %v", on["push"])
	}
	input := on["workflow_dispatch"].(map[string]any)["inputs"].(map[string]any)[RunAllInput].(map[string]any)
	if input["type"] != "boolean" || input["default"] != false {
		t.Fatalf("run_all input: %v", input)
	}
	if group := doc["concurrency"].(map[string]any)["group"].(string); !strings.Contains(group, "inputs.run_all") {
		t.Fatalf("concurrency group: %s", group)
	}

	detect := job(t, doc, "detect")
	if _, has := detect["if"]; has {
		t.Fatal("the detect job is conditional")
	}
	outputs := detect["outputs"].(map[string]any)
	if !reflect.DeepEqual(keysSorted(outputs), []string{"core", "web"}) {
		t.Fatalf("detect outputs: %v", outputs)
	}
	with := detect["steps"].([]any)[1].(map[string]any)["with"].(map[string]any)
	if with["predicate-quantifier"] != PredicateQuantifier || !strings.Contains(with["filters"].(string), "core:\n  - 'packages/core/**'") {
		t.Fatalf("paths filter: %v", with)
	}

	test := job(t, doc, "core-ci-test")
	if test["name"] != "core-ci / test" || !reflect.DeepEqual(test["needs"], []any{"detect"}) {
		t.Fatalf("core-ci-test: %v", test)
	}
	if test["if"] != "(needs.detect.outputs['core'] == 'true' || inputs.run_all)" {
		t.Fatalf("if: %v", test["if"])
	}
	if test["defaults"].(map[string]any)["run"].(map[string]any)["working-directory"] != "packages/core" {
		t.Fatalf("defaults: %v", test["defaults"])
	}
	if test["env"].(map[string]any)["GOFLAGS"] != "-mod=readonly" {
		t.Fatalf("env: %v", test["env"])
	}
	setup := test["steps"].([]any)[1].(map[string]any)["with"].(map[string]any)
	if setup["go-version-file"] != "packages/core/go.mod" {
		t.Fatalf("setup-go: %v", setup)
	}

	lint := job(t, doc, "core-ci-lint")
	if !reflect.DeepEqual(lint["needs"], []any{"detect", "core-ci-test"}) {
		t.Fatalf("needs: %v", lint["needs"])
	}
	if lint["if"] != "(needs.detect.outputs['core'] == 'true' || inputs.run_all) && (github.event_name == 'push')" {
		t.Fatalf("if: %v", lint["if"])
	}
	if lint["env"].(map[string]any)["GOFLAGS"] != "-mod=mod" {
		t.Fatalf("the job's own env lost to the workflow's: %v", lint["env"])
	}
	if job(t, doc, "web-ci-npm-test")["name"] != "web-ci-npm / unit" {
		t.Fatalf("web job name: %v", job(t, doc, "web-ci-npm-test")["name"])
	}
}

// The Python router dropped a member workflow's top-level permissions, so an
// inlined job ran with the router's default token permissions instead of
// the ones its workflow declared.
func TestAnInlinedJobKeepsItsWorkflowsPermissions(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, threeMembers, routerWorkspace(t, nil))
	text, _, err := CIRouter(w, RouterInputs{Actions: routerActions})
	if err != nil {
		t.Fatal(err)
	}
	perms := job(t, parseYAML(t, text), "core-ci-test")["permissions"]
	if !reflect.DeepEqual(perms, map[string]any{"contents": "read"}) {
		t.Fatalf("permissions: %v", perms)
	}
}

func TestTheCheckPatternNamesTheInlinedJobs(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, threeMembers, routerWorkspace(t, map[string]string{"apps/web/.github/workflows/ci-go.yml": webCI}))
	pattern, ok, err := CheckPattern("web", w.MemberDir(member(t, w, "web")))
	if err != nil || !ok {
		t.Fatalf("ok %v, err %v", ok, err)
	}
	if pattern != `^(web-ci-go|web-ci-npm) / ` {
		t.Fatalf("got %s", pattern)
	}
	re := regexp.MustCompile(pattern)
	if !re.MatchString("web-ci-npm / unit") || re.MatchString("web-ci-npmx / unit") {
		t.Fatalf("the pattern %s does not match the check runs", pattern)
	}
	if _, ok, err := CheckPattern("root", w.Root); err != nil || ok {
		t.Fatalf("a member with no CI of its own has a pattern: ok %v, err %v", ok, err)
	}
}

func TestTheRouterRefusesWhatItCannotInline(t *testing.T) {
	hygiene.Isolate(t)
	for name, extra := range map[string]map[string]string{
		"a placeholder": {"packages/core/.github/workflows/ci.yml": "jobs:\n  test:\n    runs-on: {{runner}}\n"},
		"a conflict":    {"packages/core/.github/workflows/ci.yml": "jobs:\n<<<<<<< ours\n  a: {}\n=======\n  b: {}\n>>>>>>> theirs\n"},
		"no jobs":       {"packages/core/.github/workflows/ci.yml": "name: CI\n"},
		"an alias":      {"packages/core/.github/workflows/ci.yml": "jobs:\n  a: &x\n    runs-on: ubuntu-latest\n  b: *x\n"},
		"a collision":   {"packages/core/.github/workflows/ci-x.yml": "jobs:\n  y: {runs-on: ubuntu-latest}\n", "packages/core/.github/workflows/ci.yml": "jobs:\n  x-y: {runs-on: ubuntu-latest}\n"},
	} {
		w := fixtureWorkspace(t, threeMembers, routerWorkspace(t, extra))
		if _, _, err := CIRouter(w, RouterInputs{Actions: routerActions}); err == nil {
			t.Errorf("%s was inlined", name)
		}
	}
}

func TestNoMemberWithCIRoutesNothing(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, threeMembers, nil)
	if _, ok, err := CIRouter(w, RouterInputs{Actions: routerActions}); err != nil || ok {
		t.Fatalf("ok %v, err %v", ok, err)
	}
}

func keysSorted(m map[string]any) []string {
	keys := keysOf(m)
	sort.Strings(keys)
	return keys
}
