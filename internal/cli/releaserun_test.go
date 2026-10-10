package cli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestReleaseInitWritesAndCommitsTheReleaseFile(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	testsupport.FakeSafegit(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"release", "init"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Wrote and committed .strictmetadata/releases/portal/unreleased.toml") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if text := repo.Git("show", "HEAD:.strictmetadata/releases/portal/unreleased.toml"); !strings.Contains(text, `include = ["npm"]`) {
		t.Fatalf("the committed release file:\n%s", text)
	}
}

// No command keeps a machine-local name index any more: a mutating command
// in a confidential repository writes nothing under the user's
// configuration directory.
func TestAMutatingCommandWritesNoNameIndex(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	repo.Write(lifecycle.RecordFile, "format_version = 1\n\n[[licenses]]\nsubject = \"portal\"\nlicense = \"proprietary\"\nfrom = 2026-01-01\nreason = \"the license\"\n")
	repo.Commit("the license", lifecycle.RecordFile)
	testsupport.FakeSafegit(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"release", "init"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	config, err := os.UserConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(config, "strictspec")); !os.IsNotExist(err) {
		t.Fatalf("a command wrote under %s: %v", filepath.Join(config, "strictspec"), err)
	}
}
