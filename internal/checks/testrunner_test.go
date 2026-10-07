package checks

import (
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// portalTesting is portal with its go tests replaced by command.
func portalTesting(t *testing.T, command string) *testsupport.Repo {
	t.Helper()
	declared := fmt.Sprintf(standalonePortal, "none") + fmt.Sprintf("test = { go_command = %q }\n", command)
	return newRepo(t, declared, map[string]string{
		"go.mod":   "module github.com/acme/portal\n\ngo 1.26\n",
		"VERSION":  "1.0.0\n",
		recordFile: publicRecord,
	})
}

func TestAFailingSuiteFailsTheCheckAndAPassingOnePasses(t *testing.T) {
	hygiene.Isolate(t)
	r := portalTesting(t, "exit 3")
	got := runCheck(t, inputs(t, r.Dir), "test-suite")
	mustStatus(t, got, "fail")
	mustMention(t, got, "the go tests failed", "exited 3")
	r = portalTesting(t, "test -f go.mod")
	got = runCheck(t, inputs(t, r.Dir), "test-suite")
	mustStatus(t, got, "pass")
	mustMention(t, got, `the go tests of "root" passed`)
}

func TestTheSuiteOfAWorkspacesRootIsLeftToTheWorkspaceCheck(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	got := runCheck(t, inputs(t, r.Dir), "test-suite")
	mustStatus(t, got, "skip")
	mustMention(t, got, "test-suite-workspace")
}

func TestAnNpmPackageWithoutATestScriptHasNothingToRun(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"go.mod":       "",
		"VERSION":      "",
		"package.json": fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"),
	})
	got := runCheck(t, inputs(t, r.Dir), "test-suite")
	mustStatus(t, got, "skip")
	mustMention(t, got, "no test script")
}

// testingWorkspace is the widget and gadget workspace whose members run
// command as their go tests.
func testingWorkspace(t *testing.T, widget, gadget string) *testsupport.Repo {
	t.Helper()
	declared := strings.Replace(workspaceWidgetGadget, "name = \"widget\"\nreleasable = \"widget\"\n", "name = \"widget\"\nreleasable = \"widget\"\n"+fmt.Sprintf("test = { go_command = %q }\n", widget), 1)
	declared = strings.Replace(declared, "name = \"gadget\"\nreleasable = \"gadget\"\n", "name = \"gadget\"\nreleasable = \"gadget\"\n"+fmt.Sprintf("test = { go_command = %q }\n", gadget), 1)
	return newRepo(t, declared, map[string]string{
		"widget/go.mod": "module github.com/acme/repo/widget\n\ngo 1.26\n",
		"gadget/go.mod": "module github.com/acme/repo/gadget\n\ngo 1.26\n",
		".strictmetadata/releases/widget/version": "1.0.0\n",
		".strictmetadata/releases/gadget/version": "1.0.0\n",
	})
}

func TestThePushRunsTheSuitesOfTheMembersItTouches(t *testing.T) {
	hygiene.Isolate(t)
	r := testingWorkspace(t, "exit 0", "exit 4")
	got := runCheck(t, inputs(t, r.Dir), "test-suite-workspace")
	mustStatus(t, got, "skip")
	mustMention(t, got, "not in a push")
	base := r.Head()
	widgetOnly := r.CommitFile("widget/main.go", "package main\n\nfunc main() {}\n", "Add widget's entry point")
	in := inputs(t, r.Dir)
	in.PushLines = []string{pushLine(widgetOnly, base)}
	got = runCheck(t, in, "test-suite-workspace")
	mustStatus(t, got, "pass")
	mustMention(t, got, "the suites of 1 member(s) passed")
	both := r.CommitFile("gadget/main.go", "package main\n\nfunc main() {}\n", "Add gadget's entry point")
	in.PushLines = []string{pushLine(both, base)}
	got = runCheck(t, in, "test-suite-workspace")
	mustStatus(t, got, "fail")
	mustMention(t, got, "gadget: the go tests failed", "exited 4")
	if strings.Contains(got.texts(), "widget") {
		t.Errorf("the passing member was reported: %s", got)
	}
}

func TestAPushTouchingOnlyTheRootRunsNoMemberSuite(t *testing.T) {
	hygiene.Isolate(t)
	r := testingWorkspace(t, "exit 1", "exit 1")
	base := r.Head()
	head := r.CommitFile("README.md", "readme\n", "Add a readme")
	in := inputs(t, r.Dir)
	in.PushLines = []string{pushLine(head, base)}
	got := runCheck(t, in, "test-suite-workspace")
	mustStatus(t, got, "pass")
	mustMention(t, got, "touches no member")
}

// optionsOn writes the options entry switching rlsbl:test-sandbox on.
func optionsOn(r *testsupport.Repo) {
	r.Write(".strictmetadata/options/manifest.toml", "owner = \"strictspec\"\n")
	r.Write(".strictmetadata/options/tests.toml", "format_version = 1\n\n[[entry]]\nid = \"rlsbl:test-sandbox\"\ncurrent = \"on\"\nideal = \"on\"\nreason = \"the suite runs in the sandbox\"\n")
}

const sandboxRunner = "format_version = 1\nrunner_path = \"scripts/test-sandbox.sh\"\ncommand = \"go test ./...\"\nci_workflows = [\".github/workflows/ci.yml\"]\n"

func TestTheTestisolationFloorIsNotAdoptedByDefault(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	got := runCheck(t, inputs(t, r.Dir), "testisolation-floor")
	mustStatus(t, got, "skip")
	mustMention(t, got, "not adopted", "rlsbl:test-sandbox is off")
}

func TestASuiteRequiringTheSandboxFailsUntilTheRunnerIsAdopted(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"pyproject.toml": "[project]\nname = \"portal\"\nversion = \"1.0.0\"\n\n[dependency-groups]\ndev = [\"testisolation>=0.3\"]\n\n[tool.pytest.ini_options]\ntestisolation_sandbox_required = true\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "testisolation-floor")
	mustStatus(t, got, "fail")
	mustMention(t, got, "testisolation_sandbox_required = true", "rlsbl options set rlsbl:test-sandbox --current on --ideal on", "rlsbl scaffold")
	// The fix: the option on, the runner's settings, and the runner scaffold
	// writes, which the CI workflow runs.
	optionsOn(r)
	r.Write(".strictmetadata/test-runner/test-runner.toml", sandboxRunner)
	r.Write("scripts/test-sandbox.sh", "#!/bin/sh\n")
	if err := os.Chmod(r.Path("scripts/test-sandbox.sh"), 0o755); err != nil {
		t.Fatal(err)
	}
	r.Write(".github/workflows/ci.yml", "jobs:\n  test:\n    steps:\n      - run: scripts/test-sandbox.sh\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "testisolation-floor"), "pass")
}

func TestTheAdoptedSandboxRunnerMustExistBeExecutableAndBeRunByCI(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	optionsOn(r)
	r.Write(".strictmetadata/test-runner/test-runner.toml", sandboxRunner)
	got := runCheck(t, inputs(t, r.Dir), "testisolation-floor")
	mustStatus(t, got, "fail")
	mustMention(t, got, `runner_path "scripts/test-sandbox.sh", which does not exist`, ".github/workflows/ci.yml, which does not exist")
	r.Write("scripts/test-sandbox.sh", "#!/bin/sh\n")
	r.Write(".github/workflows/ci.yml", "jobs:\n  test:\n    steps:\n      - run: go test ./...\n")
	got = runCheck(t, inputs(t, r.Dir), "testisolation-floor")
	mustStatus(t, got, "fail")
	mustMention(t, got, "not an executable file", "does not invoke the sandbox runner")
	if err := os.Chmod(r.Path("scripts/test-sandbox.sh"), 0o755); err != nil {
		t.Fatal(err)
	}
	r.Write(".github/workflows/ci.yml", "jobs:\n  test:\n    steps:\n      - run: scripts/test-sandbox.sh\n")
	got = runCheck(t, inputs(t, r.Dir), "testisolation-floor")
	mustStatus(t, got, "pass")
	mustMention(t, got, ".github/workflows/ci.yml invokes scripts/test-sandbox.sh")
}
