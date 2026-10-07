// Package runstate is the state of rlsbl operations in progress, kept under
// .strictmetadata/.release-state/: the advisory lock every mutation of
// release state takes, a release's in-progress state (which steps finished,
// which failed), the retry file, the batch plan, and the paths of the
// reconcile plan and the scrub result.
//
// Run state is the operator's working state, never a record: the directory
// ignores everything but its own .gitignore, and every file here is written
// in the working tree, even by a release running in the release checkout.
package runstate

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The run-state paths, repository-relative.
const (
	// LockPath is the advisory lock.
	LockPath = declarations.ReleaseStateDir + "/lock"
	// BatchPlanPath is the plan of a batch release in progress.
	BatchPlanPath = declarations.ReleaseStateDir + "/batch-plan.toml"
	// gitignorePath keeps everything in the directory out of git but itself.
	gitignorePath = declarations.ReleaseStateDir + "/.gitignore"
)

// gitignoreContent ignores everything in the run-state directory but the
// .gitignore itself.
const gitignoreContent = "*\n!.gitignore\n"

// InProgressPath is a releasable's in-progress release state.
func InProgressPath(releasable string) string {
	return declarations.RunStateDir(releasable) + "/in-progress.toml"
}

// RetryPath is a releasable's retry file.
func RetryPath(releasable string) string {
	return declarations.RunStateDir(releasable) + "/retry.toml"
}

// ReconcilePlanPath is a releasable's reconcile plan.
func ReconcilePlanPath(releasable string) string {
	return declarations.RunStateDir(releasable) + "/reconcile-plan.toml"
}

// ScrubResultPath is a releasable's scrub result.
func ScrubResultPath(releasable string) string {
	return declarations.RunStateDir(releasable) + "/scrub-result.json"
}

// stateFileMode is the mode of every run-state file: tool-owned working
// state nobody else reads, the same whichever writer ran last.
const stateFileMode = 0o600

func absolute(root, rel string) string { return filepath.Join(root, filepath.FromSlash(rel)) }

// readFile reads the run-state file rel. found is false when it does not
// exist.
func readFile(root, rel string) (data []byte, found bool, err error) {
	data, err = os.ReadFile(absolute(root, rel))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", rel, err)
	}
	return data, true, nil
}

// exists reports whether the run-state path rel exists.
func exists(root, rel string) (bool, error) {
	_, err := os.Lstat(absolute(root, rel))
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("reading %s: %w", rel, err)
	}
	return true, nil
}

// ensureDirectory creates the run-state directory and its .gitignore when
// they are missing, so run state never shows as an untracked file, even in
// a repository scaffolded before the directory existed.
func ensureDirectory(e *strictcli.Effects, root string) error {
	found, err := exists(root, gitignorePath)
	if err != nil || found {
		return err
	}
	if _, err := e.Mkdir(absolute(root, declarations.ReleaseStateDir)); err != nil {
		return fmt.Errorf("creating %s: %w", declarations.ReleaseStateDir, err)
	}
	if _, err := e.Write(absolute(root, gitignorePath), gitignoreContent); err != nil {
		return fmt.Errorf("writing %s: %w", gitignorePath, err)
	}
	return nil
}

// writeFile replaces the run-state file rel with data, atomically.
func writeFile(e *strictcli.Effects, root, rel string, data []byte) error {
	if !filepath.IsAbs(root) {
		return fmt.Errorf("the repository root %q is not absolute", root)
	}
	if err := ensureDirectory(e, root); err != nil {
		return err
	}
	target := absolute(root, rel)
	if _, err := e.Mkdir(filepath.Dir(target)); err != nil {
		return fmt.Errorf("creating the directory of %s: %w", rel, err)
	}
	temporary := target + ".tmp"
	if _, err := e.Write(temporary, data, strictcli.Mode(stateFileMode)); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	if _, err := e.Rename(temporary, target); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	return nil
}

// removeFile removes the run-state file rel when it exists, and its
// directory when that is left empty.
func removeFile(e *strictcli.Effects, root, rel string) error {
	found, err := exists(root, rel)
	if err != nil || !found {
		return err
	}
	if _, err := e.Remove(absolute(root, rel)); err != nil {
		return fmt.Errorf("removing %s: %w", rel, err)
	}
	dir := filepath.Dir(absolute(root, rel))
	if dir == absolute(root, declarations.ReleaseStateDir) {
		return nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("reading %s: %w", dir, err)
	}
	// Under --dry-run the file is still there; the directory's removal is
	// recorded beside it.
	onlyTheFile := len(entries) == 1 && entries[0].Name() == filepath.Base(rel)
	if len(entries) == 0 || onlyTheFile {
		if _, err := e.Remove(dir); err != nil {
			return fmt.Errorf("removing %s: %w", dir, err)
		}
	}
	return nil
}

// FileError is a run-state file rlsbl refuses: the file and every problem in
// it.
type FileError struct {
	File     string
	Problems []string
}

func (e *FileError) Error() string {
	if len(e.Problems) == 1 {
		return e.File + ": " + e.Problems[0]
	}
	return e.File + " is refused:\n  " + strings.Join(e.Problems, "\n  ")
}

// decode decodes a run-state file strictly: an unknown key, a wrong type,
// and a missing required key are refused.
func decode[T any](rel string, data []byte) (*T, error) {
	v, err := tomledit.Unmarshal[T](data)
	if err != nil {
		return nil, &FileError{File: rel, Problems: []string{err.Error()}}
	}
	return v, nil
}

func quote(s string) string { return tomledit.QuoteString(s) }

func stringArray(values []string) string {
	quoted := make([]string, len(values))
	for i, v := range values {
		quoted[i] = quote(v)
	}
	return "[" + strings.Join(quoted, ", ") + "]"
}

func quoteKey(s string) string { return tomledit.QuoteKey(s) }
