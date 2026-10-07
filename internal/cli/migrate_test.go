package cli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMigrateRecordsIsConsequential(t *testing.T) {
	hygiene.Isolate(t)
	cmd, ok := appWith(t, testsupport.NewFakeHTTP(t)).Groups()["migrate"].Commands["records"]
	if !ok || cmd.Effect != strictcli.EffectMutating || !cmd.Consequential {
		t.Fatalf("registered %v: %+v", ok, cmd)
	}
}

// oldLayoutRepository is a standalone project in the old layout whose
// manifest states no license, with its origin on GitHub answering public.
func oldLayoutRepository(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Git("remote", "add", "origin", "git@github.com:owner/portal.git")
	repo.Write("package.json", `{"name": "portal", "version": "0.1.0"}`+"\n")
	repo.Write(".rlsbl/config.json", `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}}`+"\n")
	repo.Commit("the old layout", "package.json", ".rlsbl/config.json")
	testsupport.FakeGH(t, testsupport.GHAnswer{
		Args:   []string{"api", "--method", "GET", "repos/owner/portal"},
		Stdout: `{"full_name": "owner/portal", "visibility": "public", "private": false}`,
	})
	return repo
}

func TestMigrateRecordsReadsTheLicensesFileAndCommits(t *testing.T) {
	hygiene.Isolate(t)
	repo := oldLayoutRepository(t)
	testsupport.FakeSafegit(t)
	saferm := t.TempDir()
	script := "#!/bin/sh\nfor last; do :; done\nrm -rf -- \"$last\"\n"
	if err := os.WriteFile(filepath.Join(saferm, "saferm"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", saferm+string(os.PathListSeparator)+os.Getenv("PATH"))
	hygiene.Chdir(t, repo.Dir)
	app := appWith(t, testsupport.NewFakeHTTP(t))

	r := app.Test([]string{"migrate", "records", "--dry-run"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "no license can be read from the manifests of portal") || !strings.Contains(r.Stderr, "--licenses") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	licenses := filepath.Join(t.TempDir(), "licenses.toml")
	if err := os.WriteFile(licenses, []byte("portal = \"MIT\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r = app.Test([]string{"migrate", "records", "--licenses", licenses, "--dry-run"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Would write .strictmetadata/releasables/releasables.toml") {
		t.Fatalf("passing the licenses did not clear the refusal: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if _, err := os.Stat(filepath.Join(repo.Dir, ".strictmetadata")); err == nil {
		t.Fatal("the dry run wrote something")
	}
	r = app.Test([]string{"migrate", "records", "--licenses", licenses, "--approve-consequential"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if !strings.Contains(repo.Git("log", "-1", "--format=%B"), "Migrate rlsbl records to the .strictmetadata layout") {
		t.Fatal("the migration was not committed")
	}
	r = app.Test([]string{"migrate", "records", "--approve-consequential"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "nothing to migrate") {
		t.Fatalf("a second run: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}
