package cli

import (
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// monorepo remove refuses a member the record holds an open lifecycle
// period of, naming the retirement, and the retirement it names clears the
// refusal.
func TestTheRetirementTheRemoveRefusalNamesClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := monorepoFixture(t)
	decls := strings.Replace(monorepoWorkspace, "[[releasables]]\nname = \"gadget\"\ntag_format = \"{name}@v{version}\"\npublish_mode = \"none\"\n\n", "", 1)
	decls = strings.Replace(decls, "releasable = \"gadget\"", "releasable = \"widget\"", 1)
	repo.Write(declarations.ReleasablesFile, decls)
	repo.Write(lifecycle.RecordFile, "format_version = 1\n\n[[lifecycle]]\nsubject = \"gadget\"\nstatus = \"active\"\nfrom = 2026-01-01\nreason = \"a member with a lifecycle of its own\"\n")
	repo.Write(lifecycle.ManifestFile, "owner = \"strictspec\"\n")
	repo.Write("apps/gadget/package.json", "{\n  \"name\": \"gadget\",\n  \"version\": \"0.2.0\"\n}\n")
	repo.Commit("gadget is a member of widget with a lifecycle of its own", declarations.ReleasablesFile, lifecycle.RecordFile, lifecycle.ManifestFile, "apps/gadget/package.json")
	app := appWith(t, testsupport.NewFakeHTTP(t))
	fix := "rlsbl transition lifecycle --subject gadget --status retired --reason <why>"
	r := app.Test([]string{"monorepo", "remove", "apps/gadget"})
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, "an open lifecycle period") || !strings.Contains(r.Stderr, fix) {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if r := app.Test([]string{"transition", "lifecycle", "--subject", "gadget", "--status", "retired", "--reason", "the owner ruled gadget deleted", "--approve-consequential"}); r.ExitCode != 0 {
		t.Fatalf("the retirement the refusal names: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if r := app.Test([]string{"monorepo", "remove", "apps/gadget"}); r.ExitCode != 0 {
		t.Fatalf("the remove after the retirement: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	ws, err := declarations.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := ws.Member("gadget"); ok {
		t.Fatal("gadget is still declared")
	}
}
