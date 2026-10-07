package testsupport

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ModulePath is rlsbl's Go module path.
const ModulePath = "github.com/stricttools/rlsbl"

// skippedDirs are directory names the module walks never enter: the scratch
// directories (each a module of its own, and git-ignored, so an experiment
// inside one cannot turn a guard red), git's directory, npm's tree, and test
// fixtures.
var skippedDirs = map[string]bool{
	"experiments":  true,
	"screenshots":  true,
	".git":         true,
	"node_modules": true,
	"testdata":     true,
}

// ModuleRoot is the root of rlsbl's Go module: the nearest ancestor of the
// working directory (a package directory, under go test) whose go.mod
// declares ModulePath.
func ModuleRoot(t testing.TB) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		data, err := os.ReadFile(filepath.Join(dir, "go.mod"))
		if err == nil && strings.HasPrefix(string(data), "module "+ModulePath+"\n") {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("testsupport: no ancestor of the working directory holds the go.mod of %s", ModulePath)
		}
		dir = parent
	}
}

// GoFiles lists every .go file of the module under root, as slash-separated
// paths relative to root, in walk order. It enters no skipped directory and
// no directory holding a go.mod of its own.
func GoFiles(t testing.TB, root string) []string {
	t.Helper()
	var files []string
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if path == root {
				return nil
			}
			if skippedDirs[d.Name()] {
				return filepath.SkipDir
			}
			if _, err := os.Stat(filepath.Join(path, "go.mod")); err == nil {
				return filepath.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(d.Name(), ".go") {
			rel, err := filepath.Rel(root, path)
			if err != nil {
				return err
			}
			files = append(files, filepath.ToSlash(rel))
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(files) == 0 {
		t.Fatalf("testsupport: no Go file found under %s", root)
	}
	return files
}

// ImportPath is the import path of the package holding the Go file at rel
// (a path GoFiles returned).
func ImportPath(rel string) string {
	dir := filepath.ToSlash(filepath.Dir(rel))
	if dir == "." {
		return ModulePath
	}
	return ModulePath + "/" + dir
}
