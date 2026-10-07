package migration

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
)

// oldTree is the old layout's files on disk: every file under the
// old-layout directories, each of which some conversion must claim. A file
// nothing claims is one the migration does not recognize, and refuses.
type oldTree struct {
	root string
	// dirs are the old-layout directories, repository-relative.
	dirs []string
	// files are every file under dirs, repository-relative.
	files map[string]bool
	// claimed are the files a conversion has accounted for.
	claimed map[string]bool
}

func newOldTree(root string, dirs []string) (*oldTree, error) {
	t := &oldTree{root: root, dirs: dirs, files: map[string]bool{}, claimed: map[string]bool{}}
	for _, dir := range dirs {
		err := filepath.WalkDir(t.abs(dir), func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				return nil
			}
			rel, err := filepath.Rel(root, p)
			if err != nil {
				return err
			}
			t.files[filepath.ToSlash(rel)] = true
			return nil
		})
		if err != nil {
			return nil, fmt.Errorf("listing the old layout under %s: %w", dir, err)
		}
	}
	return t, nil
}

func (t *oldTree) abs(rel string) string { return filepath.Join(t.root, filepath.FromSlash(rel)) }

// has reports whether rel is a file of the old layout.
func (t *oldTree) has(rel string) bool { return t.files[rel] }

// claim accounts for rel.
func (t *oldTree) claim(rel string) { t.claimed[rel] = true }

// under lists the files below dir (repository-relative), sorted.
func (t *oldTree) under(dir string) []string {
	prefix := dir + "/"
	var out []string
	for f := range t.files {
		if strings.HasPrefix(f, prefix) {
			out = append(out, f)
		}
	}
	sort.Strings(out)
	return out
}

// children are the names directly below dir: files and directories alike.
func (t *oldTree) children(dir string) []string {
	seen := map[string]bool{}
	var out []string
	for _, f := range t.under(dir) {
		first, _, _ := strings.Cut(strings.TrimPrefix(f, dir+"/"), "/")
		if !seen[first] {
			seen[first] = true
			out = append(out, first)
		}
	}
	sort.Strings(out)
	return out
}

// unclaimed are the files no conversion accounted for, sorted.
func (t *oldTree) unclaimed() []string {
	var out []string
	for f := range t.files {
		if !t.claimed[f] {
			out = append(out, f)
		}
	}
	sort.Strings(out)
	return out
}

// read reads an old-layout file.
func (t *oldTree) read(rel string) ([]byte, error) {
	data, err := os.ReadFile(t.abs(rel))
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", rel, err)
	}
	return data, nil
}

// readJSON reads an old JSON document into plain values, numbers kept as
// json.Number. A missing file is found false.
func (t *oldTree) readJSON(rel string) (value any, found bool, err error) {
	data, err := os.ReadFile(t.abs(rel))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", rel, err)
	}
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.UseNumber()
	if err := dec.Decode(&value); err != nil {
		return nil, true, fmt.Errorf("%s is not valid JSON: %w", rel, err)
	}
	return value, true, nil
}

// readTOML reads an old TOML document into plain values. A missing file is
// found false.
func (t *oldTree) readTOML(rel string) (doc map[string]any, found bool, err error) {
	data, err := os.ReadFile(t.abs(rel))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", rel, err)
	}
	parsed, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return nil, true, fmt.Errorf("%s is not valid TOML: %w", rel, err)
	}
	return *parsed, true, nil
}

// isLocalOnly reports a machine-local file the Python wrote beside its
// records (a watch log, a release log): never tracked, removed with its
// directory.
func isLocalOnly(rel string) bool { return strings.HasSuffix(path.Base(rel), ".local-only") }

// exists reports whether rel exists under root, as a file or a directory.
func exists(root, rel string) (bool, error) {
	_, err := os.Lstat(filepath.Join(root, filepath.FromSlash(rel)))
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("reading %s: %w", rel, err)
	}
	return true, nil
}
