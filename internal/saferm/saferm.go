// Package saferm deletes files through saferm, the deletion tool with an
// audit trail and undo. It is the one place rlsbl spells a saferm argv.
//
// saferm requires --on-error and has no default for it, so an invocation
// without it exits before deleting anything. The argv here always carries
// --on-error abort (stop at the first failure rather than press on through a
// partial delete), and there is no parameter for it: there is nothing to
// decide.
package saferm

import (
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// Request is one deletion.
type Request struct {
	// Path is the file or directory to delete, absolute or relative to the
	// directory the deletion runs in.
	Path string
	// Description says why it is deleted: the audit trail.
	Description string
	// Recursive deletes a directory and everything in it.
	Recursive bool
	// SkipMissing makes an absent path not an error.
	SkipMissing bool
}

// Args is the saferm argv (after "saferm") for the request. An empty path
// or description is refused.
func Args(r Request) ([]string, error) {
	if r.Path == "" {
		return nil, errors.New("a saferm deletion needs the path it deletes")
	}
	if strings.TrimSpace(r.Description) == "" {
		return nil, fmt.Errorf("deleting %s needs a description of why: it is the audit trail", r.Path)
	}
	args := []string{"delete"}
	if r.Recursive {
		args = append(args, "-r")
	}
	if r.SkipMissing {
		args = append(args, "-f")
	}
	return append(args, "--description", r.Description, "--on-error", "abort", "--", r.Path), nil
}

// Delete deletes the request's path through saferm, run in dir (absolute).
// saferm missing from PATH is an error naming it; there is no fallback to a
// deletion without an audit trail. Under --dry-run the deletion is recorded
// rather than performed.
func Delete(e *strictcli.Effects, dir string, r Request) error {
	if !filepath.IsAbs(dir) {
		return fmt.Errorf("saferm: the directory %q to delete from is not absolute", dir)
	}
	args, err := Args(r)
	if err != nil {
		return err
	}
	if _, err := exec.LookPath("saferm"); err != nil {
		return errors.New("saferm is not on PATH: rlsbl deletes only through saferm, which keeps an audit trail and can undo a deletion; install saferm and run this again")
	}
	argv := []interface{}{"saferm"}
	for _, a := range args {
		argv = append(argv, a)
	}
	if _, err := e.Run(argv, strictcli.Cwd(dir), strictcli.Stream(true)); err != nil {
		return fmt.Errorf("deleting %s through saferm: %w", r.Path, err)
	}
	return nil
}
