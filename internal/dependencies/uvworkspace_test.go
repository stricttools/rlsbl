package dependencies_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// write writes content to rel under root.
func write(t *testing.T, root, rel, content string) {
	t.Helper()
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), content)
}

// mkdir creates rel under root.
func mkdir(t *testing.T, root, rel string) string {
	t.Helper()
	dir := filepath.Join(root, filepath.FromSlash(rel))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	return dir
}

// resolvedDir is dir with its symlinks resolved, as the locator reports it.
func resolvedDir(t *testing.T, dir string) string {
	t.Helper()
	r, err := filepath.EvalSymlinks(dir)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func workspaceRoot(t *testing.T, members, exclude string) string {
	t.Helper()
	root := t.TempDir()
	text := "[tool.uv.workspace]\nmembers = " + members + "\n"
	if exclude != "" {
		text += "exclude = " + exclude + "\n"
	}
	write(t, root, "pyproject.toml", text)
	return root
}

func findRoot(t *testing.T, dir string) (string, bool) {
	t.Helper()
	root, found, err := dependencies.FindUvWorkspaceRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	return root, found
}

func TestAWorkspaceClaimsALiteralAndAGlobMember(t *testing.T) {
	hygiene.Isolate(t)
	root := workspaceRoot(t, `["core", "packages/*"]`, "")
	for _, rel := range []string{"core", "packages/widget"} {
		got, found := findRoot(t, mkdir(t, root, rel))
		if !found || got != resolvedDir(t, root) {
			t.Errorf("%s: got %q, %v", rel, got, found)
		}
	}
}

func TestADirectoryNoGlobClaimsOrAnExcludeGlobClaimsIsNoMember(t *testing.T) {
	hygiene.Isolate(t)
	root := workspaceRoot(t, `["packages/*"]`, `["packages/legacy"]`)
	for _, rel := range []string{"tools/gadget", "packages/legacy", "packages/widget/deep"} {
		if got, found := findRoot(t, mkdir(t, root, rel)); found {
			t.Errorf("%s was claimed by %s", rel, got)
		}
	}
}

func TestARecursiveGlobCrossesDirectories(t *testing.T) {
	hygiene.Isolate(t)
	root := workspaceRoot(t, `["packages/**"]`, "")
	if _, found := findRoot(t, mkdir(t, root, "packages/group/widget")); !found {
		t.Error("packages/** did not claim packages/group/widget")
	}
}

func TestTheFirstDeclaringAncestorDecidesAndTheRootIsNotItsOwnMember(t *testing.T) {
	hygiene.Isolate(t)
	outer := workspaceRoot(t, `["inner/*"]`, "")
	write(t, outer, "inner/pyproject.toml", "[tool.uv.workspace]\nmembers = [\"elsewhere\"]\n")
	if got, found := findRoot(t, mkdir(t, outer, "inner/widget")); found {
		t.Errorf("a directory the nearest workspace does not claim was claimed by %s", got)
	}
	if got, found := findRoot(t, outer); found {
		t.Errorf("the workspace root claimed itself: %s", got)
	}
	plain := t.TempDir()
	if got, found := findRoot(t, mkdir(t, plain, "a/b")); found {
		t.Errorf("no workspace above, yet %s claimed it", got)
	}
}

func TestAnAncestorThatDoesNotParseIsAnErrorNamingIt(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	write(t, root, "pyproject.toml", "[tool.uv.workspace\n")
	_, _, err := dependencies.FindUvWorkspaceRoot(mkdir(t, root, "widget"))
	if err == nil || !strings.Contains(err.Error(), filepath.Join(resolvedDir(t, root), "pyproject.toml")) {
		t.Fatalf("got %v", err)
	}
}

func TestLocateUvLock(t *testing.T) {
	hygiene.Isolate(t)
	root := workspaceRoot(t, `["packages/*"]`, `["packages/legacy"]`)
	member := mkdir(t, root, "packages/widget")

	search, err := dependencies.LocateUvLock(member)
	if err != nil || search.Location != nil {
		t.Fatalf("no lock anywhere: %+v, %v", search, err)
	}
	if !strings.Contains(search.Probed, "(absent) and") || !strings.Contains(search.Probed, filepath.Join(resolvedDir(t, root), "uv.lock")) {
		t.Errorf("the refusal names one location: %s", search.Probed)
	}

	write(t, root, "uv.lock", "version = 1\n")
	search, err = dependencies.LocateUvLock(member)
	if err != nil || search.Location == nil || search.Location.WorkspaceRoot != resolvedDir(t, root) {
		t.Fatalf("the member did not reach the root lock: %+v, %v", search, err)
	}
	if !strings.Contains(search.Location.Describe(member), "claims this directory as a member") {
		t.Errorf("describe: %s", search.Location.Describe(member))
	}

	write(t, member, "uv.lock", "version = 1\n")
	search, _ = dependencies.LocateUvLock(member)
	if search.Location == nil || search.Location.WorkspaceRoot != "" || search.Location.Path != filepath.Join(member, "uv.lock") {
		t.Errorf("the lock beside the manifest did not win: %+v", search.Location)
	}

	excluded := mkdir(t, root, "packages/legacy")
	search, _ = dependencies.LocateUvLock(excluded)
	if search.Location != nil || !strings.Contains(search.Probed, "no ancestor declares") {
		t.Errorf("an excluded directory reached a lock: %+v", search)
	}
}

func TestAnUnreadableLockIsStillTheLocation(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	write(t, dir, "uv.lock", "this is not toml [")
	search, err := dependencies.LocateUvLock(dir)
	if err != nil || search.Location == nil {
		t.Fatalf("%+v, %v", search, err)
	}
	if _, _, err := dependencies.PypiLocked(search.Location.Path); err == nil {
		t.Error("an unparseable lock read as a lock")
	}
}

func TestIsVirtualUvRoot(t *testing.T) {
	hygiene.Isolate(t)
	virtual := workspaceRoot(t, `["packages/*"]`, "")
	project := t.TempDir()
	write(t, project, "pyproject.toml", "[project]\nname = \"widget\"\n\n[tool.uv.workspace]\nmembers = []\n")
	for dir, want := range map[string]bool{virtual: true, project: false, t.TempDir(): false} {
		got, err := dependencies.IsVirtualUvRoot(dir)
		if err != nil || got != want {
			t.Errorf("%s: %v, %v", dir, got, err)
		}
	}
}
