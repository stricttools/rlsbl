package checks

import (
	"fmt"
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
	if specs := externalCheckProvider(decls)(); len(specs) != 2 {
		t.Errorf("the provider supplied %d checks, want 2", len(specs))
	}
	hygiene.Chdir(t, t.TempDir())
	if specs := externalCheckProvider(decls)(); len(specs) != 0 {
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
