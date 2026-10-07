package dependencies

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"
	"syscall"
)

// The files a uv project and a uv workspace are read from.
const (
	PyprojectFile = "pyproject.toml"
	UvLockFile    = "uv.lock"
)

// The relock commands every staleness finding names.
const (
	PypiRelock = "uv lock"
	NpmRelock  = "npm install --package-lock-only --ignore-scripts"
	GoRelock   = "go mod tidy"
)

// NpmRelockArgv is the npm lockfile refresh as argv. --ignore-scripts keeps
// the package's own lifecycle scripts out of it: a refresh has no business
// running project code.
var NpmRelockArgv = []string{"npm", "install", "--package-lock-only", "--ignore-scripts"}

// LockLocation is the one uv.lock that resolves a manifest, and how it was
// reached.
type LockLocation struct {
	// Path is the lock, absolute.
	Path string
	// WorkspaceRoot is the uv workspace root whose lock this is, absolute,
	// or empty when the lock sits beside the manifest.
	WorkspaceRoot string
}

// Label names the lock relative to the project directory, as errors name it.
func (l LockLocation) Label(projectDir string) string {
	return relative(projectDir, l.Path)
}

// Describe is the fact line naming which lock a version was read from.
func (l LockLocation) Describe(projectDir string) string {
	if l.WorkspaceRoot == "" {
		return fmt.Sprintf("floor read from %s (beside the manifest)", l.Label(projectDir))
	}
	return fmt.Sprintf("floor read from %s -- the uv workspace root at %s claims this directory as a member, and a member has no lock of its own",
		l.Label(projectDir), relative(projectDir, l.WorkspaceRoot))
}

// LockSearch is the outcome of looking for a manifest's lock. Location is
// nil when no lock was found; Probed then names every location looked at.
type LockSearch struct {
	Location *LockLocation
	Probed   string
}

// relative is target relative to base, or target itself when it has no
// relative form.
func relative(base, target string) string {
	rel, err := filepath.Rel(base, target)
	if err != nil {
		return target
	}
	return rel
}

// uvWorkspaceTable is the [tool.uv.workspace] table of the pyproject.toml at
// p; found is false when the file is absent or declares none. A file that
// does not parse, and a [tool.uv.workspace] that is not a table, are errors:
// walking past an unreadable declaration would answer from a different
// workspace.
func uvWorkspaceTable(p string) (map[string]any, bool, error) {
	doc, found, err := readTOML(p)
	if err != nil || !found {
		return nil, false, err
	}
	uv, ok := table(doc, "tool", "uv")
	if !ok {
		return nil, false, nil
	}
	raw, present := uv["workspace"]
	if !present {
		return nil, false, nil
	}
	ws, ok := raw.(map[string]any)
	if !ok {
		return nil, false, fmt.Errorf("%s: [tool.uv.workspace] is not a table", p)
	}
	return ws, true, nil
}

// globList is a [tool.uv.workspace] glob list: absent is empty, anything but
// a list of strings is refused.
func globList(ws map[string]any, key, where string) ([]string, error) {
	raw, present := ws[key]
	if !present {
		return nil, nil
	}
	items, ok := raw.([]any)
	if !ok {
		return nil, fmt.Errorf("%s: [tool.uv.workspace].%s is not a list of globs", where, key)
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		s, ok := item.(string)
		if !ok {
			return nil, fmt.Errorf("%s: [tool.uv.workspace].%s holds %v, which is not a glob string", where, key, item)
		}
		out = append(out, s)
	}
	return out, nil
}

// resolved is p with every symlink resolved, or p cleaned when it does not
// exist.
func resolved(p string) string {
	abs, err := filepath.Abs(p)
	if err != nil {
		return filepath.Clean(p)
	}
	if r, err := filepath.EvalSymlinks(abs); err == nil {
		return r
	}
	return abs
}

// globClaims reports whether any glob, expanded against the files under
// root the way uv reads it (relative to root, "*" stopping at a separator,
// "**" crossing them, hidden names matched only by a pattern spelling the
// dot), reaches target (resolved).
func globClaims(patterns []string, root, target string) (bool, error) {
	for _, pattern := range patterns {
		hits, err := expandGlob(root, strings.Split(strings.Trim(filepath.ToSlash(pattern), "/"), "/"))
		if err != nil {
			return false, fmt.Errorf("expanding the uv workspace glob %q under %s: %w", pattern, root, err)
		}
		for _, hit := range hits {
			if resolved(hit) == target {
				return true, nil
			}
		}
	}
	return false, nil
}

// hasMagic reports whether a glob segment holds a wildcard.
func hasMagic(segment string) bool { return strings.ContainsAny(segment, "*?[") }

// listDir lists dir; a directory that does not exist, or a path that is not
// a directory, lists nothing.
func listDir(dir string) ([]fs.DirEntry, error) {
	entries, err := os.ReadDir(dir)
	if errors.Is(err, os.ErrNotExist) || errors.Is(err, syscall.ENOTDIR) {
		return nil, nil
	}
	return entries, err
}

// expandGlob is every path under dir the segments reach.
func expandGlob(dir string, segments []string) ([]string, error) {
	if len(segments) == 0 {
		return []string{dir}, nil
	}
	segment, rest := segments[0], segments[1:]
	switch {
	case segment == "" || segment == ".":
		return expandGlob(dir, rest)
	case segment == "**":
		dirs := []string{dir}
		if err := descendants(dir, &dirs); err != nil {
			return nil, err
		}
		var out []string
		for _, d := range dirs {
			hits, err := expandGlob(d, rest)
			if err != nil {
				return nil, err
			}
			out = append(out, hits...)
		}
		return out, nil
	case !hasMagic(segment):
		p := filepath.Join(dir, segment)
		if _, err := os.Lstat(p); errors.Is(err, os.ErrNotExist) {
			return nil, nil
		} else if err != nil {
			return nil, err
		}
		return expandGlob(p, rest)
	}
	entries, err := listDir(dir)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, ".") && !strings.HasPrefix(segment, ".") {
			continue
		}
		ok, err := path.Match(segment, name)
		if err != nil {
			return nil, err
		}
		if !ok {
			continue
		}
		hits, err := expandGlob(filepath.Join(dir, name), rest)
		if err != nil {
			return nil, err
		}
		out = append(out, hits...)
	}
	return out, nil
}

// descendants appends every directory below dir, hidden ones and symlinks
// left out, depth first.
func descendants(dir string, out *[]string) error {
	entries, err := listDir(dir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		child := filepath.Join(dir, e.Name())
		*out = append(*out, child)
		if err := descendants(child, out); err != nil {
			return err
		}
	}
	return nil
}

// workspaceClaims is uv's membership rule: a members glob reaches target and
// no exclude glob does.
func workspaceClaims(root string, ws map[string]any, target string) (bool, error) {
	where := filepath.Join(root, PyprojectFile)
	members, err := globList(ws, "members", where)
	if err != nil {
		return false, err
	}
	claimed, err := globClaims(members, root, target)
	if err != nil || !claimed {
		return false, err
	}
	exclude, err := globList(ws, "exclude", where)
	if err != nil {
		return false, err
	}
	excluded, err := globClaims(exclude, root, target)
	if err != nil {
		return false, err
	}
	return !excluded, nil
}

// FindUvWorkspaceRoot is the uv workspace root that claims projectDir, and
// false when none does. It walks as uv discovers a workspace: from the
// project's parent upward, the first ancestor declaring [tool.uv.workspace]
// decides. A directory that declaration does not claim (no members glob
// reaches it, or an exclude glob does) is a standalone project, not a member
// of a further ancestor: uv forbids nested workspaces. Paths are compared
// with symlinks resolved, so a checkout reached through a symlink resolves
// to the same workspace.
func FindUvWorkspaceRoot(projectDir string) (string, bool, error) {
	target := resolved(projectDir)
	current := filepath.Dir(target)
	for {
		ws, found, err := uvWorkspaceTable(filepath.Join(current, PyprojectFile))
		if err != nil {
			return "", false, err
		}
		if found {
			claimed, err := workspaceClaims(current, ws, target)
			if err != nil || !claimed {
				return "", false, err
			}
			return current, true, nil
		}
		parent := filepath.Dir(current)
		if parent == current {
			return "", false, nil
		}
		current = parent
	}
}

// LocateUvLock finds the one uv.lock resolving the manifest in projectDir:
// the lock beside it when one exists, otherwise the lock of the uv
// workspace root that claims the directory, otherwise none, with every
// location probed named. Readability is not decided here: a lock that
// exists and does not parse is still the location, and refusing to read it
// is the reader's job.
func LocateUvLock(projectDir string) (LockSearch, error) {
	beside := filepath.Join(projectDir, UvLockFile)
	found, err := fileExists(beside)
	if err != nil {
		return LockSearch{}, err
	}
	if found {
		return LockSearch{Location: &LockLocation{Path: beside}, Probed: beside + " (present)"}, nil
	}
	root, claimed, err := FindUvWorkspaceRoot(projectDir)
	if err != nil {
		return LockSearch{}, err
	}
	if !claimed {
		return LockSearch{Probed: beside + " (absent); no ancestor declares a [tool.uv.workspace] claiming this directory, so there is no workspace lock to read either"}, nil
	}
	workspaceLock := filepath.Join(root, UvLockFile)
	found, err = fileExists(workspaceLock)
	if err != nil {
		return LockSearch{}, err
	}
	if found {
		return LockSearch{Location: &LockLocation{Path: workspaceLock, WorkspaceRoot: root}, Probed: workspaceLock + " (present)"}, nil
	}
	return LockSearch{Probed: fmt.Sprintf("%s (absent) and %s (absent), the lock of the uv workspace root that claims this directory as a member", beside, workspaceLock)}, nil
}

// IsVirtualUvRoot reports whether dir is a virtual uv workspace root: its
// pyproject.toml declares [tool.uv.workspace] and no [project] table, so it
// aggregates members and is no package itself.
func IsVirtualUvRoot(dir string) (bool, error) {
	doc, found, err := readTOML(filepath.Join(dir, PyprojectFile))
	if err != nil || !found {
		return false, err
	}
	if _, isProject := doc["project"]; isProject {
		return false, nil
	}
	uv, ok := table(doc, "tool", "uv")
	if !ok {
		return false, nil
	}
	_, declared := uv["workspace"]
	return declared, nil
}
