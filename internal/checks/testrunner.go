package checks

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/targets"
)

// The test runner family: the built-in test runs of a member and of the
// members a push touches, and the testisolation floor: an adopted sandboxed
// test runner that exists, is executable, and is what every CI workflow
// test-runner.toml names invokes.
func testRunnerChecks() []check {
	return []check{
		errorCheck("test-suite", checkTestSuite),
		errorCheck("test-suite-workspace", checkTestSuiteWorkspace),
		errorCheck("testisolation-floor", checkTestisolationFloor),
	}
}

// testRun is one member's built-in test run.
type testRun struct {
	member declarations.Member
	target string
	plan   targets.TestPlan
}

// testInputs are what the member's test run on target t is selected with.
// uvRoot is the uv workspace root t.dir is a member of, or empty; overlays
// are the packages the dev overlays install.
func testInputs(m declarations.Member, t memberTarget, uvRoot string, skipSync bool, overlays []string) targets.TestInputs {
	return targets.TestInputs{Dir: t.dir, Settings: m.Test, UVWorkspaceRoot: uvRoot, SkipSync: skipSync, Overlays: overlays}
}

// uvRootOf is the uv workspace root a pypi target's directory is a member
// of, or empty; other targets have none.
func uvRootOf(t memberTarget) string {
	if !t.target.Facts().SharesWorkspaceEnvironment {
		return ""
	}
	root, found, err := dependencies.FindUvWorkspaceRoot(t.dir)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if !found {
		return ""
	}
	return root
}

// overlayPackages are the names of the overlays.
func overlayPackages(overlays []dependencies.Overlay) []string {
	var names []string
	for _, o := range overlays {
		names = append(names, o.Package)
	}
	return names
}

// runTestRuns runs each plan through the observed effects handle and
// returns a problem per run that failed or did not finish, and the members
// that passed and whose run had nothing to run.
func runTestRuns(c *Context, runs []testRun) (problems, passed, skipped []string) {
	for _, run := range runs {
		if run.plan.Skip != "" {
			skipped = append(skipped, fmt.Sprintf("%s (%s)", run.member.Name, run.plan.Skip))
			continue
		}
		ok, failure, err := targets.RunTests(observedHandle{c.Effects()}, run.plan, c.CheckTimeout())
		switch {
		case err != nil:
			problems = append(problems, fmt.Sprintf("%s: the %s tests did not run to the end: %v", run.member.Name, run.target, err))
		case !ok:
			problems = append(problems, fmt.Sprintf("%s: the %s tests failed: %s", run.member.Name, run.target, failure))
		default:
			passed = append(passed, run.member.Name)
		}
	}
	return problems, passed, skipped
}

func checkTestSuite(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	m := c.Member()
	if c.Declarations().IsWorkspace() && m.IsRoot() {
		return r.Skipped("the workspace's root member: test-suite-workspace runs the suites of the members a push touches")
	}
	ts := targetsOf(c, m)
	if len(ts) == 0 {
		return r.Skipped(fmt.Sprintf("the member %q has no target, and the built-in test runners belong to targets (%s)", m.Name, strings.Join(targets.Names(), ", ")))
	}
	t := ts[0]
	var overlays []string
	if t.target.Facts().SharesWorkspaceEnvironment {
		active, err := dependencies.ActiveOverlays(c.Workspace().MemberDir(m))
		if err != nil {
			return reportErrors(r, []string{err.Error()}, "the dev overlays disagree with what was synced", "")
		}
		overlays = overlayPackages(active)
	}
	plan, err := targets.TestPlanOf(t.target, testInputs(m, t, uvRootOf(t), false, overlays))
	if err != nil {
		return reportErrors(r, []string{err.Error()}, fmt.Sprintf("the %s test run cannot be selected", t.target.Name()), "")
	}
	if plan.Skip != "" {
		return r.Skipped(plan.Skip)
	}
	problems, _, _ := runTestRuns(c, []testRun{{member: m, target: t.target.Name(), plan: plan}})
	return reportErrors(r, problems, fmt.Sprintf("the %s tests of %q failed", t.target.Name(), m.Name), fmt.Sprintf("the %s tests of %q passed", t.target.Name(), m.Name))
}

func checkTestSuiteWorkspace(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	refs, inPush := pushRefs(c)
	if !inPush {
		return r.Skipped(notInPush)
	}
	changed, err := c.Repo().PushChangedFiles(refs)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	inScope := map[string]bool{}
	for _, m := range c.Members() {
		inScope[m.Name] = true
	}
	var affected []declarations.Member
	for _, m := range c.Workspace().AffectedMembers(changed) {
		if inScope[m.Name] {
			affected = append(affected, m)
		}
	}
	if len(affected) == 0 {
		return r.Passed("the push touches no member whose suite runs here")
	}
	type selected struct {
		member declarations.Member
		target memberTarget
		uvRoot string
	}
	var picks []selected
	var noRunner []string
	groupDirs := map[string][]string{}
	for _, m := range affected {
		ts := targetsOf(c, m)
		if len(ts) == 0 {
			noRunner = append(noRunner, m.Name)
			continue
		}
		s := selected{member: m, target: ts[0], uvRoot: uvRootOf(ts[0])}
		if s.uvRoot != "" {
			groupDirs[s.uvRoot] = append(groupDirs[s.uvRoot], c.Workspace().MemberDir(m))
		}
		picks = append(picks, s)
	}
	// The members of one uv workspace share one environment: it is synced
	// once, keeping every member's overlays, and each run leaves it alone.
	groupOverlays := map[string][]string{}
	for root, dirs := range groupDirs {
		merged, err := dependencies.CollectActiveOverlays(dirs)
		if err != nil {
			return reportErrors(r, []string{err.Error()}, "the dev overlays of the members sharing one environment disagree", "")
		}
		groupOverlays[root] = overlayPackages(merged)
	}
	synced := map[string]bool{}
	var runs []testRun
	var problems []string
	for _, s := range picks {
		var overlays []string
		if s.uvRoot != "" {
			overlays = groupOverlays[s.uvRoot]
		} else if s.target.target.Facts().SharesWorkspaceEnvironment {
			active, err := dependencies.ActiveOverlays(c.Workspace().MemberDir(s.member))
			if err != nil {
				problems = append(problems, fmt.Sprintf("%s: %v", s.member.Name, err))
				continue
			}
			overlays = overlayPackages(active)
		}
		plan, err := targets.TestPlanOf(s.target.target, testInputs(s.member, s.target, s.uvRoot, s.uvRoot != "" && synced[s.uvRoot], overlays))
		if err != nil {
			problems = append(problems, fmt.Sprintf("%s: the %s test run cannot be selected: %v", s.member.Name, s.target.target.Name(), err))
			continue
		}
		if s.uvRoot != "" {
			synced[s.uvRoot] = true
		}
		runs = append(runs, testRun{member: s.member, target: s.target.target.Name(), plan: plan})
	}
	failed, passed, skipped := runTestRuns(c, runs)
	problems = append(problems, failed...)
	summary := fmt.Sprintf("the suites of %d member(s) passed", len(passed))
	if len(skipped) > 0 {
		summary += "; nothing to run for " + strings.Join(skipped, ", ")
	}
	if len(noRunner) > 0 {
		sort.Strings(noRunner)
		summary += "; no target, so no test runner, for " + strings.Join(noRunner, ", ")
	}
	return reportErrors(r, problems, fmt.Sprintf("the suites of %d member(s) failed or could not run", len(problems)), summary)
}

// testisolationPlugin is the distribution of the testisolation pytest
// plugin.
const testisolationPlugin = "testisolation"

// requirementName is the distribution name a PEP 508 requirement starts
// with.
var requirementName = regexp.MustCompile(`^\s*([A-Za-z0-9][A-Za-z0-9._-]*)`)

// pyprojectDocument reads a pyproject.toml; found is false when it does not
// exist.
func pyprojectDocument(path string) (doc map[string]any, found bool) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false
	}
	if err != nil {
		panic(unanswered(err.Error()))
	}
	parsed, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		panic(unanswered(fmt.Sprintf("%s: %v", path, err)))
	}
	return *parsed, true
}

// pluginDeclared reports whether the pyproject declares the testisolation
// plugin as a dependency: a runtime one, an optional one, or one of a
// dependency group.
func pluginDeclared(doc map[string]any) bool {
	project, _ := doc["project"].(map[string]any)
	buckets := []any{project["dependencies"]}
	extras, _ := project["optional-dependencies"].(map[string]any)
	for _, v := range extras {
		buckets = append(buckets, v)
	}
	groups, _ := doc["dependency-groups"].(map[string]any)
	for _, v := range groups {
		buckets = append(buckets, v)
	}
	for _, b := range buckets {
		list, _ := b.([]any)
		for _, entry := range list {
			s, ok := entry.(string)
			if !ok {
				continue
			}
			if m := requirementName.FindStringSubmatch(s); m != nil && dependencies.NormalizePypiName(m[1]) == testisolationPlugin {
				return true
			}
		}
	}
	return false
}

// sandboxRequired reports whether the suite declares
// testisolation_sandbox_required true under [tool.pytest.ini_options].
func sandboxRequired(doc map[string]any) bool {
	tool, _ := doc["tool"].(map[string]any)
	pytest, _ := tool["pytest"].(map[string]any)
	ini, _ := pytest["ini_options"].(map[string]any)
	switch v := ini["testisolation_sandbox_required"].(type) {
	case bool:
		return v
	case string:
		switch strings.ToLower(strings.TrimSpace(v)) {
		case "1", "true", "yes", "on":
			return true
		}
	}
	return false
}

func checkTestisolationFloor(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	sandbox, err := c.Options().Value(options.TestSandbox, declarations.RootPath)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	on := sandbox.Value != options.Off
	m := c.Member()
	pyproject := filepath.Join(c.Workspace().MemberDir(m), targets.Pyproject)
	doc, _ := pyprojectDocument(pyproject)
	plugin := pluginDeclared(doc)
	if !on && !plugin {
		return r.Skipped(fmt.Sprintf("the testisolation floor is not adopted: %s%s is off (%s), and %s declares no testisolation dependency", options.Prefix, options.TestSandbox, sandbox.Source, declarations.Join(m.Path, targets.Pyproject)))
	}
	runner, found, err := declarations.LoadTestRunner(c.Root())
	if err != nil {
		return reportErrors(r, []string{err.Error()}, declarations.TestRunnerFile+" cannot be read", "")
	}
	if !found {
		if on {
			return reportErrors(r, []string{fmt.Sprintf("%s%s is %s (%s), but there is no %s, so no sandbox runner is distributed: write it, and run `rlsbl scaffold`", options.Prefix, options.TestSandbox, sandbox.Value, sandbox.Source, declarations.TestRunnerFile)}, "no sandbox runner is declared", "")
		}
		if sandboxRequired(doc) {
			strongest := c.Options().Registry().Strongest(options.TestSandbox)
			problem := fmt.Sprintf("%s sets testisolation_sandbox_required = true, but %s%s is off (%s), so no sandbox runner is distributed to this repository. Switch it on (%s), write %s, and run `rlsbl scaffold`", declarations.Join(m.Path, targets.Pyproject), options.Prefix, options.TestSandbox, sandbox.Source, options.SetCommand(options.TestSandbox, strongest, strongest, "<why this repository runs its suite in the sandbox>", options.MemberScope(c.Declarations(), declarations.RootPath)), declarations.TestRunnerFile)
			return reportErrors(r, []string{problem}, "the suite requires a sandbox runner nothing distributes", "")
		}
		return r.Passed("the testisolation plugin is adopted without a sandbox runner, and the suite does not require one (testisolation_sandbox_required is not true)")
	}
	var problems, notes []string
	runnerAbs := filepath.Join(c.Root(), filepath.FromSlash(runner.RunnerPath))
	info, err := os.Stat(runnerAbs)
	switch {
	case errors.Is(err, os.ErrNotExist):
		problems = append(problems, fmt.Sprintf("%s declares runner_path %q, which does not exist: run `rlsbl scaffold`, which writes the runner", declarations.TestRunnerFile, runner.RunnerPath))
	case err != nil:
		panic(unanswered(err.Error()))
	case info.IsDir() || info.Mode().Perm()&0o111 == 0:
		problems = append(problems, fmt.Sprintf("the sandbox runner %s is not an executable file: run `rlsbl scaffold`, which writes it executable", runner.RunnerPath))
	default:
		notes = append(notes, fmt.Sprintf("the runner %s is present", runner.RunnerPath))
	}
	for _, workflow := range runner.CIWorkflows {
		data, err := os.ReadFile(filepath.Join(c.Root(), filepath.FromSlash(workflow)))
		if errors.Is(err, os.ErrNotExist) {
			problems = append(problems, fmt.Sprintf("the ci_workflows of %s name %s, which does not exist: correct ci_workflows, or create the workflow", declarations.TestRunnerFile, workflow))
			continue
		}
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if !strings.Contains(string(data), runner.RunnerPath) {
			problems = append(problems, fmt.Sprintf("the CI workflow %s does not invoke the sandbox runner %s, though %s declares that it runs the suite through it: run the suite there with %s", workflow, runner.RunnerPath, declarations.TestRunnerFile, runner.RunnerPath))
			continue
		}
		notes = append(notes, fmt.Sprintf("%s invokes %s", workflow, runner.RunnerPath))
	}
	return reportErrors(r, problems, fmt.Sprintf("%d testisolation floor problem(s)", len(problems)), strings.Join(firstOf(append(notes, "the testisolation floor is adopted"), 3), "; "))
}
