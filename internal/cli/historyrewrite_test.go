package cli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

const historyRewriteDeclarations = "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\ngithub_repository = \"acme/portal\"\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n"

// historyRewriteProject is the standalone project portal with no release,
// pushed to a bare origin, the working directory for the rest of the test.
func historyRewriteProject(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", historyRewriteDeclarations)
	repo.Write("package.json", "{\n  \"name\": \"portal\",\n  \"version\": \"0.1.0\"\n}\n")
	repo.Commit("the project", ".strictmetadata/releasables/releasables.toml", "package.json")
	repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	hygiene.Chdir(t, repo.Dir)
	return repo
}

// versionedSafegit puts a safegit on PATH that answers --version with
// version and records the scrubs it is asked for without running them.
func versionedSafegit(t *testing.T, version string) {
	t.Helper()
	dir := t.TempDir()
	script := "#!/bin/sh\nif [ \"$1\" = --version ]; then echo \"safegit " + version + "\"; exit 0; fi\necho \"fake safegit: unexpected $*\" >&2\nexit 97\n"
	if err := os.WriteFile(filepath.Join(dir, "safegit"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func TestReleaseScrubTakesItsModeAndRangeAsMemberFlags(t *testing.T) {
	hygiene.Isolate(t)
	historyRewriteProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))

	// --replace belongs to the pattern mode's scope.
	r := app.Test([]string{"release", "scrub", "--file", "notes.txt", "--replace", "x", "--entire-history", "--reason", "a token", "--dry-run", "--approve-consequential"})
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, "--replace") {
		t.Fatalf("--replace under --file: exit %d: %s", r.ExitCode, r.Stderr)
	}
	// A safegit older than the release the scrub needs is refused, and the
	// newer one clears it.
	versionedSafegit(t, "0.30.0")
	r = app.Test([]string{"release", "scrub", "--pattern", "SECRET", "--mangle", "--entire-history", "--reason", "a token", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "safegit 0.30.0 is installed") {
		t.Fatalf("an old safegit: exit %d: %s", r.ExitCode, r.Stderr)
	}
	versionedSafegit(t, "0.31.0")
	r = app.Test([]string{"release", "scrub", "--pattern", "SECRET", "--mangle", "--entire-history", "--reason", "a token", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout+r.Stderr, "scrub match --json --dry-run --pattern SECRET --mangle --entire-history") {
		t.Fatalf("the dry run: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"release", "scrub", "--pattern", "SECRET", "--replace", "", "--from-commit", "HEAD", "--reason", "a token", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "--replace must not be empty") {
		t.Fatalf("an empty --replace: exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestReleaseReconcileRefusesTheSelectorWhereTheDirectorySelectsTheReleasable(t *testing.T) {
	hygiene.Isolate(t)
	historyRewriteProject(t)
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: []string{"--version"}, Stdout: "gh version 2.0.0"},
		testsupport.GHAnswer{Args: []string{"auth", "status", "--hostname", "github.com"}},
		testsupport.GHAnswer{Args: []string{"release", "list", "--repo", "acme/portal", "--limit", "1000", "--json", "tagName", "--jq", ".[].tagName"}},
	)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"release", "reconcile", "--mode", "plan", "--releasable", "portal", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "--releasable is refused here") {
		t.Fatalf("the selector in a standalone repository: exit %d: %s", r.ExitCode, r.Stderr)
	}
	r = app.Test([]string{"release", "reconcile", "--mode", "plan", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Nothing to reconcile") {
		t.Fatalf("without the selector: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"release", "reconcile", "--mode", "plan", "--push-timeout", "0", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "--push-timeout must be a positive number of seconds") {
		t.Fatalf("a zero push timeout: exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestReleaseBackfillPreviewsAProjectWithNothingReleased(t *testing.T) {
	hygiene.Isolate(t)
	historyRewriteProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"release", "backfill", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "0 archive(s) would be written") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"release", "backfill", "--overrides", "", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "--overrides must not be empty") {
		t.Fatalf("an empty --overrides: exit %d: %s", r.ExitCode, r.Stderr)
	}
}
