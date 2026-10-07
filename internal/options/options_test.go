package options_test

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// fixtureChecks is a checks registry with an error check another depends
// on, a warn check, and the dep-floors adoption check.
const fixtureChecks = `app = "rlsbl"

[checks.changelog-hashes]
description = "Every commit hash resolves."
subject = "changelog"
tags = ["changelog"]
severity = "error"
depends_on = []

[checks.changelog-coverage]
description = "Every unreleased commit is covered."
subject = "changelog"
tags = ["changelog"]
severity = "error"
depends_on = ["changelog-hashes"]

[checks.dep-floors]
description = "Every internal dependency declares a floor."
subject = "dependencies"
tags = ["dependencies"]
severity = "error"
depends_on = []

[checks.stash-free]
description = "The repository has no stash entries."
subject = "release"
tags = ["release"]
severity = "warn"
depends_on = []
`

func fixtureRegistry(t *testing.T) *options.Registry {
	t.Helper()
	doc, err := options.RenderRegistry([]byte(fixtureChecks))
	if err != nil {
		t.Fatal(err)
	}
	reg, err := options.ParseRegistry(doc)
	if err != nil {
		t.Fatal(err)
	}
	return reg
}

func workspaceDeclarations() *declarations.Releasables {
	return &declarations.Releasables{
		Layout: declarations.LayoutWorkspace,
		Members: []declarations.Member{
			{Path: ".", Name: "root", DevOnly: true},
			{Path: "widget", Name: "widget", Releasable: "widget"},
			{Path: "gadget", Name: "gadget", Releasable: "gadget"},
		},
	}
}

func standaloneDeclarations() *declarations.Releasables {
	return &declarations.Releasables{
		Layout:  declarations.LayoutStandalone,
		Members: []declarations.Member{{Path: ".", Name: "root", Releasable: "portal"}},
	}
}

// writeEntries writes a subject document of the repository at root.
func writeEntries(t *testing.T, root, subject, body string) {
	t.Helper()
	testsupport.WriteFile(t, filepath.Join(root, ".strictmetadata", "options", subject+".toml"), "format_version = 1\n"+body)
}

func entry(id, scope, current, ideal string) string {
	text := "\n[[entry]]\nid = \"" + id + "\"\n"
	if scope != "" {
		text += "scope = \"" + scope + "\"\n"
	}
	return text + "current = \"" + current + "\"\nideal = \"" + ideal + "\"\nreason = \"a reason\"\n"
}

func load(t *testing.T, reg *options.Registry, root string, d *declarations.Releasables) *options.Options {
	t.Helper()
	o, err := options.Load(reg, root, d)
	if err != nil {
		t.Fatal(err)
	}
	return o
}

func value(t *testing.T, o *options.Options, name, member string) options.Value {
	t.Helper()
	v, err := o.Value(name, member)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func requireContains(t *testing.T, text string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in:\n%s", want, text)
		}
	}
}

func TestTheShippedRegistryIsAFreshRendering(t *testing.T) {
	hygiene.Isolate(t)
	root := testsupport.ModuleRoot(t)
	checks, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(options.ChecksFile)))
	if err != nil {
		t.Fatal(err)
	}
	committed, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(options.RegistryFile)))
	if err != nil {
		t.Fatal(err)
	}
	rendered, err := options.RenderRegistry(checks)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(rendered, committed) {
		t.Fatalf("%s is stale: run `go run ./internal/options/gen` from the repository root and commit the result", options.RegistryFile)
	}
}

func TestTheShippedRegistryLoadsWithTheOptionsThatAreNotChecks(t *testing.T) {
	hygiene.Isolate(t)
	reg, err := options.Shipped()
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range options.FrameworkChecks {
		decl, ok := reg.Declaration(f.Name)
		if !ok || decl.Default != f.Severity || decl.Scope != options.NoScope {
			t.Errorf("framework check %s is declared as %+v", f.Name, decl)
		}
	}
	sandbox, ok := reg.Declaration(options.TestSandbox)
	if !ok || sandbox.Values != "on > off" || sandbox.Default != options.Off || sandbox.Scope != options.PathScope {
		t.Errorf("test-sandbox is declared as %+v", sandbox)
	}
	tagging, ok := reg.Declaration(options.EcosystemTagging)
	if !ok || tagging.Values != "on > off" || tagging.Default != "on" || tagging.Scope != options.NoScope {
		t.Errorf("ecosystem-tagging is declared as %+v", tagging)
	}
	if !bytes.HasPrefix(reg.Document(), []byte("# rlsbl's options registry")) {
		t.Errorf("the document is not the shipped registry:\n%s", reg.Document())
	}
}

func TestEveryCheckIsAnOptionRankedByItsSeverity(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	for name, want := range map[string][3]string{
		"changelog-hashes":   {"error > warn > off", "error", options.NoScope},
		"changelog-coverage": {"error > warn > off", "error", options.NoScope},
		"stash-free":         {"warn > off", "warn", options.NoScope},
		"dep-floors":         {"error > warn > off", options.Off, options.PathScope},
	} {
		decl, ok := reg.Declaration(name)
		if !ok {
			t.Fatalf("%s is not declared", name)
		}
		if got := [3]string{decl.Values, decl.Default, decl.Scope}; got != want {
			t.Errorf("%s is declared %v, want %v", name, got, want)
		}
	}
	coverage, _ := reg.Declaration("changelog-coverage")
	if strings.Join(coverage.Requires, ",") != "changelog-hashes" {
		t.Errorf("changelog-coverage requires %v", coverage.Requires)
	}
	if reg.Strongest("stash-free") != "warn" || reg.Strongest(options.TestSandbox) != "on" {
		t.Errorf("strongest values: %q, %q", reg.Strongest("stash-free"), reg.Strongest(options.TestSandbox))
	}
}

func TestACheckTakingTheNameOfAnOptionThatIsNotACheckIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	_, err := options.RenderRegistry([]byte(fixtureChecks + `
[checks.effects-bypass]
description = "A duplicate."
subject = "code"
tags = ["code"]
severity = "error"
depends_on = []
`))
	if err == nil {
		t.Fatal("a check named like a framework check was rendered")
	}
	requireContains(t, err.Error(), "rlsbl:effects-bypass is declared twice")
}

func TestARenderingStrictspecRefusesIsNotProduced(t *testing.T) {
	hygiene.Isolate(t)
	_, err := options.RenderRegistry([]byte(fixtureChecks + `
[checks.changelog-orphans]
description = "Requires an option nobody declares."
subject = "changelog"
tags = ["changelog"]
severity = "error"
depends_on = ["no-such-check"]
`))
	if err == nil {
		t.Fatal("a registry requiring an undeclared option was rendered")
	}
	requireContains(t, err.Error(), "refused by strictspec")
}

func TestWithoutEntriesEveryOptionIsAtItsDefault(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	o := load(t, reg, t.TempDir(), workspaceDeclarations())
	v := value(t, o, "changelog-hashes", "widget")
	if v.Value != "error" || v.Source != "rlsbl:changelog-hashes default" || v.Entry != nil {
		t.Fatalf("got %+v", v)
	}
	if _, ok := o.CheckValue("changelog-hashes", "widget"); ok {
		t.Error("a check at its default was given a value")
	}
	cv, ok := o.CheckValue(options.DepFloors, "widget")
	if !ok || cv.Value != options.Off || cv.Source != "rlsbl:dep-floors default" {
		t.Errorf("an adoption check without an entry resolved to %+v, %v", cv, ok)
	}
	if _, ok := o.CheckValue("external-check", "widget"); ok {
		t.Error("a check that is not an option was given a value")
	}
	if _, err := o.Value("external-check", "widget"); err == nil {
		t.Error("an undeclared option had a value")
	}
	defaults := options.Defaults(reg)
	if off, err := defaults.IsOff(options.DepFloors, "."); err != nil || !off {
		t.Errorf("dep-floors at its default: off=%v err=%v", off, err)
	}
}

func TestAWarnEntryGivesItsCheckWarnWithItsSource(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	writeEntries(t, root, "changelog", entry("rlsbl:changelog-hashes", "", "warn", "error"))
	o := load(t, reg, root, standaloneDeclarations())
	cv, ok := o.Resolver(".")("changelog-hashes")
	if !ok || cv.Value != "warn" || cv.Source != "rlsbl:changelog-hashes in .strictmetadata/options/changelog.toml" {
		t.Fatalf("got %+v, %v", cv, ok)
	}
}

func TestAScopedEntryCoversItsMemberAndWinsOverAnUnscopedOne(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	writeEntries(t, root, "dependencies",
		entry("rlsbl:dep-floors", "", "warn", "error")+entry("rlsbl:dep-floors", "widget", "error", "error"))
	o := load(t, reg, root, workspaceDeclarations())
	if v := value(t, o, options.DepFloors, "widget"); v.Value != "error" || !strings.Contains(v.Source, `(scope "widget")`) {
		t.Errorf("widget: %+v", v)
	}
	if v := value(t, o, options.DepFloors, "gadget"); v.Value != "warn" {
		t.Errorf("gadget: %+v", v)
	}
}

func TestAScopeNamingNoMemberIsRefusedNamingTheMembersAndTheFixClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	writeEntries(t, root, "dependencies", entry("rlsbl:dep-floors", "gizmo", "error", "error"))
	_, err := options.Load(reg, root, workspaceDeclarations())
	if err == nil {
		t.Fatal("a scope naming no member was accepted")
	}
	requireContains(t, err.Error(), `rlsbl:dep-floors is scoped to "gizmo", which names no member`, "., gadget, widget", "rlsbl options set")
	writeEntries(t, root, "dependencies", entry("rlsbl:dep-floors", "gadget", "error", "error"))
	load(t, reg, root, workspaceDeclarations())
}

func TestAScopeInAStandaloneRepositoryIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	writeEntries(t, root, "dependencies", entry("rlsbl:dep-floors", ".", "error", "error"))
	_, err := options.Load(reg, root, standaloneDeclarations())
	if err == nil {
		t.Fatal("a scope in a standalone repository was accepted")
	}
	requireContains(t, err.Error(), "standalone", "Remove the entry's scope")
	_, err = options.Load(reg, root, nil)
	if err == nil {
		t.Fatal("a scope in a repository without declarations was accepted")
	}
	requireContains(t, err.Error(), "declares no members")
}

func TestAnOptionWithoutAScopeTypeTakesNoScope(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	writeEntries(t, root, "changelog", entry("rlsbl:changelog-hashes", "widget", "warn", "error"))
	_, err := options.Load(reg, root, workspaceDeclarations())
	if err == nil {
		t.Fatal("a scope on an option without a scope type was accepted")
	}
	requireContains(t, err.Error(), "STRICTSPEC_OPTIONS_SCOPE_NOT_ACCEPTED")
}

func TestInvalidEntriesStopEverythingAndTheFixClearsThem(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	writeEntries(t, root, "changelog", entry("rlsbl:changelog-hashes", "", "loud", "error"))
	_, err := options.Load(reg, root, standaloneDeclarations())
	if err == nil {
		t.Fatal("an undeclared value was accepted")
	}
	refused, ok := err.(*options.RefusedError)
	if !ok {
		t.Fatalf("got %T: %v", err, err)
	}
	if len(refused.Lines) == 0 {
		t.Fatal("the refusal names nothing")
	}
	requireContains(t, err.Error(), "STRICTSPEC_OPTIONS_UNDECLARED_CURRENT", "rlsbl options set")
	writeEntries(t, root, "changelog", entry("rlsbl:changelog-hashes", "", "warn", "error"))
	load(t, reg, root, standaloneDeclarations())
}

func TestSwitchingOffAnOptionOthersRequireIsRefusedUntilTheyAreOff(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	writeEntries(t, root, "changelog", entry("rlsbl:changelog-hashes", "", "off", "off"))
	_, err := options.Load(reg, root, standaloneDeclarations())
	if err == nil {
		t.Fatal("switching off an option another requires was accepted")
	}
	requireContains(t, err.Error(), "rlsbl:changelog-coverage")
	writeEntries(t, root, "changelog", entry("rlsbl:changelog-hashes", "", "off", "off")+entry("rlsbl:changelog-coverage", "", "off", "off"))
	o := load(t, reg, root, standaloneDeclarations())
	if off, err := o.IsOff("changelog-coverage", "."); err != nil || !off {
		t.Errorf("changelog-coverage off=%v err=%v", off, err)
	}
}

// setting runs Set in a mutating command.
func setting(t *testing.T, reg *options.Registry, root string, d *declarations.Releasables, dryRun bool, req options.SetRequest) (options.SetResult, strictcli.Result, error) {
	t.Helper()
	var result options.SetResult
	var serr error
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun}, func(ctx *strictcli.Context) error {
		result, serr = options.Set(ctx.Effects(), reg, root, d, req)
		return nil
	})
	return result, res, serr
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestSetCreatesTheDirectoryItsManifestAndTheEntry(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	req := options.SetRequest{ID: "rlsbl:dep-floors", Current: "warn", Ideal: "error", Reason: "floors land member by member", Scope: "widget"}
	result, _, err := setting(t, reg, root, workspaceDeclarations(), false, req)
	if err != nil {
		t.Fatal(err)
	}
	if result.Action != options.ActionCreated || result.Class != "debt" || result.File != ".strictmetadata/options/dependencies.toml" {
		t.Errorf("got %+v", result)
	}
	if strings.Join(result.Written, ",") != ".strictmetadata/options/manifest.toml,.strictmetadata/options/dependencies.toml" {
		t.Errorf("wrote %v", result.Written)
	}
	if got := readFile(t, filepath.Join(root, ".strictmetadata", "options", "manifest.toml")); got != "owner = \"strictspec\"\n" {
		t.Errorf("manifest: %q", got)
	}
	want := "format_version = 1\n\n[[entry]]\nid = \"rlsbl:dep-floors\"\nscope = \"widget\"\ncurrent = \"warn\"\nideal = \"error\"\nreason = \"floors land member by member\"\n"
	if got := readFile(t, filepath.Join(root, ".strictmetadata", "options", "dependencies.toml")); got != want {
		t.Errorf("document:\n%s", got)
	}
	o := load(t, reg, root, workspaceDeclarations())
	if v := value(t, o, options.DepFloors, "widget"); v.Value != "warn" {
		t.Errorf("the written entry reads as %+v", v)
	}
	again, _, err := setting(t, reg, root, workspaceDeclarations(), false, req)
	if err != nil || again.Action != options.ActionUnchanged || len(again.Written) != 0 {
		t.Errorf("a repeated set: %+v, %v", again, err)
	}
}

func TestSetUnderDryRunWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	_, res, err := setting(t, reg, root, standaloneDeclarations(), true, options.SetRequest{ID: "rlsbl:stash-free", Current: "off", Ideal: "warn", Reason: "stashes are kept here"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, ".strictmetadata")); !os.IsNotExist(err) {
		t.Fatalf("a dry run wrote .strictmetadata (%v)\n%s", err, res.Stdout)
	}
}

func TestSetUpdatesAnEntryKeepingEveryOtherLine(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	body := "# kept comment\n" + entry("rlsbl:changelog-hashes", "", "warn", "error") + entry("rlsbl:changelog-coverage", "", "warn", "error")
	writeEntries(t, root, "changelog", body)
	result, _, err := setting(t, reg, root, standaloneDeclarations(), false, options.SetRequest{ID: "rlsbl:changelog-coverage", Current: "off", Ideal: "error", Reason: "coverage starts next month"})
	if err != nil {
		t.Fatal(err)
	}
	if result.Action != options.ActionUpdated {
		t.Errorf("got %+v", result)
	}
	got := readFile(t, filepath.Join(root, ".strictmetadata", "options", "changelog.toml"))
	want := "format_version = 1\n" + "# kept comment\n" + entry("rlsbl:changelog-hashes", "", "warn", "error") +
		"\n[[entry]]\nid = \"rlsbl:changelog-coverage\"\ncurrent = \"off\"\nideal = \"error\"\nreason = \"coverage starts next month\"\n"
	if got != want {
		t.Errorf("got:\n%s\nwant:\n%s", got, want)
	}
}

func TestSetRefusesAnotherToolsIDAndAnUndeclaredValue(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	_, _, err := setting(t, reg, root, standaloneDeclarations(), false, options.SetRequest{ID: "strictcode:lint", Current: "off", Ideal: "error", Reason: "r"})
	if err == nil {
		t.Fatal("another tool's entry was written")
	}
	requireContains(t, err.Error(), "an option of strictcode, not of rlsbl")
	_, _, err = setting(t, reg, root, standaloneDeclarations(), false, options.SetRequest{ID: "rlsbl:stash-free", Current: "loud", Ideal: "warn", Reason: "r"})
	if err == nil {
		t.Fatal("an undeclared value was written")
	}
	requireContains(t, err.Error(), "nothing was written", "STRICTSPEC_OPTIONS_UNDECLARED_CURRENT")
	if _, err := os.Stat(filepath.Join(root, ".strictmetadata")); !os.IsNotExist(err) {
		t.Fatalf("a refused set wrote .strictmetadata (%v)", err)
	}
}

func TestSetRefusesAManifestNamingAnotherOwnerAndTheNamedLineFixesIt(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	manifest := filepath.Join(root, ".strictmetadata", "options", "manifest.toml")
	testsupport.WriteFile(t, manifest, "owner = \"rlsbl\"\n")
	req := options.SetRequest{ID: "rlsbl:stash-free", Current: "off", Ideal: "warn", Reason: "stashes are kept here"}
	_, _, err := setting(t, reg, root, standaloneDeclarations(), false, req)
	if err == nil {
		t.Fatal("a manifest naming another owner was accepted")
	}
	requireContains(t, err.Error(), "must name \"strictspec\"", "owner = \"strictspec\"")
	line := err.Error()[strings.LastIndex(err.Error(), "\n")+1:]
	testsupport.WriteFile(t, manifest, line+"\n")
	if _, _, err := setting(t, reg, root, standaloneDeclarations(), false, req); err != nil {
		t.Fatalf("the named line did not clear the refusal: %v", err)
	}
}

func TestADepFloorsOptionOnWithoutFloorsIsRefusedAndDeclaringThemClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	writeEntries(t, root, "dependencies", entry("rlsbl:dep-floors", "widget", "error", "error"))
	d := workspaceDeclarations()
	problems, err := load(t, reg, root, d).SettingsProblems(d, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 1 {
		t.Fatalf("problems: %v", problems)
	}
	requireContains(t, problems[0], `"widget"`, "declares no internal_dep_floors", "entry[0] (rlsbl:dep-floors) from .strictmetadata/options/dependencies.toml")
	d.Members[1].InternalDepFloors = []string{"gadget"}
	problems, err = load(t, reg, root, d).SettingsProblems(d, false)
	if err != nil || len(problems) != 0 {
		t.Fatalf("declaring the floors left %v (%v)", problems, err)
	}
}

func TestFloorsWhileDepFloorsIsOffAreRefusedAndTheNamedCommandSwitchesItOn(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	d := workspaceDeclarations()
	d.Members[2].InternalDepFloors = []string{"widget"}
	problems, err := options.Defaults(reg).SettingsProblems(d, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 1 {
		t.Fatalf("problems: %v", problems)
	}
	requireContains(t, problems[0], `"gadget"`, "nothing reads them", "rlsbl options set rlsbl:dep-floors --current error --ideal error --scope gadget")
	if _, _, err := setting(t, reg, root, d, false, options.SetRequest{ID: "rlsbl:dep-floors", Current: "error", Ideal: "error", Reason: "gadget pins its floors", Scope: "gadget"}); err != nil {
		t.Fatal(err)
	}
	problems, err = load(t, reg, root, d).SettingsProblems(d, false)
	if err != nil || len(problems) != 0 {
		t.Fatalf("the named command left %v (%v)", problems, err)
	}
}

func TestTheTestRunnerSettingsFollowTheTestSandboxOption(t *testing.T) {
	hygiene.Isolate(t)
	reg := fixtureRegistry(t)
	root := t.TempDir()
	d := standaloneDeclarations()
	problems, err := options.Defaults(reg).SettingsProblems(d, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 1 {
		t.Fatalf("problems: %v", problems)
	}
	requireContains(t, problems[0], declarations.TestRunnerFile, "nothing reads it", "rlsbl options set rlsbl:test-sandbox --current on --ideal on --reason")
	if _, _, err := setting(t, reg, root, d, false, options.SetRequest{ID: "rlsbl:test-sandbox", Current: "on", Ideal: "on", Reason: "the suite runs sandboxed"}); err != nil {
		t.Fatal(err)
	}
	o := load(t, reg, root, d)
	if problems, err := o.SettingsProblems(d, true); err != nil || len(problems) != 0 {
		t.Fatalf("the named command left %v (%v)", problems, err)
	}
	problems, err = o.SettingsProblems(d, false)
	if err != nil || len(problems) != 1 {
		t.Fatalf("an option on without its file: %v (%v)", problems, err)
	}
	requireContains(t, problems[0], "rlsbl:test-sandbox is on", "there is no "+declarations.TestRunnerFile)
}
