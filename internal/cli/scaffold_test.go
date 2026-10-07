package cli

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// scaffoldTopics answers the topic read of acme/portal: the rlsbl topic is
// already there, so scaffold writes none.
var scaffoldTopics = testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/acme/portal/topics"}, Stdout: `{"names":["rlsbl"]}`}

// scaffoldGoProject is a standalone Go project on GitHub as acme/portal
// publishing nothing, the working directory for the rest of the test.
func scaffoldGoProject(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(declarations.ReleasablesFile, "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\ngithub_repository = \"acme/portal\"\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n")
	repo.Write("go.mod", "module github.com/acme/portal\n\ngo 1.26\n")
	repo.Write("main.go", "package main\n\nfunc main() {}\n")
	repo.Commit("the module", declarations.ReleasablesFile, "go.mod", "main.go")
	testsupport.FakeGH(t, scaffoldTopics)
	hygiene.Chdir(t, repo.Dir)
	return repo
}

func TestScaffoldThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	repo := scaffoldGoProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))

	r := app.Test([]string{"scaffold", "--dry-run"})
	if r.ExitCode != 0 {
		t.Fatalf("a dry run: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if _, err := os.Stat(repo.Path("VERSION")); err == nil {
		t.Fatal("the dry run wrote VERSION")
	}

	r = app.Test([]string{"scaffold", "--no-auto-commit"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Not committed (--no-auto-commit).") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	for _, p := range []string{"VERSION", ".github/workflows/ci.yml", ".strictmetadata/go.mod", declarations.ScaffoldStateFile} {
		if _, err := os.Stat(repo.Path(p)); err != nil {
			t.Errorf("%s was not written: %v", p, err)
		}
	}
	if subject := repo.Git("log", "-1", "--format=%s"); subject != "the module" {
		t.Errorf("--no-auto-commit committed: %q", subject)
	}

	testsupport.FakeSafegit(t)
	r = app.Test([]string{"scaffold"})
	if r.ExitCode != 0 || repo.Git("log", "-1", "--format=%s") != "rlsbl scaffold" {
		t.Fatalf("committed by default: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestScaffoldRefusesAPublishModeItDoesNotKnow(t *testing.T) {
	hygiene.Isolate(t)
	scaffoldGoProject(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"scaffold", "--publish-mode", "registry", "--no-auto-commit"})
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, "publish-mode") {
		t.Fatalf("exit %d\n%s", r.ExitCode, r.Stderr)
	}
}

// --publish-mode declares the releasable's publish mode in the
// declarations.
func TestScaffoldDeclaresThePublishMode(t *testing.T) {
	hygiene.Isolate(t)
	repo := scaffoldGoProject(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"scaffold", "--publish-mode", "ci", "--no-auto-commit"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	data, err := os.ReadFile(repo.Path(declarations.ReleasablesFile))
	if err != nil || !strings.Contains(string(data), `publish_mode = "ci"`) {
		t.Fatalf("the declarations:\n%s", data)
	}
	if !strings.Contains(r.Stdout, "No publish workflow: no pipeline of the member publishes from CI.") {
		t.Errorf("the report:\n%s", r.Stdout)
	}
}
