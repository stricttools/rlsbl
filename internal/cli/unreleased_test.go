package cli

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestUnreleasedThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	if r := app.Test([]string{"unreleased"}); r.ExitCode != 0 || !strings.Contains(r.Stdout, "[EXEMPT]") || !strings.Contains(r.Stdout, "Coverage: 0/0 commits covered (1 exempted).") {
		t.Fatalf("exit %d: %s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	change := repo.CommitFile("b.txt", "b\n", "a change")
	r := app.Test([]string{"unreleased"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d: %s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	for _, want := range []string{"Unreleased commits of portal since 0.4.0 (2):", "  " + change[:7] + "  a change  [MISSING]", "Coverage: 0/1 commits covered (1 exempted)."} {
		if !strings.Contains(r.Stdout, want) {
			t.Errorf("%q is not in:\n%s", want, r.Stdout)
		}
	}
	r = app.Test([]string{"unreleased", "--json"})
	commits := jsonPayload(t, r)["commits"].([]any)
	if r.ExitCode != 0 || len(commits) != 2 || commits[0].(map[string]any)["hash"] != change {
		t.Fatalf("exit %d: %v", r.ExitCode, commits)
	}
}

func TestUnreleasedWithNothingSinceTheRelease(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n")
	release := repo.Commit("the project", ".strictmetadata/releasables/releasables.toml")
	repo.Write(".strictmetadata/releases/portal/v0.1.0.toml", "format_version = 2\nbump = \"minor\"\ninclude = []\nexclude = []\ndescription = \"the first release\"\nrelease_commit = \""+release+"\"\n\n[released_trees]\n\".\" = \""+strings.Repeat("e", 40)+"\"\n")
	hygiene.Chdir(t, repo.Dir)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	if r := app.Test([]string{"unreleased"}); r.ExitCode != 0 || strings.TrimSpace(r.Stdout) != "No unreleased commits." {
		t.Fatalf("exit %d: %s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}
