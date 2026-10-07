package cli

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMonorepoSyncRefusesAStandaloneRepository(t *testing.T) {
	hygiene.Isolate(t)
	scaffoldGoProject(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"monorepo", "sync", "--no-auto-commit"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "standalone layout") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestMonorepoSyncRequiresTheCommitChoice(t *testing.T) {
	hygiene.Isolate(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"monorepo", "sync"})
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, "auto-commit") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

// A workspace whose members have no CI and publish nothing has nothing to
// route, and the payload says so.
func TestMonorepoSyncOfAWorkspaceWithNothingToRoute(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(declarations.ReleasablesFile, "format_version = 1\nrepository_layout = \"workspace\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"widget\"\ntag_format = \"widget/v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\ndev_only = true\nreleasable = false\n\n[[members]]\npath = \"widget\"\nname = \"widget\"\nreleasable = \"widget\"\n")
	repo.Write("widget/go.mod", "module github.com/acme/widget\n\ngo 1.26\n")
	hygiene.Chdir(t, repo.Dir)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"monorepo", "sync", "--no-auto-commit"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "The routers are current; nothing was written.") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"monorepo", "sync", "--no-auto-commit", "--json"})
	payload := jsonPayload(t, r)
	if written, _ := payload["written"].([]any); len(written) != 0 || payload["committed"] != false {
		t.Fatalf("payload: %v", payload)
	}
}
