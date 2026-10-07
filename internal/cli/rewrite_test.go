package cli

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func rewriteFileText(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// rewriteGoProject is a repository owning github.com/o/foo, the working directory
// for the rest of the test.
func rewriteGoProject(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write("go.mod", "module github.com/o/foo\n\ngo 1.26\n")
	repo.Write("cmd/foo/main.go", "package main\n\nimport _ \"github.com/o/foo/internal/x\"\n\nfunc main() {}\n")
	hygiene.Chdir(t, repo.Path("cmd/foo"))
	return repo
}

func TestRewriteGoModulePathThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	repo := rewriteGoProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))

	r := app.Test([]string{"rewrite", "go-module-path", "--from-module", "github.com/o/foo", "--to-module", "github.com/n/bar", "--dry-run"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "go.mod: rewrite: 1 occurrence in this go.mod") || !strings.Contains(r.Stdout, "cmd/foo/main.go: rewrite: 1 occurrence") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if strings.Contains(rewriteFileText(t, repo.Path("go.mod")), "github.com/n/bar") {
		t.Fatal("the dry run wrote")
	}

	r = app.Test([]string{"rewrite", "go-module-path", "--from-module", "github.com/o/fooo", "--to-module", "github.com/n/bar"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "Check --from-module for a typo") {
		t.Fatalf("a typo: exit %d: %s", r.ExitCode, r.Stderr)
	}

	r = app.Test([]string{"rewrite", "go-module-path", "--from-module", "github.com/o/foo", "--to-module", "github.com/n/bar"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Renamed github.com/o/foo -> github.com/n/bar across 2 files.") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if !strings.Contains(rewriteFileText(t, repo.Path("cmd/foo/main.go")), `"github.com/n/bar/internal/x"`) {
		t.Error("the apply did not write")
	}
}

func TestRewriteGoModulePathRefusesMissingAndEmptyModules(t *testing.T) {
	hygiene.Isolate(t)
	rewriteGoProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	if r := app.Test([]string{"rewrite", "go-module-path", "--from-module", "github.com/o/foo"}); r.ExitCode == 0 || !strings.Contains(r.Stderr, "to-module") {
		t.Errorf("a missing --to-module: exit %d: %s", r.ExitCode, r.Stderr)
	}
	if r := app.Test([]string{"rewrite", "go-module-path", "--from-module", "", "--to-module", "github.com/n/bar"}); r.ExitCode != 1 || !strings.Contains(r.Stderr, "--from-module must not be empty") {
		t.Errorf("an empty --from-module: exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestRewriteProjectNameIsConsequentialAndPreviews(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n")
	repo.Write("package.json", "{\n  \"name\": \"portal\",\n  \"version\": \"0.1.0\"\n}\n")
	repo.Write(".strictmetadata/releases/portal/unreleased.toml", "format_version = 2\nbump = \"minor\"\ninclude = [\"npm\"]\nexclude = []\ndescription = \"the next release\"\n")
	repo.Commit("the project", ".strictmetadata/releasables/releasables.toml", "package.json", ".strictmetadata/releases/portal/unreleased.toml")
	hygiene.Chdir(t, repo.Dir)
	app := appWith(t, testsupport.NewFakeHTTP(t))

	r := app.Test([]string{"rewrite", "project-name", "--from", "portal", "--to", "gateway", "--dry-run"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "package.json: rewrite: 1 occurrence in this npm manifest") || !strings.Contains(r.Stdout, "Remaining steps, in order:") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"rewrite", "project-name", "--from", "portal", "--to", "gateway", "--approve-consequential"})
	if r.ExitCode != 0 || !strings.Contains(rewriteFileText(t, repo.Path("package.json")), `"name": "gateway"`) {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestRewriteUvPathSourcesRefusesAMemberWithoutAManifest(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n")
	hygiene.Chdir(t, repo.Dir)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"rewrite", "uv-path-sources", "--dry-run"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "no pyproject.toml at") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestRewriteUvPathSourcesPlansThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n")
	repo.Write("pyproject.toml", "[project]\nname = \"portal\"\nversion = \"0.1.0\"\ndependencies = [\"core @ file:///abs/core\"]\n")
	repo.Write("uv.lock", "version = 1\n\n[[package]]\nname = \"core\"\nversion = \"1.4.0\"\nsource = { directory = \"/abs/core\" }\n")
	hygiene.Chdir(t, repo.Dir)
	fake := testsupport.NewFakeHTTP(t, httpGet("https://pypi.org/pypi/core/json", 200, `{"info": {"version": "1.4.0"}, "releases": {"1.4.0": []}}`))
	r := appWith(t, fake).Test([]string{"rewrite", "uv-path-sources", "--dry-run"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "core: convert: 1 entry -> core>=1.4.0") || !strings.Contains(r.Stdout, "rlsbl:dep-floors would be switched on") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if strings.Contains(rewriteFileText(t, repo.Path("pyproject.toml")), ">=1.4.0") {
		t.Error("the dry run wrote")
	}
}
