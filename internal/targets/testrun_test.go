package targets

import (
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// steps renders a plan's steps as "dir: command" lines, dir relative to
// base.
func steps(t *testing.T, base string, plan TestPlan) string {
	t.Helper()
	var lines []string
	for _, s := range plan.Steps {
		rel, err := filepath.Rel(base, s.Dir)
		if err != nil {
			t.Fatal(err)
		}
		lines = append(lines, filepath.ToSlash(rel)+": "+s.String())
	}
	return strings.Join(lines, "\n")
}

func planFor(t *testing.T, name string, in TestInputs) TestPlan {
	t.Helper()
	target, err := Get(name)
	if err != nil {
		t.Fatal(err)
	}
	plan, err := TestPlanOf(target, in)
	if err != nil {
		t.Fatal(err)
	}
	return plan
}

func TestGoRunsGoTestOrTheDeclaredCommand(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	if got := steps(t, dir, planFor(t, Go, TestInputs{Dir: dir})); got != ".: go test ./... -race -short -count=1" {
		t.Errorf("default: %s", got)
	}
	plan := planFor(t, Go, TestInputs{Dir: dir, Settings: declarations.TestSettings{GoCommand: "scripts/go-suite.sh --fast"}})
	if len(plan.Steps) != 1 || plan.Steps[0].Shell != "scripts/go-suite.sh --fast" {
		t.Errorf("declared: %+v", plan)
	}
}

func TestAnNpmPackageWithoutATestScriptHasNothingToRun(t *testing.T) {
	hygiene.Isolate(t)
	dir := project(t, map[string]string{"package.json": "{\"name\": \"portal\"}"})
	if plan := planFor(t, NPM, TestInputs{Dir: dir}); plan.Skip == "" || len(plan.Steps) != 0 {
		t.Errorf("no script: %+v", plan)
	}
	dir = project(t, map[string]string{"package.json": "{\"scripts\": {\"test\": \"node --test\"}}"})
	if got := steps(t, dir, planFor(t, NPM, TestInputs{Dir: dir})); got != ".: npm test" {
		t.Errorf("a script: %s", got)
	}
}

func TestAWorkspaceMemberSyncsAtTheWorkspaceRootKeepingTheOverlays(t *testing.T) {
	hygiene.Isolate(t)
	root := project(t, map[string]string{"pyproject.toml": "[tool.uv.workspace]\nmembers = [\"widget\"]\n", "widget/pyproject.toml": "[project]\nname = \"widget\"\n"})
	member := filepath.Join(root, "widget")
	plan := planFor(t, PyPI, TestInputs{Dir: member, UVWorkspaceRoot: root, Settings: declarations.TestSettings{PyPIMarkers: "not slow"}})
	if got := steps(t, root, plan); got != ".: uv sync --all-packages --quiet\nwidget: uv run python -P -m pytest -m not slow" {
		t.Errorf("plain:\n%s", got)
	}
	plan = planFor(t, PyPI, TestInputs{Dir: member, UVWorkspaceRoot: root, Overlays: []string{"gadget"}})
	if got := steps(t, root, plan); got != ".: uv sync --all-packages --quiet --inexact --no-install-package gadget\nwidget: uv run --no-sync python -P -m pytest" {
		t.Errorf("overlays:\n%s", got)
	}
	if plan := planFor(t, PyPI, TestInputs{Dir: member, UVWorkspaceRoot: root, SkipSync: true}); len(plan.Steps) != 1 {
		t.Errorf("a synced workspace synced again: %+v", plan)
	}
}

func TestAStandaloneProjectRunsPytestWhereItIsDeclared(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ pyproject, want string }{
		{"[project]\nname = \"p\"\n\n[dependency-groups]\ndev = [\"pytest>=8\"]\n", ".: uv run python -P -m pytest"},
		{"[project]\nname = \"p\"\n\n[dependency-groups]\nlint = [\"ruff\"]\ntest = [\"pytest\"]\n", ".: uv run --group test python -P -m pytest"},
		{"[project]\nname = \"p\"\n\n[project.optional-dependencies]\nci = [\"pytest-cov\"]\n", ".: uv run --extra ci python -P -m pytest"},
		{"[project]\nname = \"p\"\n\n[tool.uv]\ndev-dependencies = [\"pytest\"]\n", ".: uv run python -P -m pytest"},
	}
	for _, c := range cases {
		dir := project(t, map[string]string{"pyproject.toml": c.pyproject})
		if got := steps(t, dir, planFor(t, PyPI, TestInputs{Dir: dir})); got != c.want {
			t.Errorf("%q: %s", c.pyproject, got)
		}
	}
	dir := project(t, map[string]string{"pyproject.toml": "[project]\nname = \"p\"\n\n[dependency-groups]\ntest = [\"pytest\"]\n"})
	plan := planFor(t, PyPI, TestInputs{Dir: dir, Overlays: []string{"gadget"}})
	if got := steps(t, dir, plan); got != ".: uv sync --inexact --quiet --group test --no-install-package gadget\n.: uv run --no-sync --group test python -P -m pytest" {
		t.Errorf("overlays:\n%s", got)
	}
	target, _ := Get(PyPI)
	if _, err := TestPlanOf(target, TestInputs{Dir: project(t, map[string]string{"pyproject.toml": "[project]\nname = \"p\"\n"})}); err == nil || !strings.Contains(err.Error(), "pytest is not declared") {
		t.Fatalf("no pytest: %v", err)
	}
}

func TestRunTestsReportsTheFailingStep(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	plan := TestPlan{Steps: []TestStep{{Shell: "exit 0", Dir: dir}, {Shell: "exit 3", Dir: dir}, {Shell: "exit 0", Dir: dir}}}
	var passed bool
	var failure string
	var runErr error
	mutate(t, func(e *strictcli.Effects) error {
		passed, failure, runErr = RunTests(e, plan, time.Minute)
		return nil
	})
	if runErr != nil || passed || failure != "`exit 3` exited 3" {
		t.Fatalf("passed %v, failure %q, err %v", passed, failure, runErr)
	}
}
