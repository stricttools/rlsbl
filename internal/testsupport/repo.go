// Package testsupport is rlsbl's test harness: throwaway git repositories
// with file:// bare remotes, a throwaway strictcli command to run code that
// needs an effects handle, a fake gh that is the test binary itself, a fake
// HTTP transport, and the walks the module-wide guard tests share.
//
// It is imported only from _test.go files (the layering test refuses any
// other importer), so it builds fixtures directly with os/exec and os: a
// fixture is the world a test starts from, not an effect of the code under
// test.
package testsupport

import (
	"bytes"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// Repo is a throwaway git repository on branch main, created under the
// test's temporary directory. Commits take the throwaway identity
// hygiene.Isolate exports.
type Repo struct {
	t   testing.TB
	Dir string
}

// NewRepo initializes an empty repository whose branch is main.
func NewRepo(t testing.TB) *Repo {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "repo")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	r := &Repo{t: t, Dir: dir}
	r.Git("init", "-q", "-b", "main")
	return r
}

// Git runs git in the repository and returns its stdout with surrounding
// whitespace removed. A non-zero exit fails the test, naming the command and
// what git printed.
func (r *Repo) Git(args ...string) string {
	r.t.Helper()
	stdout, stderr, code := r.GitResult(args...)
	if code != 0 {
		r.t.Fatalf("git %s in %s exited %d: %s%s", strings.Join(args, " "), r.Dir, code, stderr, stdout)
	}
	return strings.TrimSpace(stdout)
}

// GitResult runs git in the repository and returns its untrimmed output and
// exit code, for commands a test expects may fail.
func (r *Repo) GitResult(args ...string) (stdout, stderr string, code int) {
	r.t.Helper()
	return RunGit(r.t, r.Dir, args...)
}

// RunGit runs git in dir and returns its untrimmed output and exit code. A
// git that cannot be started fails the test.
func RunGit(t testing.TB, dir string, args ...string) (stdout, stderr string, code int) {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	var out, errOut bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errOut
	err := cmd.Run()
	var exitErr *exec.ExitError
	if err != nil && !errors.As(err, &exitErr) {
		t.Fatalf("starting git %s in %s: %v", strings.Join(args, " "), dir, err)
	}
	return out.String(), errOut.String(), cmd.ProcessState.ExitCode()
}

// Path is the absolute path of rel inside the repository.
func (r *Repo) Path(rel string) string {
	return filepath.Join(r.Dir, filepath.FromSlash(rel))
}

// Write writes content to rel, creating parent directories.
func (r *Repo) Write(rel, content string) {
	r.t.Helper()
	WriteFile(r.t, r.Path(rel), content)
}

// WriteFile writes content to path, creating parent directories.
func WriteFile(t testing.TB, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// Commit stages the named paths and commits them, returning the new HEAD.
func (r *Repo) Commit(message string, rels ...string) string {
	r.t.Helper()
	if len(rels) == 0 {
		r.t.Fatal("testsupport: Commit needs the paths it commits")
	}
	r.Git(append([]string{"add", "--"}, rels...)...)
	r.Git("commit", "-q", "-m", message)
	return r.Head()
}

// CommitFile writes content to rel and commits it, returning the new HEAD.
func (r *Repo) CommitFile(rel, content, message string) string {
	r.t.Helper()
	r.Write(rel, content)
	return r.Commit(message, rel)
}

// Head is the full hash of HEAD.
func (r *Repo) Head() string {
	r.t.Helper()
	return r.Git("rev-parse", "HEAD")
}

// AddBareRemote creates a bare repository under the test's temporary
// directory, adds it as the named remote through a file:// URL (the one
// transport hygiene.Isolate leaves open), and returns the bare repository's
// path. Nothing is pushed.
func (r *Repo) AddBareRemote(name string) string {
	r.t.Helper()
	bare := filepath.Join(r.t.TempDir(), name+".git")
	if _, stderr, code := RunGit(r.t, filepath.Dir(bare), "init", "-q", "--bare", "-b", "main", bare); code != 0 {
		r.t.Fatalf("git init --bare %s: %s", bare, stderr)
	}
	r.Git("remote", "add", name, "file://"+bare)
	return bare
}

// Clone clones the repository into a new directory under the test's
// temporary directory and returns it as a Repo whose origin is this one.
func (r *Repo) Clone() *Repo {
	r.t.Helper()
	dir := filepath.Join(r.t.TempDir(), "clone")
	if _, stderr, code := RunGit(r.t, filepath.Dir(dir), "clone", "-q", "file://"+r.Dir, dir); code != 0 {
		r.t.Fatalf("git clone %s: %s", r.Dir, stderr)
	}
	return &Repo{t: r.t, Dir: dir}
}

// Refs maps every ref of the repository at dir (a bare remote included) to
// the object it names.
func Refs(t testing.TB, dir string) map[string]string {
	t.Helper()
	stdout, stderr, code := RunGit(t, dir, "for-each-ref", "--format=%(refname) %(objectname)")
	if code != 0 {
		t.Fatalf("git for-each-ref in %s: %s", dir, stderr)
	}
	refs := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(stdout), "\n") {
		if name, object, ok := strings.Cut(line, " "); ok {
			refs[name] = object
		}
	}
	return refs
}
