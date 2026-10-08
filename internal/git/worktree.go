package git

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// submoduleTimeout bounds a submodule update, which may clone over the
// network.
const submoduleTimeout = 30 * time.Minute

// restoreChunk bounds the paths one `git restore` names, keeping a release
// that regenerates many files far below the argument limit.
const restoreChunk = 500

// literalPathspecs makes git read every path as a path, never as a glob.
var literalPathspecs = map[string]string{"GIT_LITERAL_PATHSPECS": "1"}

// Change is one working-tree change: git's two status columns and the path.
type Change struct {
	Status string
	Path   string
}

// Changes are every change in the working tree, untracked files listed one
// by one and a staged rename reported as a deletion and an addition, so both
// of its paths are named.
func (r Repo) Changes() ([]Change, error) {
	out, err := r.output("--no-optional-locks", "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all")
	if err != nil {
		return nil, err
	}
	var changes []Change
	for _, field := range strings.Split(out, "\x00") {
		if len(field) >= 4 {
			changes = append(changes, Change{Status: field[:2], Path: field[3:]})
		}
	}
	return changes, nil
}

// ChangedBetween are the paths commit new changes relative to commit old,
// sorted, a rename split into its two paths.
func (r Repo) ChangedBetween(old, new string) ([]string, error) {
	out, err := r.output("--no-optional-locks", "diff", "--name-only", "-z", "--no-renames", old, new)
	if err != nil {
		return nil, err
	}
	paths := nulFields(out)
	sort.Strings(paths)
	return paths, nil
}

// Tracks reports whether the index tracks the path.
func (r Repo) Tracks(path string) (bool, error) {
	out, err := r.output("ls-files", "-z", "--", path)
	if err != nil {
		return false, err
	}
	return len(nulFields(out)) > 0, nil
}

// Ignored reports whether git's ignore rules match the path (relative to
// the repository root).
func (r Repo) Ignored(path string) (bool, error) {
	args := []string{"check-ignore", "-q", "--", path}
	res, err := r.read(localTimeout, nil, args...)
	if err != nil {
		return false, err
	}
	switch res.code {
	case 0:
		return true, nil
	case 1:
		return false, nil
	}
	return false, r.failed(args, res)
}

// IgnoreRules maps each of the paths (relative to the repository root) that
// git's ignore rules match to the rule matching it, as
// "<source>:<line>:<pattern>". Tracked paths are never matched.
func (r Repo) IgnoreRules(paths []string) (map[string]string, error) {
	out := map[string]string{}
	if len(paths) == 0 {
		return out, nil
	}
	args := []string{"check-ignore", "-v", "-z", "--stdin"}
	res, err := r.read(localTimeout, []byte(strings.Join(paths, "\x00")+"\x00"), args...)
	if err != nil {
		return nil, err
	}
	if res.code != 0 && res.code != 1 {
		return nil, r.failed(args, res)
	}
	fields := nulFields(res.stdout)
	if len(fields)%4 != 0 {
		return nil, fmt.Errorf("git %s in %s printed %d NUL-separated fields, not a multiple of four", strings.Join(args, " "), r.dir, len(fields))
	}
	for i := 0; i < len(fields); i += 4 {
		out[fields[i+3]] = fields[i] + ":" + fields[i+1] + ":" + fields[i+2]
	}
	return out, nil
}

// mutateWithEnv is mutate with environment variables set for git.
func (r Repo) mutateWithEnv(timeout time.Duration, env map[string]string, args ...string) error {
	_, err := r.e.Run(argv(args), strictcli.Cwd(r.dir), strictcli.Timeout(timeout), strictcli.Stream(true), strictcli.EffectEnv(env))
	if err != nil {
		return fmt.Errorf("git %s in %s: %w", strings.Join(args, " "), r.dir, err)
	}
	return nil
}

// AddDetachedWorktree registers a working tree at the absolute path, its
// HEAD detached at commit. --force lets a registration whose directory was
// deleted by hand be replaced; it names only this path.
func (r Repo) AddDetachedWorktree(path, commit string) error {
	if !filepath.IsAbs(path) {
		return fmt.Errorf("refusing to add the worktree %q: its path is not absolute", path)
	}
	return r.mutate(localTimeout, "worktree", "add", "--force", "--detach", "--quiet", path, commit)
}

// ResetDetached puts the Repo's working tree at commit with HEAD detached,
// discarding every change to tracked files, and removes its untracked files
// while keeping the ignored ones (dependency environments, build caches).
func (r Repo) ResetDetached(commit string) error {
	if err := r.mutate(localTimeout, "checkout", "--detach", "--force", "--quiet", commit); err != nil {
		return err
	}
	return r.mutate(localTimeout, "clean", "-ffdq")
}

// UpdateSubmodules initializes and checks out every submodule the commit
// declares, recursively.
func (r Repo) UpdateSubmodules() error {
	return r.mutate(submoduleTimeout, "submodule", "update", "--init", "--recursive", "--force")
}

// SwapRef moves the local ref (a full ref name) from old to new by
// compare-and-swap: git refuses when the ref no longer holds old, and the
// refusal is an error. reason is the reflog message.
func (r Repo) SwapRef(ref, new, old, reason string) error {
	if !strings.HasPrefix(ref, "refs/") {
		return fmt.Errorf("refusing to move %q: it is not a full ref name (refs/...)", ref)
	}
	for _, id := range []string{new, old} {
		if !IsObjectID(id) {
			return fmt.Errorf("refusing to move %s: %q is not a full object id", ref, id)
		}
	}
	return r.mutate(localTimeout, "update-ref", "-m", reason, ref, new, old)
}

// RestorePaths makes the paths in the index and the working tree hold what
// they hold in commit source; a path source does not hold is removed from
// both. Paths are literal, never globs.
func (r Repo) RestorePaths(source string, paths []string) error {
	for start := 0; start < len(paths); start += restoreChunk {
		end := min(start+restoreChunk, len(paths))
		args := append([]string{"restore", "--source=" + source, "--staged", "--worktree", "--"}, paths[start:end]...)
		if err := r.mutateWithEnv(localTimeout, literalPathspecs, args...); err != nil {
			return err
		}
	}
	return nil
}
