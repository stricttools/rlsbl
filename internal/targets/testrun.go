package targets

import (
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The built-in test commands.
var (
	goTestArgv = []string{"go", "test", "./...", "-race", "-short", "-count=1"}
	// pytestArgv runs pytest with -P so the target's directory is not put
	// first on sys.path: a flat-layout module there named like a standard
	// library one (html.py) would otherwise break pytest's own imports.
	pytestArgv = []string{"uv", "run", "python", "-P", "-m", "pytest"}
)

// TimeoutHint names the setting a test timeout comes from; every timeout
// failure ends with it.
const TimeoutHint = "(the budget is check_seconds under [timeouts] in .strictmetadata/releasables/releasables.toml; a hang still fails)"

// TestStep is one command a test run runs, in Dir.
type TestStep struct {
	// Argv runs directly; Shell, when set instead, runs through sh -c (a
	// member's declared go_command, which may carry its own arguments).
	Argv  []string
	Shell string
	Dir   string
}

// String is the step as one command line, for messages.
func (s TestStep) String() string {
	if s.Shell != "" {
		return s.Shell
	}
	return strings.Join(s.Argv, " ")
}

// TestPlan is what a target's built-in test runner runs, in order.
type TestPlan struct {
	Steps []TestStep
	// Skip is why nothing runs, or empty: an npm package declaring no test
	// script has nothing to run, which passes.
	Skip string
}

// TestInputs are what selecting a test run reads besides the target.
type TestInputs struct {
	// Dir is the target's directory, absolute.
	Dir string
	// Settings are the member's declared test settings.
	Settings declarations.TestSettings
	// UVWorkspaceRoot is the root of the uv workspace Dir is a member of
	// (absolute), or empty when it is none's.
	UVWorkspaceRoot string
	// SkipSync is set when the caller already synced the uv workspace.
	SkipSync bool
	// Overlays are the packages the project's dev overlays install from
	// local checkouts: every uv sync leaves them alone (--inexact plus one
	// --no-install-package each) and every uv run is told not to sync, or
	// the locked registry wheels would be reinstalled over the checkouts.
	Overlays []string
}

// TestPlanOf selects the built-in test run for target t.
func TestPlanOf(t Target, in TestInputs) (TestPlan, error) {
	switch t.Name() {
	case Go:
		if in.Settings.GoCommand != "" {
			return TestPlan{Steps: []TestStep{{Shell: in.Settings.GoCommand, Dir: in.Dir}}}, nil
		}
		return TestPlan{Steps: []TestStep{{Argv: append([]string(nil), goTestArgv...), Dir: in.Dir}}}, nil
	case NPM:
		p, err := readManifest(in.Dir)
		if err != nil {
			return TestPlan{}, err
		}
		var scripts map[string]any
		if _, err := p.field("scripts", &scripts); err != nil {
			return TestPlan{}, err
		}
		if test, _ := scripts["test"].(string); test == "" {
			return TestPlan{Skip: "package.json declares no test script"}, nil
		}
		return TestPlan{Steps: []TestStep{{Argv: []string{"npm", "test"}, Dir: in.Dir}}}, nil
	case PyPI:
		return pypiTestPlan(in)
	}
	return TestPlan{}, fmt.Errorf("the %s target has no built-in test runner", t.Name())
}

// overlayExclusions are the uv sync arguments keeping the overlaid packages.
func overlayExclusions(overlays []string) []string {
	var args []string
	for _, p := range overlays {
		args = append(args, "--no-install-package", p)
	}
	return args
}

// pypiTestPlan runs pytest through uv: in a uv workspace member, after a
// sync of the whole workspace at its root; in a standalone project, with
// the dependency group or extra that declares pytest.
func pypiTestPlan(in TestInputs) (TestPlan, error) {
	var markers []string
	if in.Settings.PyPIMarkers != "" {
		markers = []string{"-m", in.Settings.PyPIMarkers}
	}
	var noSync []string
	if len(in.Overlays) > 0 {
		noSync = []string{"--no-sync"}
	}
	run := func(selectors []string) []string {
		argv := append([]string{"uv", "run"}, noSync...)
		argv = append(argv, selectors...)
		argv = append(argv, pytestArgv[2:]...)
		return append(argv, markers...)
	}
	var steps []TestStep
	if in.UVWorkspaceRoot != "" {
		if !in.SkipSync {
			has, err := exists(filepath.Join(in.UVWorkspaceRoot, Pyproject))
			if err != nil {
				return TestPlan{}, err
			}
			if has {
				sync := []string{"uv", "sync", "--all-packages", "--quiet"}
				if len(in.Overlays) > 0 {
					sync = append(append(sync, "--inexact"), overlayExclusions(in.Overlays)...)
				}
				steps = append(steps, TestStep{Argv: sync, Dir: in.UVWorkspaceRoot})
			}
		}
		return TestPlan{Steps: append(steps, TestStep{Argv: run(nil), Dir: in.Dir})}, nil
	}
	selectors, err := pytestSelectors(in.Dir)
	if err != nil {
		return TestPlan{}, err
	}
	if len(in.Overlays) > 0 {
		// uv run's own sync is exact and would wipe the overlays, so the
		// sync runs first, with the selectors the suite runs with.
		sync := append([]string{"uv", "sync", "--inexact", "--quiet"}, selectors...)
		steps = append(steps, TestStep{Argv: append(sync, overlayExclusions(in.Overlays)...), Dir: in.Dir})
	}
	return TestPlan{Steps: append(steps, TestStep{Argv: run(selectors), Dir: in.Dir})}, nil
}

// pytestSelectors are the uv run selectors that install pytest for a
// standalone project: none for the dev group or uv's dev-dependencies,
// --group for another dependency group, --extra for an optional
// dependency. A project declaring pytest nowhere is refused.
func pytestSelectors(dir string) ([]string, error) {
	where, name, found, err := PytestDeclaration(dir)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, fmt.Errorf("pytest is not declared in %s; add it to [dependency-groups].dev or [project.optional-dependencies].test", filepath.Join(dir, Pyproject))
	}
	switch {
	case where == "dependency-group" && name != "dev":
		return []string{"--group", name}, nil
	case where == "optional-dependency":
		return []string{"--extra", name}, nil
	}
	return nil, nil
}

// PytestDeclaration is where dir/pyproject.toml declares pytest: a
// dependency group, an optional dependency (extra), or uv's
// dev-dependencies, each looked through in that order and by name within
// it in document order. found is false when it declares none.
func PytestDeclaration(dir string) (where, name string, found bool, err error) {
	doc, ok, err := readPyproject(dir)
	if err != nil || !ok {
		return "", "", false, err
	}
	mentions := func(entries any) bool {
		list, _ := entries.([]any)
		for _, e := range list {
			if s, ok := e.(string); ok && strings.Contains(strings.ToLower(s), "pytest") {
				return true
			}
		}
		return false
	}
	if groups, ok := doc.table("dependency-groups"); ok {
		for _, g := range doc.keysOf("dependency-groups") {
			if mentions(groups[g]) {
				return "dependency-group", g, true, nil
			}
		}
	}
	if extras, ok := doc.table("project", "optional-dependencies"); ok {
		for _, e := range doc.keysOf("project", "optional-dependencies") {
			if mentions(extras[e]) {
				return "optional-dependency", e, true, nil
			}
		}
	}
	if uv, ok := doc.table("tool", "uv"); ok && mentions(uv["dev-dependencies"]) {
		return "uv-dev-dependencies", "dev", true, nil
	}
	return "", "", false, nil
}

// RunTests runs the plan's steps in order through r, each bounded by
// timeout, and reports whether every step passed, with the failure when
// one did not. A step that does not finish (a timeout) is an error naming
// the command and the budget, and so is a program that is not on PATH.
func RunTests(r Runner, plan TestPlan, timeout time.Duration) (passed bool, failure string, err error) {
	for _, step := range plan.Steps {
		argv := step.Argv
		if step.Shell != "" {
			argv = []string{"sh", "-c", step.Shell}
		}
		if _, err := exec.LookPath(argv[0]); err != nil {
			return false, "", fmt.Errorf("%s is not on PATH, so `%s` cannot run", argv[0], step)
		}
		args := make([]interface{}, len(argv))
		for i, a := range argv {
			args[i] = a
		}
		res, err := r.Run(args, strictcli.Cwd(step.Dir), strictcli.Stream(true), strictcli.Check(false), strictcli.Timeout(timeout))
		if err != nil {
			return false, "", fmt.Errorf("`%s` did not finish within %s: %w %s", step, timeout, err, TimeoutHint)
		}
		if res.ExitCode() != 0 {
			return false, fmt.Sprintf("`%s` exited %d", step, res.ExitCode()), nil
		}
	}
	return true, "", nil
}
