package release

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/checks"
	"github.com/stricttools/rlsbl/internal/declarations"
)

func contains(list []string, item string) bool {
	for _, l := range list {
		if l == item {
			return true
		}
	}
	return false
}

// shippedPreflight is every check the shipped registry tags preflight.
var shippedPreflight = []string{"confidential-names", "lifecycle-record-valid", "strictcode", "test-suite", "upload-private-paths", "go-module-identity"}

var preflightMember = declarations.Member{
	Path: ".", Name: "root", Releasable: "portal",
	ExternalChecks: []declarations.ExternalCheck{
		{Name: "portal-smoke", Tag: "preflight", Command: "true"},
		{Name: "portal-lint", Tag: "prepush", Command: "true"},
	},
}

func TestWithoutAPreReleaseHookEveryPreflightCheckRunsTheBuiltInTestsIncluded(t *testing.T) {
	hygiene.Isolate(t)
	sel, err := preflightSelection(checks.Registry, declarations.Releasable{Name: "portal"}, preflightMember)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range append(append([]string{}, shippedPreflight...), "portal-smoke", "cross-repo-path-sources", "scaffold-conflicts") {
		if !contains(sel.Checks, want) {
			t.Errorf("the preflight leaves out %s: %v", want, sel.Checks)
		}
	}
	if contains(sel.Checks, "portal-lint") {
		t.Error("an external check of another tag runs in the preflight")
	}
	if !sel.BuiltInTests || sel.TestsReplacedBy != "" {
		t.Errorf("the built-in tests do not run: %+v", sel)
	}
}

func TestADeclaredPreReleaseHookReplacesOnlyTheBuiltInTests(t *testing.T) {
	hygiene.Isolate(t)
	plain, err := preflightSelection(checks.Registry, declarations.Releasable{Name: "portal"}, preflightMember)
	if err != nil {
		t.Fatal(err)
	}
	hooked := declarations.Releasable{Name: "portal", Hooks: declarations.Hooks{PreRelease: []declarations.Hook{{Command: "make test"}}}}
	sel, err := preflightSelection(checks.Registry, hooked, preflightMember)
	if err != nil {
		t.Fatal(err)
	}
	var want []string
	for _, c := range plain.Checks {
		if c != builtInTestsCheck {
			want = append(want, c)
		}
	}
	if strings.Join(sel.Checks, ",") != strings.Join(want, ",") {
		t.Errorf("the hook replaced more than the built-in tests:\n got %v\nwant %v", sel.Checks, want)
	}
	if sel.BuiltInTests || sel.TestsReplacedBy != `the releasable "portal"` {
		t.Errorf("the selection: %+v", sel)
	}
	member := preflightMember
	member.Hooks.PreRelease = []declarations.Hook{{Command: "make test"}}
	sel, err = preflightSelection(checks.Registry, declarations.Releasable{Name: "portal"}, member)
	if err != nil {
		t.Fatal(err)
	}
	if contains(sel.Checks, builtInTestsCheck) || !contains(sel.Checks, "portal-smoke") || !contains(sel.Checks, "strictcode") || sel.TestsReplacedBy != `the member "root"` {
		t.Errorf("a member's own hook: %+v", sel)
	}
}

func TestASelectionNeedsTheHooksTagAndTheBuiltInTests(t *testing.T) {
	hygiene.Isolate(t)
	const noHook = "app = \"rlsbl\"\n\n[checks.test-suite]\ntags = [\"preflight\"]\n"
	if _, err := preflightSelection([]byte(noHook), declarations.Releasable{Name: "portal"}, preflightMember); err == nil || !strings.Contains(err.Error(), "[hooks.pre-release]") {
		t.Errorf("a registry without the pre-release hook's tag was accepted: %v", err)
	}
	const untaggedTests = "app = \"rlsbl\"\n\n[hooks.pre-release]\ntag = \"preflight\"\n\n[checks.test-suite]\ntags = [\"prepush\"]\n"
	if _, err := preflightSelection([]byte(untaggedTests), declarations.Releasable{Name: "portal"}, preflightMember); err == nil || !strings.Contains(err.Error(), "test-suite") {
		t.Errorf("a registry whose preflight leaves out the built-in tests was accepted: %v", err)
	}
}

// fakeChecks answers RunChecks from fixed outcomes: each named check pulls
// in the checks it depends on, as strictcli's runner does.
type fakeChecks struct {
	outcomes  map[string]func() strictcli.CheckOutcome
	dependsOn map[string][]string
	impure    map[string]bool
	asked     []string
	pureOnly  []bool
}

func (f *fakeChecks) RunChecks(_ strictcli.CheckContext, opts strictcli.RunChecksOptions) ([]strictcli.CheckRunResult, []string, int, error) {
	f.asked = append(f.asked, opts.NameGlob)
	f.pureOnly = append(f.pureOnly, opts.PureOnly)
	var results []strictcli.CheckRunResult
	var listed []string
	for _, name := range append(append([]string{}, f.dependsOn[opts.NameGlob]...), opts.NameGlob) {
		if opts.PureOnly && f.impure[name] {
			listed = append(listed, name)
			continue
		}
		results = append(results, strictcli.CheckRunResult{Name: name, Outcome: f.outcomes[name]()})
	}
	return results, listed, 0, nil
}

type root string

func (r root) ProjectRoot() string { return string(r) }

func passing() strictcli.CheckOutcome { return (&strictcli.ErrorReporter{}).Passed("ok") }

func failing() strictcli.CheckOutcome {
	r := &strictcli.ErrorReporter{}
	r.Error("broken")
	return r.Found("1 problem")
}

func TestRunPreflightRunsEachCheckByNameOnceAndNamesTheFailures(t *testing.T) {
	hygiene.Isolate(t)
	fake := &fakeChecks{
		outcomes:  map[string]func() strictcli.CheckOutcome{"declarations-valid": passing, "dep-floors": failing, "strictcode": passing, "testisolation-floor": passing},
		dependsOn: map[string][]string{"dep-floors": {"declarations-valid"}, "testisolation-floor": {"declarations-valid"}},
	}
	report, err := RunPreflight(fake, root("/repo"), PreflightSelection{Checks: []string{"dep-floors", "strictcode", "testisolation-floor"}}, false)
	if strings.Join(fake.asked, ",") != "dep-floors,strictcode,testisolation-floor" {
		t.Errorf("asked: %v", fake.asked)
	}
	var ran []string
	for _, r := range report.Results {
		ran = append(ran, r.Name)
	}
	if strings.Join(ran, ",") != "declarations-valid,dep-floors,strictcode,testisolation-floor" {
		t.Errorf("each check is not reported once: %v", ran)
	}
	if err == nil || !strings.Contains(err.Error(), "dep-floors") || strings.Join(report.Failed, ",") != "dep-floors" {
		t.Errorf("the failure was not named: %v %v", err, report.Failed)
	}
}

func TestAPreflightUnderDryRunRunsPureChecksAndListsTheOthers(t *testing.T) {
	hygiene.Isolate(t)
	fake := &fakeChecks{
		outcomes: map[string]func() strictcli.CheckOutcome{"lifecycle-record-valid": passing},
		impure:   map[string]bool{"test-suite": true},
	}
	report, err := RunPreflight(fake, root("/repo"), PreflightSelection{Checks: []string{"lifecycle-record-valid", "test-suite"}}, true)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(report.Impure, ",") != "test-suite" || len(report.Results) != 1 || fake.pureOnly[0] != true {
		t.Errorf("the report: %+v (pure only %v)", report, fake.pureOnly)
	}
}

func TestTheChangelogPreflightIsEveryChangelogCheckOfItsTag(t *testing.T) {
	hygiene.Isolate(t)
	sel, err := changelogPreflightSelection(checks.Registry)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"changelog-coverage", "changelog-hashes", "changelog-schema"} {
		if !contains(sel.Checks, want) {
			t.Errorf("the changelog preflight leaves out %s: %v", want, sel.Checks)
		}
	}
	if contains(sel.Checks, builtInTestsCheck) {
		t.Error("the changelog preflight runs the built-in tests")
	}
	if _, err := changelogPreflightSelection([]byte("app = \"rlsbl\"\n")); err == nil {
		t.Error("a registry tagging no changelog check was accepted")
	}
}
