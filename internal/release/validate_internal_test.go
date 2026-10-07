package release

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

const selectionDeclarations = `format_version = 1
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
path = "widget"
name = "widget"
releasable = "widget"

[[members]]
path = "gadget"
name = "gadget"
releasable = "gadget"
`

func selectionWorkspace(t *testing.T) *workspace.Workspace {
	t.Helper()
	root := t.TempDir()
	path := filepath.Join(root, filepath.FromSlash(declarations.ReleasablesFile))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(selectionDeclarations), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, d := range []string{"widget/src", "gadget"} {
		if err := os.MkdirAll(filepath.Join(root, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	ws, err := workspace.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	return ws
}

func TestTheWorkingDirectorySelectsTheReleasable(t *testing.T) {
	hygiene.Isolate(t)
	ws := selectionWorkspace(t)
	r, m, err := selectRelease(ws, Request{LiveRoot: ws.Root, Dir: filepath.Join(ws.Root, "widget", "src")})
	if err != nil || r.Name != "widget" || m.Name != "widget" {
		t.Fatalf("%v %v %v", r.Name, m.Name, err)
	}
	_, _, err = selectRelease(ws, Request{LiveRoot: ws.Root, Dir: filepath.Join(ws.Root, "widget"), Releasable: "gadget"})
	if err == nil || !strings.Contains(err.Error(), "--releasable gadget is refused here") {
		t.Errorf("--releasable in a member was accepted: %v", err)
	}
}

func TestAWorkspaceRootNeedsReleasableUntilItIsNamed(t *testing.T) {
	hygiene.Isolate(t)
	ws := selectionWorkspace(t)
	_, _, err := selectRelease(ws, Request{LiveRoot: ws.Root, Dir: ws.Root})
	if err == nil || !strings.Contains(err.Error(), "name it with --releasable (widget, gadget)") {
		t.Fatalf("the root selected a releasable: %v", err)
	}
	_, _, err = selectRelease(ws, Request{LiveRoot: ws.Root, Dir: ws.Root, Releasable: "portal"})
	if err == nil || !strings.Contains(err.Error(), "names no releasable") {
		t.Errorf("an undeclared releasable was accepted: %v", err)
	}
	// The fix the refusal names.
	r, m, err := selectRelease(ws, Request{LiveRoot: ws.Root, Dir: ws.Root, Releasable: "gadget"})
	if err != nil || r.Name != "gadget" || m.Name != "gadget" {
		t.Errorf("%v %v %v", r.Name, m.Name, err)
	}
}

func TestACIRouterThatCannotBeReadRefusesTheCandidateWindow(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(declarations.ReleasablesFile, selectionDeclarations)
	repo.Write("widget/a.txt", "a\n")
	repo.Write("gadget/a.txt", "a\n")
	base := repo.Commit("the workspace", declarations.ReleasablesFile, "widget/a.txt", "gadget/a.txt")
	candidate := repo.CommitFile("gadget/b.txt", "b\n", "a gadget change")
	routerDir := repo.Path(workflows.Dir)
	repo.Write(workflows.Dir+"/"+workflows.RouterFile, "name: router\n")
	if err := os.Chmod(routerDir, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(routerDir, 0o755) })
	ws, err := workspace.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	v, err := semver.Parse("0.2.0")
	if err != nil {
		t.Fatal(err)
	}
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		_, err = judgeCandidateWindow(r, ws, "widget", candidate, base, true, true, nil, v, "widget@v0.2.0", "main", RerunResume)
		if err == nil || !strings.Contains(err.Error(), "the CI router "+workflows.Dir+"/"+workflows.RouterFile+" cannot be read") {
			t.Errorf("a router that cannot be read was taken for no router: %v", err)
		}
		return nil
	})
}
