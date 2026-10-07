// Package workspace is the model of a repository built from its release
// declarations: its members and releasables, which member owns each file,
// the scopes attribution answers for, each releasable's tag scheme, the
// members' dependency graph and release order, the evaluation of a
// dependency's version constraint, and the old-layout residue `monorepo
// cleanup` removes.
//
// A standalone repository and a workspace are one model here: a standalone
// repository is declarations with one member (the root) and one releasable.
// Which one a repository is comes from the declarations, never from counting.
package workspace

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

// Workspace is one repository's model: its root and its declarations.
type Workspace struct {
	// Root is the repository root, absolute.
	Root string
	// Declarations are the repository's parsed release declarations.
	Declarations *declarations.Releasables
}

// New builds the model of the repository rooted at root (absolute) from its
// parsed declarations.
func New(root string, d *declarations.Releasables) (*Workspace, error) {
	if !filepath.IsAbs(root) {
		return nil, fmt.Errorf("the repository root %q is not absolute", root)
	}
	if d == nil {
		return nil, errors.New("a workspace needs the repository's declarations")
	}
	return &Workspace{Root: filepath.Clean(root), Declarations: d}, nil
}

// Load reads the declarations of the repository rooted at root.
func Load(root string) (*Workspace, error) {
	d, err := declarations.Load(root)
	if err != nil {
		return nil, err
	}
	return New(root, d)
}

// Discover loads the repository containing the directory start.
func Discover(start string) (*Workspace, error) {
	root, err := declarations.FindRepositoryRoot(start)
	if err != nil {
		return nil, err
	}
	return Load(root)
}

// IsWorkspace reports whether the declarations declare a workspace.
func (w *Workspace) IsWorkspace() bool { return w.Declarations.IsWorkspace() }

// Members are every member, in declaration order.
func (w *Workspace) Members() []declarations.Member { return w.Declarations.Members }

// Releasables are every releasable, in declaration order.
func (w *Workspace) Releasables() []declarations.Releasable { return w.Declarations.Releasables }

// RootMember is the member owning the repository root.
func (w *Workspace) RootMember() declarations.Member { return w.Declarations.RootMember() }

// ReleasableOf is the releasable the member is versioned under, and false
// for a member versioned under none.
func (w *Workspace) ReleasableOf(m declarations.Member) (declarations.Releasable, bool) {
	if !m.Versioned() {
		return declarations.Releasable{}, false
	}
	return w.Declarations.Releasable(m.Releasable)
}

// PublishModeOf is the member's publish mode: its releasable's, and none for
// a member versioned under no releasable, since nothing releases it.
func (w *Workspace) PublishModeOf(m declarations.Member) declarations.PublishMode {
	r, ok := w.ReleasableOf(m)
	if !ok {
		return declarations.PublishNone
	}
	return r.PublishMode
}

// MembersOf are the members versioned under the named releasable.
func (w *Workspace) MembersOf(releasable string) []declarations.Member {
	return w.Declarations.MembersOf(releasable)
}

// RelativePath is the canonical repository-relative path of the absolute
// path abs ("." for the root itself). A path outside the repository is
// refused.
func (w *Workspace) RelativePath(abs string) (string, error) {
	resolved := abs
	if real, err := filepath.EvalSymlinks(abs); err == nil {
		resolved = real
	}
	root := w.Root
	if real, err := filepath.EvalSymlinks(root); err == nil {
		root = real
	}
	rel, err := filepath.Rel(root, resolved)
	if err != nil {
		return "", err
	}
	rel = filepath.ToSlash(rel)
	if rel == ".." || strings.HasPrefix(rel, "../") || filepath.IsAbs(rel) {
		return "", fmt.Errorf("%s is outside the repository %s", abs, w.Root)
	}
	return rel, nil
}

// MemberForDirectory is the member whose territory the canonical
// repository-relative directory dir lies in: the most specific member path
// claiming it. includeRoot says whether the root member may answer for a
// directory no other member claims; it is required because the two
// questions differ ("which member owns this?" always has an answer, "which
// member am I standing in?" may be "none, you are at the root").
func (w *Workspace) MemberForDirectory(dir string, includeRoot bool) (declarations.Member, bool) {
	m, ok := w.Declarations.MemberForPath(dir)
	if !ok || (m.IsRoot() && !includeRoot) {
		return declarations.Member{}, false
	}
	return m, true
}

// MemberAtDirectory is the member whose territory the absolute directory
// abs lies in, the root member answering for directories no other member
// claims. A directory outside the repository is refused.
func (w *Workspace) MemberAtDirectory(abs string) (declarations.Member, error) {
	rel, err := w.RelativePath(abs)
	if err != nil {
		return declarations.Member{}, err
	}
	m, _ := w.MemberForDirectory(rel, true)
	return m, nil
}

// ReleasableForDirectory is the releasable the absolute directory abs
// selects: the releasable of the member whose territory it lies in. found is
// false where that member is versioned under none (a dev-node root, say),
// so the caller must name a releasable itself.
func (w *Workspace) ReleasableForDirectory(abs string) (r declarations.Releasable, found bool, err error) {
	m, err := w.MemberAtDirectory(abs)
	if err != nil {
		return declarations.Releasable{}, false, err
	}
	r, found = w.ReleasableOf(m)
	return r, found, nil
}

// NestedMemberPaths are the paths of the members lying inside m's territory,
// sorted: the directories a walk over m's own files leaves out. For the root
// member that is every other member.
func (w *Workspace) NestedMemberPaths(m declarations.Member) []string {
	var nested []string
	for _, other := range w.Members() {
		if other.Path == m.Path || other.IsRoot() {
			continue
		}
		if declarations.IsInside(other.Path, m.Path) {
			nested = append(nested, other.Path)
		}
	}
	sort.Strings(nested)
	return nested
}

// MemberDir is the absolute directory of a member.
func (w *Workspace) MemberDir(m declarations.Member) string {
	return filepath.Join(w.Root, filepath.FromSlash(m.Path))
}

// ReadReleasableVersion reads a workspace releasable's version file. A
// missing or empty file, and a version that is not MAJOR.MINOR.PATCH, are
// errors naming the file.
func (w *Workspace) ReadReleasableVersion(releasable string) (semver.Version, error) {
	rel := declarations.VersionFile(releasable)
	data, err := os.ReadFile(filepath.Join(w.Root, filepath.FromSlash(rel)))
	if errors.Is(err, os.ErrNotExist) {
		return semver.Version{}, fmt.Errorf("the version file of the releasable %q, %s, does not exist", releasable, rel)
	}
	if err != nil {
		return semver.Version{}, fmt.Errorf("reading %s: %w", rel, err)
	}
	text := strings.TrimSpace(string(data))
	if text == "" {
		return semver.Version{}, fmt.Errorf("the version file of the releasable %q, %s, is empty", releasable, rel)
	}
	v, err := semver.Parse(text)
	if err != nil {
		return semver.Version{}, fmt.Errorf("%s: %w", rel, err)
	}
	return v, nil
}

// WriteReleasableVersion writes a workspace releasable's version file
// through the effects handle, atomically.
func (w *Workspace) WriteReleasableVersion(e *strictcli.Effects, releasable string, v semver.Version) error {
	if _, ok := w.Declarations.Releasable(releasable); !ok {
		return fmt.Errorf("no releasable is named %q", releasable)
	}
	rel := declarations.VersionFile(releasable)
	target := filepath.Join(w.Root, filepath.FromSlash(rel))
	if _, err := e.Mkdir(filepath.Dir(target)); err != nil {
		return fmt.Errorf("creating the directory of %s: %w", rel, err)
	}
	temporary := target + ".tmp"
	if _, err := e.Write(temporary, v.String()+"\n"); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	if _, err := e.Rename(temporary, target); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	return nil
}
