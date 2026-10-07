package cli

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// monorepoWorkspace declares a dev-node root, widget at packages/widget,
// and gadget at apps/gadget depending on widget, each versioned under a
// releasable of its own that publishes nothing.
const monorepoWorkspace = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "widget"
tag_format = "{name}@v{version}"
publish_mode = "none"

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
library = true

[[members]]
path = "apps/gadget"
name = "gadget"
releasable = "gadget"
`

// monorepoFixture is a repository of monorepoWorkspace with npm packages,
// committed, the working directory for the rest of the test.
func monorepoFixture(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	files := map[string]string{
		declarations.ReleasablesFile:   monorepoWorkspace,
		"packages/widget/package.json": "{\n  \"name\": \"widget\",\n  \"version\": \"0.1.0\"\n}\n",
		"apps/gadget/package.json":     "{\n  \"name\": \"gadget\",\n  \"version\": \"0.2.0\",\n  \"dependencies\": {\"widget\": \"^0.1.0\"}\n}\n",
	}
	var paths []string
	for rel, content := range files {
		repo.Write(rel, content)
		paths = append(paths, rel)
	}
	repo.Commit("the workspace", paths...)
	hygiene.Chdir(t, repo.Dir)
	return repo
}

func TestMonorepoInitThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("README.md", "portal\n", "the first commit")
	hygiene.Chdir(t, repo.Dir)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	if r := app.Test([]string{"monorepo", "init", "--root-member", "root-dev-node", "--release-branch", "main", "--dry-run"}); r.ExitCode == 0 {
		t.Fatalf("init accepted --dry-run:\n%s", r.Stdout)
	}
	if r := app.Test([]string{"monorepo", "init", "--root-member", "root-releasable", "--release-branch", "main"}); r.ExitCode == 0 || !strings.Contains(r.Stderr, "releasable") {
		t.Fatalf("a root releasable without its name: exit %d: %s", r.ExitCode, r.Stderr)
	}
	r := app.Test([]string{"monorepo", "init", "--root-member", "root-releasable", "--releasable", "portal", "--tag-format", "v{version}", "--publish-mode", "none", "--release-branch", "main"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Committed: monorepo: init workspace") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	d, err := declarations.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if d.RootMember().Releasable != "portal" || d.ReleaseBranches[0] != "main" {
		t.Fatalf("declarations %+v", d)
	}
}

func TestMonorepoListThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	monorepoFixture(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"monorepo", "list"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Name    Path             Releasable  Flags") || !strings.Contains(r.Stdout, "widget  packages/widget  widget      library") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	members := jsonPayload(t, app.Test([]string{"monorepo", "list", "--json"}))["members"].([]any)
	if len(members) != 3 || members[0].(map[string]any)["releasable"] != nil || members[1].(map[string]any)["library"] != true {
		t.Fatalf("members %v", members)
	}
}

func TestMonorepoGraphThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	repo := monorepoFixture(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"monorepo", "graph", "--format", "tree"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "gadget [leaf]\n  widget [lib]\n") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	payload := jsonPayload(t, app.Test([]string{"monorepo", "graph", "--format", "dot", "--json"}))
	order := payload["order"].([]any)
	if len(order) != 3 || order[2] != "gadget" || payload["format"] != "dot" || payload["output"] != nil {
		t.Fatalf("payload %v", payload)
	}
	edges := payload["edges"].([]any)
	if len(edges) != 1 || edges[0].(map[string]any)["from"] != "gadget" {
		t.Fatalf("edges %v", edges)
	}
	r = app.Test([]string{"monorepo", "graph", "--format", "dot", "--output", "graph.dot"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Wrote the graph to graph.dot") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	data, err := os.ReadFile(repo.Path("graph.dot"))
	if err != nil || !strings.HasPrefix(string(data), "digraph dependencies {") {
		t.Fatalf("graph.dot: %v\n%s", err, data)
	}
	if r := app.Test([]string{"monorepo", "graph", "--format", "tree", "--root", "gadget", "--depth=-1"}); r.ExitCode != 1 || !strings.Contains(r.Stderr, "--depth must not be negative") {
		t.Fatalf("a negative depth: exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestMonorepoImpactAndOutdatedThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	monorepoFixture(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"monorepo", "impact", "widget"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Impact of a change to: widget") || !strings.Contains(r.Stdout, "Direct dependents (1):\n  gadget") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if r := app.Test([]string{"monorepo", "impact"}); r.ExitCode != 1 || !strings.Contains(r.Stderr, "name what changed") {
		t.Fatalf("no change named: exit %d: %s", r.ExitCode, r.Stderr)
	}
	r = app.Test([]string{"monorepo", "outdated"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "gadget  widget      ^0.1.0      0.1.0    ok") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestMonorepoCheckNamesExitsAsCheckNameDoes(t *testing.T) {
	hygiene.Isolate(t)
	monorepoFixture(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"monorepo", "check-names", "--target", "go"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "widget  widget        available") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"monorepo", "check-names", "--target", "go", "--prefix", "x_", "--json"})
	if r.ExitCode != 1 {
		t.Fatalf("a discouraged name: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	results := jsonPayload(t, r)["results"].([]any)
	first := results[0].(map[string]any)
	if first["checked_name"] != "x_widget" || first["result"].(map[string]any)["status"] != "discouraged" {
		t.Fatalf("results %v", results)
	}
}

func TestMonorepoRemoveAndCleanupThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	repo := monorepoFixture(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	if r := app.Test([]string{"monorepo", "remove", "apps/gadget", "--dry-run"}); r.ExitCode == 0 {
		t.Fatal("remove accepted --dry-run")
	}
	if r := app.Test([]string{"monorepo", "remove", "apps/gadget"}); r.ExitCode != 1 || !strings.Contains(r.Stderr, `the only member versioned under the releasable "gadget"`) {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	testsupport.FakeSaferm(t)
	repo.Write("packages/widget/CHANGELOG.md", "# Changelog\n")
	r := app.Test([]string{"monorepo", "cleanup", "--dry-run"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Would remove packages/widget/CHANGELOG.md") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestMonorepoRenameReleasablePreviewsThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	monorepoFixture(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"monorepo", "rename-releasable", "widget", "portal", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "no open releasable-name identity") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestTheMonorepoCommandsRefuseAStandaloneRepository(t *testing.T) {
	hygiene.Isolate(t)
	scaffoldGoProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	for _, argv := range [][]string{{"monorepo", "list"}, {"monorepo", "status"}, {"monorepo", "graph", "--format", "tree"}} {
		if r := app.Test(argv); r.ExitCode != 1 || !strings.Contains(r.Stderr, `repository_layout = "standalone"`) {
			t.Errorf("%v: exit %d: %s", argv, r.ExitCode, r.Stderr)
		}
	}
}
