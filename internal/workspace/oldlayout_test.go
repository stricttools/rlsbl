package workspace

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestOldLayoutDirectoryFindsTheOldLayoutOnDiskOrTracked(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	if dir, err := OldLayoutDirectory(root, []string{"src/main.go", "docs/rlsbl.md"}); err != nil || dir != "" {
		t.Fatalf("a repository without the old layout: %q, %v", dir, err)
	}
	if dir, err := OldLayoutDirectory(root, []string{"widget/.rlsbl/config.json"}); err != nil || dir != "widget/.rlsbl" {
		t.Fatalf("a tracked member directory: %q, %v", dir, err)
	}
	if err := os.MkdirAll(filepath.Join(root, ".rlsbl-monorepo"), 0o755); err != nil {
		t.Fatal(err)
	}
	if dir, err := OldLayoutDirectory(root, nil); err != nil || dir != ".rlsbl-monorepo" {
		t.Fatalf("the workspace directory on disk: %q, %v", dir, err)
	}
}
