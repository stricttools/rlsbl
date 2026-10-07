package checks

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// runExternal runs one external check of the root member in the repository
// at dir.
func runExternal(t *testing.T, dir string, ec declarations.ExternalCheck) result {
	t.Helper()
	var out result
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		c, err := NewContext(ctx.Effects(), inputs(t, dir))
		if err != nil {
			return err
		}
		m := c.Workspace().RootMember()
		out = render(t, ec.Name, runExternalCheck(c, &strictcli.ErrorReporter{}, m, ec))
		return nil
	})
	if res.ExitCode != 0 {
		t.Fatalf("the check run did not finish:\n%s%s", res.Stdout, res.Stderr)
	}
	return out
}

func TestAnExternalCheckSeesTheReleaseContext(t *testing.T) {
	hygiene.Isolate(t)
	r, released, _ := releasedPortal(t)
	report := declarations.ExternalCheck{Name: "portal-report", Tag: "preflight", Command: `printf '%s|%s|%s\n' "$RLSBL_PROJECT_ROOT" "$RLSBL_LAST_TAG" "$RLSBL_UNRELEASED_RANGE"`}
	got := runExternal(t, r.Dir, report)
	mustStatus(t, got, "pass")
	if !strings.HasSuffix(got.Message, "|v1.0.0|"+released+"..HEAD") {
		t.Errorf("the release context reached the check as %q", got.Message)
	}
}

func TestAnExternalCheckBeforeTheFirstReleaseSeesNoTag(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	report := declarations.ExternalCheck{Name: "portal-report", Tag: "preflight", Command: `printf '[%s][%s]\n' "$RLSBL_LAST_TAG" "$RLSBL_UNRELEASED_RANGE"`}
	got := runExternal(t, r.Dir, report)
	mustStatus(t, got, "pass")
	if got.Message != "[][HEAD]" {
		t.Errorf("before the first release the check saw %q", got.Message)
	}
}

func TestAFailingExternalCheckReportsWhatItPrinted(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	got := runExternal(t, r.Dir, declarations.ExternalCheck{Name: "portal-lint", Tag: "preflight", Command: "echo found it; echo it is broken >&2; exit 3"})
	mustStatus(t, got, "fail")
	mustMention(t, got, "exit 3", "found it", "it is broken")
}

func TestAnExternalCheckRunsInItsDeclaredDirectory(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"tools/README.md": "tools\n"})
	got := runExternal(t, r.Dir, declarations.ExternalCheck{Name: "portal-where", Tag: "preflight", Command: "pwd", Cwd: "tools"})
	mustStatus(t, got, "pass")
	if !strings.HasSuffix(got.Message, "/tools") {
		t.Errorf("the check ran in %q", got.Message)
	}
}

func TestAnExternalCheckWhoseProgramIsMissingFailsNamingTheDeclaration(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	got := runExternal(t, r.Dir, declarations.ExternalCheck{Name: "portal-lint", Tag: "preflight", Command: "no-such-program --strict"})
	mustStatus(t, got, "fail")
	mustMention(t, got, "no-such-program is not on PATH", declarations.ReleasablesFile)
}

func TestTheProviderSuppliesTheExternalChecksOfTheMemberHere(t *testing.T) {
	hygiene.Isolate(t)
	text := fmt.Sprintf(standalonePortal, "none") + "external_checks = [{ name = \"portal-lint\", tag = \"preflight\", command = \"true\" }, { name = \"portal-docs\", tag = \"preflight\", command = \"true\" }]\n"
	r := newRepo(t, text, map[string]string{"VERSION": "1.0.0\n"})
	decls, err := declared()
	if err != nil {
		t.Fatal(err)
	}
	hygiene.Chdir(t, r.Dir)
	if specs := externalCheckProvider(decls, os.Getwd)(); len(specs) != 2 {
		t.Errorf("the provider supplied %d checks, want 2", len(specs))
	}
	hygiene.Chdir(t, t.TempDir())
	if specs := externalCheckProvider(decls, os.Getwd)(); len(specs) != 0 {
		t.Errorf("outside a repository the provider supplied %d checks", len(specs))
	}
}

func TestAnExternalCheckMayNotTakeAShippedNameOrABadOne(t *testing.T) {
	hygiene.Isolate(t)
	decls, err := declared()
	if err != nil {
		t.Fatal(err)
	}
	m := declarations.Member{Name: "root", Path: "."}
	if problem := externalNameProblem("stash-free", m, decls); !strings.Contains(problem, "takes the name of a check rlsbl ships") {
		t.Errorf("a shipped name: %q", problem)
	}
	for _, bad := range []string{"x", "Lint", "lint-*", "lint--strict", "-lint"} {
		if problem := externalNameProblem(bad, m, decls); !strings.Contains(problem, "is not a check name") {
			t.Errorf("%q: %q", bad, problem)
		}
	}
	if problem := externalNameProblem("portal-lint", m, decls); problem != "" {
		t.Errorf("a good name was refused: %s", problem)
	}
}

// widgetGadgetWithExternalChecks is the widget and gadget workspace, each
// member declaring one external check that prints the directory it ran in.
func widgetGadgetWithExternalChecks(t *testing.T) *testsupport.Repo {
	t.Helper()
	text := strings.Replace(workspaceWidgetGadget,
		"path = \"widget\"\nname = \"widget\"\nreleasable = \"widget\"\n",
		"path = \"widget\"\nname = \"widget\"\nreleasable = \"widget\"\nexternal_checks = [{ name = \"widget-report\", tag = \"preflight\", command = \"pwd\" }]\n", 1)
	text = strings.Replace(text,
		"path = \"gadget\"\nname = \"gadget\"\nreleasable = \"gadget\"\n",
		"path = \"gadget\"\nname = \"gadget\"\nreleasable = \"gadget\"\nexternal_checks = [{ name = \"gadget-report\", tag = \"preflight\", command = \"pwd\" }]\n", 1)
	if !strings.Contains(text, "widget-report") || !strings.Contains(text, "gadget-report") {
		t.Fatal("the fixture's declarations did not take the external checks")
	}
	return newRepo(t, text, map[string]string{
		"widget/go.mod":  "module github.com/acme/repo/widget\n\ngo 1.26\n",
		"widget/VERSION": "1.0.0\n",
		"gadget/go.mod":  "module github.com/acme/repo/gadget\n\ngo 1.26\n",
		"gadget/VERSION": "1.0.0\n",
	})
}

// runNamed runs the checks named glob through the checks runner of a fresh
// application, with the context of a run standing in dir, and returns the
// results by name.
func runNamed(t *testing.T, dir, glob string) map[string]result {
	t.Helper()
	app := strictcli.NewApp("rlsbl", "0.0.0", "A test application", strictcli.WithChecksEmbed(Registry))
	runner, err := Register(app)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]result{}
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		c, err := NewContext(ctx.Effects(), inputs(t, dir))
		if err != nil {
			return err
		}
		results, _, _, err := runner.RunChecks(c, strictcli.RunChecksOptions{NameGlob: glob})
		if err != nil {
			return err
		}
		for _, r := range results {
			got[r.Name] = render(t, r.Name, r.Outcome)
		}
		return nil
	})
	if res.ExitCode != 0 {
		t.Fatalf("the check run did not finish:\n%s%s", res.Stdout, res.Stderr)
	}
	return got
}

func TestTheExternalChecksAreThoseOfTheMemberTheRunNamesNotOfTheWorkingDirectory(t *testing.T) {
	hygiene.Isolate(t)
	r := widgetGadgetWithExternalChecks(t)
	widget := filepath.Join(r.Dir, "widget")
	for _, here := range []string{r.Dir, filepath.Join(r.Dir, "gadget"), t.TempDir()} {
		hygiene.Chdir(t, here)
		got := runNamed(t, widget, "widget-report")
		report, ok := got["widget-report"]
		if !ok {
			t.Fatalf("standing in %s, a run in the widget member did not run its external check widget-report", here)
		}
		mustStatus(t, report, "pass")
		if !strings.HasSuffix(report.Message, "/widget") {
			t.Errorf("standing in %s, widget-report ran in %q", here, report.Message)
		}
		if _, ok := runNamed(t, widget, "gadget-report")["gadget-report"]; ok {
			t.Errorf("standing in %s, a run in the widget member ran the gadget member's external check", here)
		}
	}
}

func TestTheCheckValuesAreThoseOfTheMemberTheRunNamesNotOfTheWorkingDirectory(t *testing.T) {
	hygiene.Isolate(t)
	r := widgetGadgetWithExternalChecks(t)
	// dep-floors is off where no entry adopts it; this one adopts it for the
	// widget member only.
	r.Write(".strictmetadata/options/manifest.toml", "owner = \"strictspec\"\n")
	r.Write(".strictmetadata/options/dependencies.toml", "format_version = 1\n\n[[entry]]\nid = \"rlsbl:dep-floors\"\nscope = \"widget\"\ncurrent = \"error\"\nideal = \"error\"\nreason = \"widget declares its floors\"\n")
	r.Git("add", "-A")
	r.Git("commit", "-q", "-m", "options")
	widget, gadget := filepath.Join(r.Dir, "widget"), filepath.Join(r.Dir, "gadget")
	for _, here := range []string{r.Dir, gadget, widget, t.TempDir()} {
		hygiene.Chdir(t, here)
		if got := runNamed(t, widget, "dep-floors")["dep-floors"]; got.Status == "off" || got.Status == "" {
			t.Errorf("standing in %s, the widget member's dep-floors, which its options adopt, ended %s", here, got)
		}
		if got := runNamed(t, gadget, "dep-floors")["dep-floors"]; got.Status != "off" {
			t.Errorf("standing in %s, the gadget member's dep-floors, which no entry adopts, ended %s", here, got)
		}
	}
}
