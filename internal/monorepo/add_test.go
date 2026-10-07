package monorepo

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workflows"
)

// rootAndWidget declares a workspace on GitHub as acme/portal whose dev-node
// root holds widget at packages/widget, versioned under widget, which
// publishes nothing.
const rootAndWidget = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "widget"
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
`

// addFixture is a workspace of rootAndWidget with a Go module at
// apps/gadget not declared yet, a fake gh answering the scaffold's topic
// question, and a fake safegit.
func addFixture(t *testing.T) *testsupport.Repo {
	t.Helper()
	testsupport.FakeSafegit(t)
	testsupport.FakeGH(t, topicsAnswer)
	return newWorkspace(t, rootAndWidget, addFiles)
}

// addFiles are the Go modules of the add fixture.
var addFiles = map[string]string{
	"packages/widget/go.mod":                     "module github.com/acme/widget\n\ngo 1.26\n",
	"packages/widget/main.go":                    "package main\n\nfunc main() {}\n",
	"apps/gadget/go.mod":                         "module github.com/acme/gadget\n\ngo 1.26\n",
	"apps/gadget/main.go":                        "package main\n\nfunc main() {}\n",
	declarations.ReleaseStateDir + "/.gitignore": "*\n!.gitignore\n",
}

// gadgetRequest adds apps/gadget under a new releasable gadget.
func gadgetRequest(repo *testsupport.Repo) AddRequest {
	return AddRequest{
		Dir:         repo.Dir,
		Path:        "apps/gadget",
		Releasable:  "gadget",
		TagFormat:   "{name}@v{version}",
		PublishMode: "none",
		AutoCommit:  true,
		Version:     "0.132.0",
		Now:         today,
	}
}

func adding(req AddRequest) func(e *strictcli.Effects, say func(string)) error {
	return func(e *strictcli.Effects, say func(string)) error {
		gh, err := github.New(e)
		if err != nil {
			return err
		}
		req.GitHub, req.Say = gh, say
		return Add(e, req)
	}
}

func TestAddDeclaresScaffoldsAndCommitsAsOneCommit(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	before := repo.Head()
	text := mustRun(t, strictcli.EffectMutating, false, adding(gadgetRequest(repo)))
	ws := load(t, repo)
	m, ok := ws.Declarations.Member("gadget")
	r, rok := ws.Declarations.Releasable("gadget")
	if !ok || m.Path != "apps/gadget" || m.Releasable != "gadget" || !rok || r.TagFormat != "{name}@v{version}" || r.PublishMode != declarations.PublishNone {
		t.Fatalf("declarations: %+v", ws.Declarations)
	}
	if !exists(repo, "apps/gadget/.github/workflows/ci.yml") || !exists(repo, workflows.RouterPath) {
		t.Fatalf("the member was not scaffolded or the router not written:\n%s", text)
	}
	if got := repo.Git("rev-list", "--count", before+"..HEAD"); got != "1" {
		t.Fatalf("the add made %s commits", got)
	}
	if subject := repo.Git("log", "-1", "--format=%s"); subject != AddCommitMessage("gadget") {
		t.Errorf("the last commit is %q", subject)
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("the add left changes uncommitted:\n%s", status)
	}
	if strings.Contains(text, scaffoldNotCommitted) {
		t.Errorf("the add passed on the scaffold's own commit line:\n%s", text)
	}
}

func TestAddJoiningADeclaredReleasableAddsNoReleasable(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	req := gadgetRequest(repo)
	req.Releasable, req.TagFormat, req.PublishMode = "widget", "", ""
	mustRun(t, strictcli.EffectMutating, false, adding(req))
	ws := load(t, repo)
	if len(ws.Releasables()) != 1 || len(ws.MembersOf("widget")) != 2 {
		t.Fatalf("declarations: %+v", ws.Declarations)
	}
}

func TestAddWithoutAutoCommitLeavesEverythingUncommitted(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	before := commitCount(repo)
	req := gadgetRequest(repo)
	req.AutoCommit = false
	text := mustRun(t, strictcli.EffectMutating, false, adding(req))
	if commitCount(repo) != before || !strings.Contains(text, "Not committed (--no-auto-commit): ") || !strings.Contains(text, declarations.ReleasablesFile) {
		t.Fatalf("commits %s -> %s:\n%s", before, commitCount(repo), text)
	}
}

func TestADryRunAddWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	before := readText(t, repo, declarations.ReleasablesFile)
	text := mustRun(t, strictcli.EffectMutating, true, adding(gadgetRequest(repo)))
	if readText(t, repo, declarations.ReleasablesFile) != before || exists(repo, "apps/gadget/.github") {
		t.Fatal("the dry run wrote")
	}
	if !strings.Contains(text, `Would declare the releasable "gadget"`) {
		t.Errorf("the preview does not name the releasable it creates:\n%s", text)
	}
}

// A commit that fails puts every path the add changed back, and the add
// succeeds once what the commit reported is fixed.
func TestAFailedAddPutsTheWorkingTreeBack(t *testing.T) {
	hygiene.Isolate(t)
	repo := newWorkspace(t, rootAndWidget, addFiles)
	testsupport.PathOnly(t, "git")
	testsupport.FakeGH(t, topicsAnswer)
	deletingSaferm(t)
	declared := readText(t, repo, declarations.ReleasablesFile)
	mustFail(t, strictcli.EffectMutating, false, adding(gadgetRequest(repo)), "safegit is not on PATH", `the member "gadget" is not added`, "run this `rlsbl monorepo add` again")
	if readText(t, repo, declarations.ReleasablesFile) != declared {
		t.Error("the declarations were not put back")
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("the failed add left changes behind:\n%s", status)
	}
	testsupport.FakeSafegit(t)
	mustRun(t, strictcli.EffectMutating, false, adding(gadgetRequest(repo)))
	if _, ok := load(t, repo).Declarations.Member("gadget"); !ok {
		t.Fatal("the second add did not declare the member")
	}
}

// A path that had uncommitted changes before the add and that the add
// writes is left uncommitted and named; the rest is committed.
func TestAPathChangedBeforeTheAddIsLeftUncommitted(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	repo.Write("apps/gadget/.gitignore", "local-notes.txt\n")
	text := mustRun(t, strictcli.EffectMutating, false, adding(gadgetRequest(repo)))
	if !strings.Contains(text, "Left uncommitted") || !strings.Contains(text, "apps/gadget/.gitignore") {
		t.Fatalf("the touched path was not named:\n%s", text)
	}
	if status := repo.Git("status", "--porcelain"); !strings.Contains(status, "apps/gadget/.gitignore") {
		t.Errorf("the touched path was committed:\n%s", status)
	}
}

// Each refusal is made before anything is written, and the request the
// refusal names succeeds (checked by a dry run, which previews and stops).
func TestAddRefusals(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	declared := readText(t, repo, declarations.ReleasablesFile)
	cases := []struct {
		name   string
		change func(*AddRequest)
		wants  []string
		fix    func(*AddRequest)
	}{
		{"a path that is not canonical", func(r *AddRequest) { r.Path = "apps/gadget/" }, []string{"not canonical", `Write it as "apps/gadget"`},
			func(r *AddRequest) { r.Path = "apps/gadget" }},
		{"the repository root", func(r *AddRequest) { r.Path = "." }, []string{"every repository declares already"}, nil},
		{"a declared member's path", func(r *AddRequest) { r.Path = "packages/widget" }, []string{`the member "widget" is already declared`}, nil},
		{"a missing directory", func(r *AddRequest) { r.Path = "apps/portal" }, []string{"apps/portal is not a directory"}, nil},
		{"a taken name", func(r *AddRequest) { r.Name = "widget" }, []string{`already named "widget"`, "--name"},
			func(r *AddRequest) { r.Name = "gadget" }},
		{"the root's name", func(r *AddRequest) { r.Name = declarations.RootName }, []string{"reserved for the root member"},
			func(r *AddRequest) { r.Name = "" }},
		{"an unknown dependency", func(r *AddRequest) { r.DependsOn = []string{"portal"} }, []string{`--depends-on names "portal"`, "root, widget"},
			func(r *AddRequest) { r.DependsOn = []string{"widget"} }},
		{"no releasable", func(r *AddRequest) { r.Releasable = "" }, []string{"--releasable is required", "--releasable false"},
			func(r *AddRequest) { r.Releasable = NoReleasable; r.TagFormat, r.PublishMode = "", "" }},
		{"a tag format for no releasable", func(r *AddRequest) { r.Releasable = NoReleasable }, []string{"--releasable false versions the member under none"},
			func(r *AddRequest) { r.Releasable = NoReleasable; r.TagFormat, r.PublishMode = "", "" }},
		{"a tag format for a declared releasable", func(r *AddRequest) { r.Releasable = "widget" }, []string{`the releasable "widget" is declared already`},
			func(r *AddRequest) { r.Releasable = "widget"; r.TagFormat, r.PublishMode = "", "" }},
		{"a created releasable without its tag format and publish mode", func(r *AddRequest) { r.TagFormat, r.PublishMode = "", "" }, []string{"pass --tag-format", "and --publish-mode"},
			func(r *AddRequest) { r.TagFormat, r.PublishMode = "{name}@v{version}", "none" }},
		{"a tag format without its version", func(r *AddRequest) { r.TagFormat = "gadget" }, []string{"--tag-format", "{version}"},
			func(r *AddRequest) { r.TagFormat = "gadget/v{version}" }},
		{"an unknown target", func(r *AddRequest) { r.Target = "zig" }, []string{"--target", `"zig" is not a release target`},
			func(r *AddRequest) { r.Target = "go" }},
	}
	for _, c := range cases {
		req := gadgetRequest(repo)
		c.change(&req)
		mustFail(t, strictcli.EffectMutating, false, adding(req), c.wants...)
		if readText(t, repo, declarations.ReleasablesFile) != declared {
			t.Fatalf("%s: the refused add wrote the declarations", c.name)
		}
		if c.fix == nil {
			continue
		}
		fixed := gadgetRequest(repo)
		c.change(&fixed)
		c.fix(&fixed)
		mustRun(t, strictcli.EffectMutating, true, adding(fixed))
	}
}

// A directory without a manifest has no target to detect; naming one with
// --target clears the refusal.
func TestAddRefusesADirectoryWithoutATargetUntilOneIsNamed(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	repo.Write("docs/README.md", "the docs\n")
	req := gadgetRequest(repo)
	req.Path, req.Releasable, req.TagFormat, req.PublishMode = "docs", NoReleasable, "", ""
	mustFail(t, strictcli.EffectMutating, true, adding(req), "no release target is detected in docs", "--target")
	req.Target = "npm"
	mustRun(t, strictcli.EffectMutating, true, adding(req))
}

// The workspace's commands refuse a standalone project; declaring the
// workspace layout clears the refusal.
func TestAStandaloneRepositoryIsRefusedUntilItDeclaresTheWorkspaceLayout(t *testing.T) {
	hygiene.Isolate(t)
	standalone := "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n"
	repo := newWorkspace(t, standalone, nil)
	if _, err := Load(repo.Dir, "list"); err == nil || !strings.Contains(err.Error(), `repository_layout = "workspace"`) {
		t.Fatalf("err %v", err)
	}
	repo.Write(declarations.ReleasablesFile, strings.Replace(standalone, `"standalone"`, `"workspace"`, 1))
	if _, err := Load(repo.Dir, "list"); err != nil {
		t.Fatal(err)
	}
}
