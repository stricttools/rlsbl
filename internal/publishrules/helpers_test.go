package publishrules_test

import (
	"archive/zip"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
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

// today is the date every rule is asked about.
var today = time.Date(2026, 10, 7, 0, 0, 0, 0, time.UTC)

// publicRecord is a record whose every license is public.
const publicRecord = `format_version = 1

[[licenses]]
subject = "portal"
license = "MIT"
from = 2026-01-01
reason = "published client"
`

// confidentialRecord is a record whose portal license is proprietary.
const confidentialRecord = `format_version = 1

[[licenses]]
subject = "portal"
license = "proprietary"
from = 2026-01-01
reason = "the server's logic"
`

func record(t *testing.T, src string) *lifecycle.Record {
	t.Helper()
	r, err := lifecycle.Parse([]byte(src))
	if err != nil {
		t.Fatalf("parsing the record: %v", err)
	}
	return r
}

// declarationsHead is a workspace whose root is a dev node and whose widget
// member is versioned under a releasable of its own; each fixture appends
// the portal releasable and member.
const declarationsHead = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "widget"
tag_format = "widget/v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "widget"
name = "widget"
releasable = "widget"
`

// portalDeclarations declares the portal releasable with the given publish
// mode and its member at portal with the given targets and pipelines.
func portalDeclarations(publishMode, member string) string {
	return declarationsHead + `
[[releasables]]
name = "portal"
tag_format = "portal/v{version}"
publish_mode = "` + publishMode + `"

[[members]]
path = "portal"
name = "portal"
releasable = "portal"
` + member
}

func newWorkspace(t *testing.T, root, text string) *workspace.Workspace {
	t.Helper()
	d, err := declarations.Parse([]byte(text))
	if err != nil {
		t.Fatalf("Parse refused:\n%s\n\n%v", text, err)
	}
	w, err := workspace.New(root, d)
	if err != nil {
		t.Fatal(err)
	}
	return w
}

// writeWheel writes a wheel at path holding entries (name to content).
func writeWheel(t *testing.T, path string, entries map[string]string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	z := zip.NewWriter(f)
	for name, content := range entries {
		w, err := z.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := z.Close(); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
}

// run runs fn with the effects handle of a command of the given
// classification, rlsbl's observe allowlist installed.
func run(t *testing.T, effect string, fn func(e *strictcli.Effects) error) {
	t.Helper()
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: effect, Allowlist: previewapply.Prefixes()}, fn)
}

const pypiPipeline = `targets = [{ name = "pypi" }]

[[members.pipelines]]
name = "pypi"
type = "pypi"
target = "pypi"
local = false
artifact = "package"
`

const goBinaryPipeline = `targets = [{ name = "go" }]

[[members.pipelines]]
name = "go"
type = "go"
target = "go"
local = false
artifact = "binary"
install_paths = ["./cmd/portal"]
`
