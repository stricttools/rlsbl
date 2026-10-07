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

// Under --dry-run a sync with a router to write and --auto-commit previews
// the writes and makes no commit: the commit would observe files the
// preview only recorded.
func TestMonorepoSyncUnderDryRunWritesAndCommitsNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(declarations.ReleasablesFile, "format_version = 1\nrepository_layout = \"workspace\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"widget\"\ntag_format = \"widget/v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\ndev_only = true\nreleasable = false\n\n[[members]]\npath = \"widget\"\nname = \"widget\"\nreleasable = \"widget\"\n")
	repo.Write("widget/go.mod", "module github.com/acme/widget\n\ngo 1.26\n")
	repo.Write("widget/.github/workflows/ci.yml", "name: CI\non:\n  push:\n    branches: [main]\npermissions:\n  contents: read\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v6\n      - run: go test ./...\n")
	repo.Commit("the workspace", declarations.ReleasablesFile, "widget/go.mod", "widget/.github/workflows/ci.yml")
	head := repo.Git("rev-parse", "HEAD")
	hygiene.Chdir(t, repo.Dir)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"monorepo", "sync", "--auto-commit", "--dry-run"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if got := repo.Git("rev-parse", "HEAD"); got != head {
		t.Fatalf("a dry run committed: HEAD %s, was %s", got, head)
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Fatalf("a dry run changed the tree:\n%s", status)
	}
	// What the preview says names what the sync would do, never what it did.
	if !strings.Contains(r.Stdout, "Would write .github/workflows/ci-router.yml") || strings.Contains(r.Stdout, "Wrote ") {
		t.Fatalf("the preview does not say what it would write:\n%s", r.Stdout)
	}
}
