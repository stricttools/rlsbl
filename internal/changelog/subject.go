package changelog

import (
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// Subject is one releasable's changelog in its repository, with what
// validating it asks of the repository.
type Subject struct {
	Repo      git.Repo
	Workspace *workspace.Workspace
	// Releasable is a releasable the declarations declare.
	Releasable string
	// Record is the releasable's release record; the nearest release it
	// holds bounds the unreleased range.
	Record *releaserecord.Record
	// Exclude are revisions whose history is not this repository's own (a
	// fork's upstream branch and the tags it inherited), left out of the
	// unreleased range: those commits are the upstream's to describe.
	Exclude []string
}

// NewSubject is the changelog of releasable, which must be declared.
// upstream is the URL of the repository this one is a fork of, empty for a
// repository that is no fork, and exclude the revisions of the fork's
// inherited history (both from the fork's upstream declaration).
func NewSubject(repo git.Repo, w *workspace.Workspace, releasable, upstream string, exclude []string) (Subject, error) {
	r, ok := w.Declarations.Releasable(releasable)
	if !ok {
		var names []string
		for _, r := range w.Releasables() {
			names = append(names, r.Name)
		}
		return Subject{}, fmt.Errorf("no releasable is named %q; the releasables are %s", releasable, strings.Join(names, ", "))
	}
	scheme, err := workspace.SchemeOf(r)
	if err != nil {
		return Subject{}, err
	}
	return Subject{
		Repo:       repo,
		Workspace:  w,
		Releasable: releasable,
		Record:     releaserecord.New(repo, releasable, scheme, upstream),
		Exclude:    append([]string(nil), exclude...),
	}, nil
}

// SubjectAt names the releasable whose changelog the absolute directory dir
// selects: the releasable of the member whose territory dir lies in. A
// member versioned under no releasable has no changelog, and is refused.
func SubjectAt(w *workspace.Workspace, dir string) (string, error) {
	m, err := w.MemberAtDirectory(dir)
	if err != nil {
		return "", err
	}
	r, ok := w.ReleasableOf(m)
	if !ok {
		return "", fmt.Errorf("the member %q (path %q) is versioned under no releasable, and only a releasable has a changelog: run the command from a directory of a member that is versioned under one", m.Name, m.Path)
	}
	return r.Name, nil
}

// Dir is the subject's changelog directory, repository-relative.
func (s Subject) Dir() string { return Dir(s.Releasable) }

// Root is the repository root.
func (s Subject) Root() string { return s.Workspace.Root }

// Scope is what the subject's changelog covers: its members' files and the
// releasable's own state paths.
func (s Subject) Scope() workspace.Scope { return s.Workspace.ScopeOfReleasable(s.Releasable) }

// Range is a releasable's unreleased range: the commits HEAD reaches that
// the nearest release it contains does not, leaving out the history the
// subject excludes.
type Range struct {
	// Base is the nearest release's commit, and empty when the record holds
	// no release this checkout contains (every commit is unreleased).
	Base string
	Head string
	// Commits are newest first.
	Commits []string
	in      map[string]bool
}

// Contains reports whether the full commit id sha is in the range.
func (r Range) Contains(sha string) bool { return r.in[sha] }

// UnreleasedRange computes the subject's unreleased range from the nearest
// release its record holds.
func (s Subject) UnreleasedRange() (Range, error) {
	head, err := s.Repo.Head()
	if err != nil {
		return Range{}, err
	}
	nearest, err := s.Record.Nearest(head)
	if err != nil {
		return Range{}, err
	}
	rng := Range{Head: head, in: map[string]bool{}}
	exclude := append([]string(nil), s.Exclude...)
	if nearest != nil {
		rng.Base = nearest.ReleaseCommit
		exclude = append([]string{nearest.ReleaseCommit}, exclude...)
	}
	commits, err := s.Repo.Commits([]string{head}, exclude)
	if err != nil {
		return Range{}, err
	}
	rng.Commits = commits
	for _, c := range commits {
		rng.in[c] = true
	}
	return rng, nil
}

// resolver resolves possibly abbreviated commit ids, once each.
type resolver struct {
	repo git.Repo
	seen map[string]string
}

func newResolver(repo git.Repo) *resolver { return &resolver{repo: repo, seen: map[string]string{}} }

// resolve is the full id of the commit h names, and empty when it names
// none in the repository.
func (r *resolver) resolve(h string) (string, error) {
	if full, ok := r.seen[h]; ok {
		return full, nil
	}
	full, found, err := r.repo.ResolveCommit(h)
	if err != nil {
		return "", err
	}
	if !found {
		full = ""
	}
	r.seen[h] = full
	return full, nil
}

// NoDeclaredOwner is what ForeignOwner answers when no member and no
// releasable claims any file of the commit: every path it touches is
// tool-owned.
const NoDeclaredOwner = "no member or releasable this repository declares"

// ForeignOwner names, in the declarations' own words, who owns the files of
// commit sha that scope does not claim: members (with the releasable each
// is versioned under) and releasables' own state directories, which belong
// to no member.
func ForeignOwner(repo git.Repo, w *workspace.Workspace, scope workspace.Scope, sha string) (string, error) {
	files, err := repo.CommitFiles(sha)
	if err != nil {
		return "", err
	}
	members := map[string]string{}
	stateOwners := map[string]bool{}
	for _, f := range files {
		if scope.Claims(f) {
			continue
		}
		if m, ok := w.OwnerOf(f); ok {
			members[m.Name] = m.Releasable
			continue
		}
		if r, ok := workspace.StateDirReleasable(f); ok {
			stateOwners[r] = true
		}
	}
	var parts []string
	for _, name := range sortedKeys(members) {
		if r := members[name]; r != "" {
			parts = append(parts, fmt.Sprintf("member %q (releasable %q)", name, r))
		} else {
			parts = append(parts, fmt.Sprintf("member %q (in no releasable)", name))
		}
	}
	for _, name := range sortedKeys(stateOwners) {
		parts = append(parts, fmt.Sprintf("the state directories of releasable %q", name))
	}
	if len(parts) == 0 {
		return NoDeclaredOwner, nil
	}
	return strings.Join(parts, ", "), nil
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// Home is the repository-relative path of a releasable's CHANGELOG.md: the
// root CHANGELOG.md in a standalone repository, and the file in the
// releasable's changelog directory in a workspace, whose root CHANGELOG.md
// is the roll-up of every releasable.
func Home(d *declarations.Releasables, releasable string) string {
	if d.IsWorkspace() {
		return Dir(releasable) + "/" + MarkdownName
	}
	return MarkdownName
}

// MarkdownName is the generated changelog's file name.
const MarkdownName = "CHANGELOG.md"

// RollUpPath is a workspace's roll-up, repository-relative.
const RollUpPath = MarkdownName
