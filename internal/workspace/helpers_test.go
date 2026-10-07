package workspace

import (
	"path/filepath"
	"testing"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// nested is a workspace with a dev-node root, a member holding a nested
// member, and a top-level go member: the shapes ownership and tags turn on.
const nested = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "draw"
tag_format = "draw/v{version}"
publish_mode = "none"

[[releasables]]
name = "cmd"
tag_format = "draw/cmd/v{version}"
publish_mode = "none"

[[releasables]]
name = "kernel"
tag_format = "{name}@v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "draw"
name = "draw"
releasable = "draw"

[[members]]
path = "draw/cmd"
name = "cmd"
releasable = "cmd"

[[members]]
path = "kernel"
name = "kernel"
releasable = "kernel"
`

// standalone is a standalone repository.
const standalone = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]

[[releasables]]
name = "gadget"
tag_format = "v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
releasable = "gadget"
`

// newWorkspace parses text and builds the model of a repository at root.
func newWorkspace(t *testing.T, root, text string) *Workspace {
	t.Helper()
	d, err := declarations.Parse([]byte(text))
	if err != nil {
		t.Fatalf("Parse refused:\n%s\n\n%v", text, err)
	}
	w, err := New(root, d)
	if err != nil {
		t.Fatal(err)
	}
	return w
}

// writeFixture writes a file of the repository, the world a test starts
// from, outside any effects handle.
func writeFixture(t *testing.T, root, rel, content string) {
	t.Helper()
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), content)
}
