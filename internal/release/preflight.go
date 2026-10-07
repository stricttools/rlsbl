package release

import (
	"fmt"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/checks"
	"github.com/stricttools/rlsbl/internal/declarations"
)

// builtInTestsCheck is the check that runs a member's built-in tests (the
// target's own runner: go test, npm test, pytest). A declared pre-release
// hook replaces it, and nothing else.
const builtInTestsCheck = "test-suite"

// releaseGuardChecks are checks every release runs by name beside the
// pre-release hook's selection, though they carry another tag: an
// unresolved merge conflict in a scaffolded file, and a [tool.uv.sources]
// path leaving the repository, would ship broken or unbuildable.
var releaseGuardChecks = []string{"cross-repo-path-sources", "scaffold-conflicts"}

// ChangelogPreflightTag selects the checks a release runs on the changelog
// before its pre-release pipeline.
const ChangelogPreflightTag = "preflight-changelog"

// PreflightSelection is what the release's preflight runs for one member.
type PreflightSelection struct {
	// Checks are the checks it runs, by name, sorted: every check tagged
	// with the pre-release hook's tag in checks.toml, the member's external
	// checks carrying that tag, and the release guards, the built-in tests
	// left out when a pre-release hook replaces them.
	Checks []string
	// BuiltInTests is whether the built-in tests run.
	BuiltInTests bool
	// TestsReplacedBy names who declared the pre-release hook that runs
	// instead of the built-in tests; empty when they run.
	TestsReplacedBy string
}

// registryHooks is the part of checks.toml the selection reads: each check's
// tags, and the tag each hook selects.
type registryHooks struct {
	App    string                    `toml:"app"`
	Checks map[string]map[string]any `toml:"checks"`
	Hooks  map[string]map[string]any `toml:"hooks"`
}

// preflightSelection decides the release's preflight for member m of
// releasable r, from the checks registry (checks.toml's text). Every
// preflight check runs, built-in and external alike, whether or not a
// pre-release hook is declared: a declared pre-release hook replaces only
// the built-in tests.
func preflightSelection(registry []byte, r declarations.Releasable, m declarations.Member) (PreflightSelection, error) {
	doc, err := tomledit.Unmarshal[registryHooks](registry)
	if err != nil {
		return PreflightSelection{}, fmt.Errorf("internal/checks/checks.toml: %w", err)
	}
	hook, ok := doc.Hooks[string(PreRelease)]
	tag, _ := hook["tag"].(string)
	if !ok || tag == "" {
		return PreflightSelection{}, fmt.Errorf("internal/checks/checks.toml declares no [hooks.%s] tag, which selects the release's preflight checks", PreRelease)
	}
	selected := map[string]bool{}
	for name, d := range doc.Checks {
		tags, _ := d["tags"].([]any)
		for _, t := range tags {
			if t == tag {
				selected[name] = true
			}
		}
	}
	if !selected[builtInTestsCheck] {
		return PreflightSelection{}, fmt.Errorf("internal/checks/checks.toml does not tag %s with %q, so a release would not run the built-in tests", builtInTestsCheck, tag)
	}
	for _, name := range releaseGuardChecks {
		if _, ok := doc.Checks[name]; !ok {
			return PreflightSelection{}, fmt.Errorf("internal/checks/checks.toml declares no check %s, which every release runs", name)
		}
		selected[name] = true
	}
	for _, x := range m.ExternalChecks {
		if x.Tag == tag {
			selected[x.Name] = true
		}
	}
	sel := PreflightSelection{BuiltInTests: true}
	if declarer, declared := DeclaresPreReleaseHook(r, m); declared {
		delete(selected, builtInTestsCheck)
		sel.BuiltInTests = false
		sel.TestsReplacedBy = declarer
	}
	for name := range selected {
		sel.Checks = append(sel.Checks, name)
	}
	sort.Strings(sel.Checks)
	return sel, nil
}

// PreflightSelectionFor is preflightSelection over the checks registry rlsbl
// ships.
func PreflightSelectionFor(r declarations.Releasable, m declarations.Member) (PreflightSelection, error) {
	return preflightSelection(checks.Registry, r, m)
}

// changelogPreflightSelection is the checks a release runs on the
// changelog before its pre-release pipeline: every check checks.toml tags
// ChangelogPreflightTag.
func changelogPreflightSelection(registry []byte) (PreflightSelection, error) {
	doc, err := tomledit.Unmarshal[registryHooks](registry)
	if err != nil {
		return PreflightSelection{}, fmt.Errorf("internal/checks/checks.toml: %w", err)
	}
	sel := PreflightSelection{}
	for name, d := range doc.Checks {
		tags, _ := d["tags"].([]any)
		for _, t := range tags {
			if t == ChangelogPreflightTag {
				sel.Checks = append(sel.Checks, name)
			}
		}
	}
	if len(sel.Checks) == 0 {
		return PreflightSelection{}, fmt.Errorf("internal/checks/checks.toml tags no check %q, so a release would not check its changelog", ChangelogPreflightTag)
	}
	sort.Strings(sel.Checks)
	return sel, nil
}

// ChangelogPreflightSelectionFor is changelogPreflightSelection over the
// checks registry rlsbl ships.
func ChangelogPreflightSelectionFor() (PreflightSelection, error) {
	return changelogPreflightSelection(checks.Registry)
}

// CheckRunner runs checks: the strictcli app the release command belongs to.
type CheckRunner interface {
	RunChecks(ctx strictcli.CheckContext, opts strictcli.RunChecksOptions) ([]strictcli.CheckRunResult, []string, int, error)
}

// PreflightReport is what a preflight ran and found.
type PreflightReport struct {
	// Results are every check that ran, each once, the checks a selected
	// check depends on included.
	Results []strictcli.CheckRunResult
	// Impure are the checks a preview listed without running.
	Impure []string
	// Failed are the checks that failed at error severity, by name.
	Failed []string
}

// RunPreflight runs the selection's checks with the check context ctx, one
// check at a time by its exact name, so the selection alone decides
// what runs (a tag expression cannot leave one check out). A dependency two
// checks share runs once per check and is reported once. Under --dry-run
// only pure checks run and the others are listed. An error-severity failure
// is an error naming every failed check.
func RunPreflight(app CheckRunner, ctx strictcli.CheckContext, sel PreflightSelection, dryRun bool) (PreflightReport, error) {
	var report PreflightReport
	seen := map[string]bool{}
	impure := map[string]bool{}
	for _, name := range sel.Checks {
		results, listed, _, err := app.RunChecks(ctx, strictcli.RunChecksOptions{NameGlob: name, PureOnly: dryRun})
		if err != nil {
			return report, fmt.Errorf("running the preflight check %s: %w", name, err)
		}
		if len(results) == 0 && len(listed) == 0 {
			return report, fmt.Errorf("the preflight check %s was selected, and no check registered for the member's directory holds that name, so nothing ran; an external check is registered from the external_checks of the member in %s", name, declarations.ReleasablesFile)
		}
		for _, r := range results {
			if seen[r.Name] {
				continue
			}
			seen[r.Name] = true
			report.Results = append(report.Results, r)
			if r.Gated() {
				report.Failed = append(report.Failed, r.Name)
			}
		}
		for _, l := range listed {
			if !impure[l] {
				impure[l] = true
				report.Impure = append(report.Impure, l)
			}
		}
	}
	if len(report.Failed) > 0 {
		return report, fmt.Errorf("the release's preflight checks failed: %s; `rlsbl check --name <check>` in the member's directory shows each one's problems", strings.Join(report.Failed, ", "))
	}
	return report, nil
}
