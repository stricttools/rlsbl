package workspace

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func residuePaths(t *testing.T, w *Workspace, tracked []string) (paths []string, removable map[string]bool) {
	t.Helper()
	found, err := w.DetectResidue(tracked)
	if err != nil {
		t.Fatal(err)
	}
	removable = map[string]bool{}
	for _, r := range found {
		if r.Reason == "" {
			t.Errorf("%s carries no reason", r.Path)
		}
		paths = append(paths, r.Path)
		removable[r.Path] = r.CleanupRemoves
	}
	return paths, removable
}

func TestResidueNamesEveryOldLayoutDirectory(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeFixture(t, root, ".rlsbl-monorepo/workspace.toml", "[[projects]]\n")
	writeFixture(t, root, ".rlsbl/config.json", "{}\n")
	writeFixture(t, root, "draw/.rlsbl/hooks/pre-release.sh", "true\n")
	writeFixture(t, root, "draw/cmd/.rlsbl/version", "0.1.0\n")
	writeFixture(t, root, "kernel/CHANGELOG.md", "# Changelog\n")
	writeFixture(t, root, "CHANGELOG.md", "# Changelog\n")
	w := newWorkspace(t, root, nested)
	paths, removable := residuePaths(t, w, []string{"vendor/old/.rlsbl/config.json", "README.md"})
	want := []string{".rlsbl", ".rlsbl-monorepo", "draw/.rlsbl", "draw/cmd/.rlsbl", "kernel/CHANGELOG.md", "vendor/old/.rlsbl"}
	if strings.Join(paths, " ") != strings.Join(want, " ") {
		t.Fatalf("residue = %v, want %v", paths, want)
	}
	for _, p := range want {
		if !removable[p] {
			t.Errorf("cleanup does not remove %s", p)
		}
	}
}

func TestAStandaloneRootChangelogIsNotResidue(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeFixture(t, root, "CHANGELOG.md", "# Changelog\n")
	paths, _ := residuePaths(t, newWorkspace(t, root, standalone), nil)
	if len(paths) != 0 {
		t.Fatalf("residue = %v", paths)
	}
}

func TestReleaseStateOfAnUndeclaredSubjectIsResidueUntilRetired(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeFixture(t, root, ".strictmetadata/changelog/gadget/unreleased.jsonl", "")
	writeFixture(t, root, ".strictmetadata/changelog/old/0.1.0.jsonl", "")
	writeFixture(t, root, ".strictmetadata/releases/old/v0.1.0.toml", "")
	writeFixture(t, root, ".strictmetadata/.changelog-validation/old.toml", "")
	writeFixture(t, root, ".strictmetadata/retired-release-histories/ancient/releases/version", "0.3.0\n")
	w := newWorkspace(t, root, standalone)
	paths, removable := residuePaths(t, w, nil)
	want := []string{".strictmetadata/.changelog-validation/old.toml", ".strictmetadata/changelog/old", ".strictmetadata/releases/old"}
	if strings.Join(paths, " ") != strings.Join(want, " ") {
		t.Fatalf("residue = %v, want %v", paths, want)
	}
	if removable[".strictmetadata/changelog/old"] || removable[".strictmetadata/releases/old"] {
		t.Error("cleanup would delete a record of what a subject released")
	}

	// Moving the state where the residue says a retired subject keeps it
	// clears it.
	retireFixture(t, root, "old")
	paths, _ = residuePaths(t, w, nil)
	if strings.Join(paths, " ") != ".strictmetadata/.changelog-validation/old.toml" {
		t.Fatalf("residue after retiring = %v", paths)
	}
}

// retireFixture moves a subject's changelog and release directories under
// its retired history, as a retirement does.
func retireFixture(t *testing.T, root, subject string) {
	t.Helper()
	retired := filepath.Join(root, ".strictmetadata", "retired-release-histories", subject)
	if err := os.MkdirAll(retired, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{"changelog", "releases"} {
		if err := os.Rename(filepath.Join(root, ".strictmetadata", dir, subject), filepath.Join(retired, dir)); err != nil {
			t.Fatal(err)
		}
	}
}
