package workflows

import (
	"os"
	"path/filepath"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

func TestMain(m *testing.M) {
	os.Exit(testsupport.RunTests(m))
}

// threeMembers is a workspace whose dev-node root holds two members, web
// depending on core, each versioned under a releasable of its own.
const threeMembers = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "core"
tag_format = "{name}@v{version}"
publish_mode = "ci"

[[releasables]]
name = "web"
tag_format = "{name}@v{version}"
publish_mode = "ci"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "packages/core"
name = "core"
releasable = "core"

[[members]]
path = "apps/web"
name = "web"
releasable = "web"
depends_on = ["core"]
`

// fixtureWorkspace writes files (repository-relative path to content) under
// a fresh directory, the declarations included, and builds its model.
func fixtureWorkspace(t *testing.T, decls string, files map[string]string) *workspace.Workspace {
	t.Helper()
	root := filepath.Join(t.TempDir(), "repo")
	all := map[string]string{declarations.ReleasablesFile: decls}
	for k, v := range files {
		all[k] = v
	}
	for rel, content := range all {
		testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), content)
	}
	d, err := declarations.Parse([]byte(decls))
	if err != nil {
		t.Fatalf("the declarations are refused:\n%s\n\n%v", decls, err)
	}
	w, err := workspace.New(root, d)
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func member(t *testing.T, w *workspace.Workspace, name string) declarations.Member {
	t.Helper()
	m, ok := w.Declarations.Member(name)
	if !ok {
		t.Fatalf("no member %q", name)
	}
	return m
}

// parseYAML reads generated YAML into plain values, failing the test when
// it is not YAML.
func parseYAML(t *testing.T, text string) map[string]any {
	t.Helper()
	var doc map[string]any
	if err := yaml.Unmarshal([]byte(text), &doc); err != nil {
		t.Fatalf("the generated text is not YAML: %v\n%s", err, text)
	}
	return doc
}

// jobsOf is the jobs mapping of a parsed workflow.
func jobsOf(t *testing.T, doc map[string]any) map[string]any {
	t.Helper()
	jobs, ok := doc["jobs"].(map[string]any)
	if !ok {
		t.Fatalf("the workflow has no jobs mapping: %v", doc)
	}
	return jobs
}

// job is one job of a parsed workflow.
func job(t *testing.T, doc map[string]any, key string) map[string]any {
	t.Helper()
	j, ok := jobsOf(t, doc)[key].(map[string]any)
	if !ok {
		t.Fatalf("the workflow has no job %q; its jobs are %v", key, keysOf(jobsOf(t, doc)))
	}
	return j
}

func keysOf(m map[string]any) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	return out
}
