package targets

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// copyExcludedAtRoot are left out of the copy a rewritten Python build runs
// in: version control, rlsbl's records old and new, and the output
// directory. __pycache__ and compiled files are left out everywhere.
var copyExcludedAtRoot = map[string]bool{".git": true, "__pycache__": true, ".rlsbl": true, ".rlsbl-monorepo": true, ".strictmetadata": true, "dist": true}

// BuildInputs are what a target's release build reads besides the target.
type BuildInputs struct {
	// Dir is the target's directory, absolute.
	Dir string
	// RewrittenPyproject is pyproject.toml with its path dependencies on
	// workspace siblings rewritten to registry constraints, or nil when it
	// has none: the build then runs in a copy of Dir carrying it, so the
	// working tree is never changed.
	RewrittenPyproject []byte
}

// Build builds what the release publishes for target t: for pypi, the
// sdist and wheel `uv build` writes into Dir/dist, bounded by the target's
// build timeout. The other targets build nothing in the release (their CI
// builds what they publish).
func Build(h Handle, t Target, in BuildInputs) error {
	facts := t.Facts()
	if facts.BuildTimeoutSeconds == 0 {
		return nil
	}
	if _, err := exec.LookPath("uv"); err != nil {
		return fmt.Errorf("uv is not on PATH, so the %s package in %s cannot be built", facts.RegistryDisplayName, in.Dir)
	}
	timeout := time.Duration(facts.BuildTimeoutSeconds) * time.Second
	dist := filepath.Join(in.Dir, "dist")
	if in.RewrittenPyproject == nil {
		_, err := h.Run([]interface{}{"uv", "build", "--out-dir", dist}, strictcli.Cwd(in.Dir), strictcli.Stream(true), strictcli.Timeout(timeout))
		return err
	}
	return buildRewritten(h, in, dist, timeout)
}

// buildRewritten copies the project into a scratch directory, writes the
// rewritten pyproject.toml there, and builds into the project's own dist directory.
func buildRewritten(h Handle, in BuildInputs, dist string, timeout time.Duration) (err error) {
	suffix := make([]byte, 6)
	if _, err := rand.Read(suffix); err != nil {
		return err
	}
	scratch := filepath.Join(os.TempDir(), "rlsbl-build-"+hex.EncodeToString(suffix))
	defer func() {
		if _, rmErr := h.Remove(scratch); rmErr != nil && err == nil {
			err = rmErr
		}
	}()
	project := filepath.Join(scratch, "project")
	if err := copyTree(h, in.Dir, project); err != nil {
		return err
	}
	if _, err := h.Write(filepath.Join(project, Pyproject), in.RewrittenPyproject); err != nil {
		return err
	}
	if _, err := h.Mkdir(dist); err != nil {
		return err
	}
	_, err = h.Run([]interface{}{"uv", "build", "--out-dir", dist}, strictcli.Cwd(project), strictcli.Stream(true), strictcli.Timeout(timeout))
	return err
}

// copyTree copies src into dst through the handle, keeping file modes and
// following symlinks to files. A symlink to a directory is refused: the
// copy would have to choose between its link and its contents.
func copyTree(h Handle, src, dst string) error {
	return filepath.WalkDir(src, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, p)
		if err != nil {
			return err
		}
		name := d.Name()
		if rel != "." && ((d.IsDir() && name == "__pycache__") || strings.HasSuffix(name, ".pyc") || (!strings.Contains(rel, string(filepath.Separator)) && copyExcludedAtRoot[name])) {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			_, err := h.Mkdir(target)
			return err
		}
		info, err := os.Stat(p)
		if err != nil {
			return err
		}
		if info.IsDir() {
			return fmt.Errorf("%s is a symlink to a directory, which the build copy of %s cannot carry", p, src)
		}
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		_, err = h.Write(target, data, strictcli.Mode(info.Mode().Perm()))
		return err
	})
}
