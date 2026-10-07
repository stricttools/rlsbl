// Package git is rlsbl's git plumbing. Every git it starts goes through the
// strictcli effects handle, so a read runs for real under --dry-run only when
// the observe allowlist admits its argv, and every write is recorded instead
// of performed.
//
// A read that git cannot answer is an error naming the command, the
// repository, and what git printed; nothing here turns a failed read into
// "no", "absent", or an empty list. Where git's own answer has a third state
// (an ancestry question git cannot follow, a name that does not resolve),
// the result type spells that state out.
//
// Output git prints for a person (paths, file contents) is read in a form
// that is never quoted or trimmed: -z for path lists, and the object store
// for file contents, because the effects handle drops one trailing newline
// from what a program prints.
package git

import (
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// The timeouts of git's own work. A local read is bounded generously: it is
// a backstop against a hung process, not a budget. Network reads talk to the
// remote and get the same bound; a push takes the timeout its caller
// declares.
const (
	localTimeout   = 2 * time.Minute
	networkTimeout = 2 * time.Minute
)

// Runner starts programs: the strictcli effects handle, or the screened view
// of it a reconciler's observation gets (previewapply.Observer), which
// refuses every git command that writes.
type Runner interface {
	Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error)
}

// Repo is one repository's working tree, bound to the runner every git
// command goes through. Dir is the repository root: every path this package
// takes or returns is relative to it.
type Repo struct {
	e   Runner
	dir string
}

// Open binds the repository rooted at dir to the runner e. dir must be
// absolute: git is never asked about whatever directory the process happens
// to stand in.
func Open(e Runner, dir string) (Repo, error) {
	if e == nil {
		return Repo{}, errors.New("git: no effects handle to run git through")
	}
	if !filepath.IsAbs(dir) {
		return Repo{}, fmt.Errorf("git: the repository directory %q is not absolute; resolve it before opening the repository", dir)
	}
	return Repo{e: e, dir: filepath.Clean(dir)}, nil
}

// Dir is the repository root the Repo was opened at.
func (r Repo) Dir() string { return r.dir }

// result is what one git read printed and exited with.
type result struct {
	stdout string
	stderr string
	code   int
}

// read runs a git command whose output is read, and returns its output
// whatever it exited with. Only a git that could not be run is an error.
func (r Repo) read(timeout time.Duration, stdin []byte, args ...string) (result, error) {
	opts := []strictcli.EffectOption{strictcli.Cwd(r.dir), strictcli.Check(false), strictcli.Timeout(timeout)}
	if stdin != nil {
		opts = append(opts, strictcli.Stdin(stdin))
	}
	c, err := r.e.Run(argv(args), opts...)
	if err != nil {
		return result{}, fmt.Errorf("git %s in %s: %w", strings.Join(args, " "), r.dir, err)
	}
	return result{stdout: c.Stdout(), stderr: c.Stderr(), code: c.ExitCode()}, nil
}

// output runs a git read that must succeed and returns its stdout.
func (r Repo) output(args ...string) (string, error) {
	res, err := r.read(localTimeout, nil, args...)
	if err != nil {
		return "", err
	}
	if res.code != 0 {
		return "", r.failed(args, res)
	}
	return res.stdout, nil
}

// mutate runs a git command that changes the repository or a remote. Its
// output streams to the command's own streams, so what git says about a
// failure reaches the operator, and a non-zero exit is an error. Under
// --dry-run the command is recorded rather than run.
func (r Repo) mutate(timeout time.Duration, args ...string) error {
	_, err := r.e.Run(argv(args), strictcli.Cwd(r.dir), strictcli.Timeout(timeout), strictcli.Stream(true))
	if err != nil {
		return fmt.Errorf("git %s in %s: %w", strings.Join(args, " "), r.dir, err)
	}
	return nil
}

// failed is the error for a git read that exited non-zero.
func (r Repo) failed(args []string, res result) error {
	detail := strings.TrimSpace(res.stderr)
	if detail == "" {
		detail = strings.TrimSpace(res.stdout)
	}
	return fmt.Errorf("git %s in %s exited %d: %s", strings.Join(args, " "), r.dir, res.code, detail)
}

// argv is the effects handle's argv for git args.
func argv(args []string) []interface{} {
	out := make([]interface{}, 0, len(args)+1)
	out = append(out, "git")
	for _, a := range args {
		out = append(out, a)
	}
	return out
}

// lines splits output into its non-empty lines.
func lines(s string) []string {
	var out []string
	for _, line := range strings.Split(s, "\n") {
		if line = strings.TrimRight(line, "\r"); strings.TrimSpace(line) != "" {
			out = append(out, line)
		}
	}
	return out
}

// nulFields splits NUL-separated output into its non-empty fields.
func nulFields(s string) []string {
	var out []string
	for _, f := range strings.Split(s, "\x00") {
		if f != "" {
			out = append(out, f)
		}
	}
	return out
}

// IsObjectID reports whether s is a full object id: 40 (SHA-1) or 64
// (SHA-256) lowercase hexadecimal digits.
func IsObjectID(s string) bool {
	if len(s) != 40 && len(s) != 64 {
		return false
	}
	for _, c := range s {
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

// IsNullObjectID reports whether s is git's all-zeros object id, the marker
// for "no object" in hook input and rewrite maps.
func IsNullObjectID(s string) bool {
	return IsObjectID(s) && strings.Trim(s, "0") == ""
}
