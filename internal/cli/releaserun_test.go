package cli

import (
	"strings"
	"testing"

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
