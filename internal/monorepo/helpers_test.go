package monorepo

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

// today is the date the commands in these tests run on.
var today = time.Date(2026, 10, 7, 0, 0, 0, 0, time.UTC)

// topicsAnswer is the gh answer a scaffold of acme/portal asks for: the
// repository's topics, which hold rlsbl already, so none is written.
var topicsAnswer = testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/acme/portal/topics"}, Stdout: `{"names":["rlsbl"]}`}

// twoMembers declares a workspace on GitHub as acme/portal: a dev-node
// root, widget at packages/widget publishing from CI, and gadget at
// apps/gadget publishing nothing, each versioned under a releasable of its
// own.
const twoMembers = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "widget"
tag_format = "{name}@v{version}"
publish_mode = "ci"

[[releasables]]
name = "gadget"
tag_format = "{name}@v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "packages/widget"
name = "widget"
releasable = "widget"

[[members]]
path = "apps/gadget"
name = "gadget"
releasable = "gadget"
`

// twoMemberFiles are the npm packages of twoMembers: gadget depends on
// widget at ^0.1.0, and widget is at 0.1.0.
var twoMemberFiles = map[string]string{
	"packages/widget/package.json":               "{\n  \"name\": \"widget\",\n  \"version\": \"0.1.0\"\n}\n",
	"apps/gadget/package.json":                   "{\n  \"name\": \"gadget\",\n  \"version\": \"0.2.0\",\n  \"dependencies\": {\"widget\": \"^0.1.0\"}\n}\n",
	declarations.ReleaseStateDir + "/.gitignore": "*\n!.gitignore\n",
}

// newWorkspace is a repository holding the declarations and files, all
// committed, the working directory for the rest of the test.
func newWorkspace(t *testing.T, decls string, files map[string]string) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(declarations.ReleasablesFile, decls)
	paths := []string{declarations.ReleasablesFile}
	for rel, content := range files {
		repo.Write(rel, content)
		paths = append(paths, rel)
	}
	repo.Commit("the workspace", paths...)
	hygiene.Chdir(t, repo.Dir)
	return repo
}

// load reads the repository's workspace.
func load(t *testing.T, repo *testsupport.Repo) *workspace.Workspace {
	t.Helper()
	ws, err := workspace.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	return ws
}

// run runs fn with the effects handle of a throwaway command of the given
// classification, and returns what the command printed with what fn said.
func run(t *testing.T, effect string, dryRun bool, fn func(e *strictcli.Effects, say func(string)) error) (strictcli.Result, string) {
	t.Helper()
	var said []string
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: effect, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		return fn(ctx.Effects(), func(s string) { said = append(said, s) })
	})
	return r, strings.Join(said, "\n") + "\n" + r.Stdout + r.Stderr
}

// mustRun is run for a command that must succeed.
func mustRun(t *testing.T, effect string, dryRun bool, fn func(e *strictcli.Effects, say func(string)) error) string {
	t.Helper()
	r, text := run(t, effect, dryRun, fn)
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, text)
	}
	return text
}

// mustFail is run for a command that must fail with every one of wants in
// what it printed.
func mustFail(t *testing.T, effect string, dryRun bool, fn func(e *strictcli.Effects, say func(string)) error, wants ...string) string {
	t.Helper()
	r, text := run(t, effect, dryRun, fn)
	if r.ExitCode == 0 {
		t.Fatalf("the command succeeded:\n%s", text)
	}
	for _, w := range wants {
		if !strings.Contains(text, w) {
			t.Errorf("the output lacks %q:\n%s", w, text)
		}
	}
	return text
}

// openRepo is a git handle on the repository under the effects handle.
func openRepo(t *testing.T, e *strictcli.Effects, dir string) git.Repo {
	t.Helper()
	repo, err := git.Open(e, dir)
	if err != nil {
		t.Fatal(err)
	}
	return repo
}

// readText is the content of rel in the repository; "" when it is missing.
func readText(t *testing.T, repo *testsupport.Repo, rel string) string {
	t.Helper()
	data, err := os.ReadFile(repo.Path(rel))
	if os.IsNotExist(err) {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// exists reports whether rel exists in the repository.
func exists(repo *testsupport.Repo, rel string) bool {
	_, err := os.Lstat(repo.Path(rel))
	return err == nil
}

// deletingSaferm puts a saferm first on PATH for the rest of the test that
// records its argv and deletes its last argument, so a test can see what a
// deletion leaves behind.
func deletingSaferm(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	calls := filepath.Join(dir, "saferm-calls.txt")
	script := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"" + calls + "\"\nfor last in \"$@\"; do :; done\n/bin/rm -rf -- \"$last\"\n"
	if err := os.WriteFile(filepath.Join(dir, "saferm"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return calls
}

// commitCount is the number of commits HEAD reaches.
func commitCount(repo *testsupport.Repo) string {
	return repo.Git("rev-list", "--count", "HEAD")
}
