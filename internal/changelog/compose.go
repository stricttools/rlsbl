package changelog

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// EntryRequest is the entry `changelog add` or `changelog amend` is asked
// to write.
type EntryRequest struct {
	// Commits are the commit ids as given.
	Commits     []string
	UserFacing  bool
	Description string
	Type        string
	BatchReason string
	// Unresolved takes the commits as given, neither resolved nor checked
	// against the scope (`changelog amend` without hash validation, for an
	// old commit a rewrite took away).
	Unresolved bool
}

// Prepared is an entry ready to write, and what writing it should say.
type Prepared struct {
	Entry Entry
	// Notes are legal overlaps the operator is told about: a commit an
	// existing entry of another type already names.
	Notes []string
}

// Prepare builds the entry req describes for the subject's changelog and
// checks it against existing, the entries of the file it is written to.
// Each commit must resolve and touch something the subject's scope claims;
// a user-facing entry needs a description and a type; an entry over the
// per-entry commit limit needs a batch reason, and an entry within it may
// not carry one; and a commit an existing entry of the same type and
// user-facing value already names is refused. In a workspace the entry's
// packages are the subject's members the commits touch.
func (s Subject) Prepare(req EntryRequest, existing []Entry) (Prepared, error) {
	var commits []string
	for _, c := range req.Commits {
		if c = strings.TrimSpace(c); c != "" {
			commits = append(commits, c)
		}
	}
	if len(commits) == 0 {
		return Prepared{}, errors.New("the entry names no commit: pass at least one commit id")
	}
	if !req.Unresolved {
		res := newResolver(s.Repo)
		for i, c := range commits {
			full, err := res.resolve(c)
			if err != nil {
				return Prepared{}, err
			}
			if full == "" {
				return Prepared{}, fmt.Errorf("the commit %s does not resolve in %s", c, s.Root())
			}
			commits[i] = full
		}
		if err := s.requireInScope(commits); err != nil {
			return Prepared{}, err
		}
	}
	if req.UserFacing && (req.Description == "" || req.Type == "") {
		return Prepared{}, errors.New("a user-facing entry needs a description and a type; mark the entry not user-facing when it describes an internal change")
	}
	if len(commits) > MaxCommitsPerEntry && req.BatchReason == "" {
		return Prepared{}, fmt.Errorf("the entry names %d commits, over the limit of %d per entry. Split it into entries of at most %d commits, or, when the commits are one change, give the entry a batch reason saying why (--batch-reason), which exempts it from the limit", len(commits), MaxCommitsPerEntry, MaxCommitsPerEntry)
	}
	if len(commits) <= MaxCommitsPerEntry && req.BatchReason != "" {
		return Prepared{}, fmt.Errorf("the entry names %d commits, within the limit of %d per entry, so it needs no batch reason; leave the batch reason out", len(commits), MaxCommitsPerEntry)
	}
	entry := Entry{
		ID:          NewID(),
		Commits:     commits,
		UserFacing:  req.UserFacing,
		Description: req.Description,
		Type:        req.Type,
		BatchReason: req.BatchReason,
	}
	if s.Workspace.IsWorkspace() && !req.Unresolved {
		packages, err := s.packagesOf(commits)
		if err != nil {
			return Prepared{}, err
		}
		entry.Packages = packages
	}
	if err := Validate(entry); err != nil {
		return Prepared{}, err
	}
	notes, err := overlaps(existing, entry)
	if err != nil {
		return Prepared{}, err
	}
	return Prepared{Entry: entry, Notes: notes}, nil
}

// requireInScope refuses a commit that touches nothing the subject's scope
// claims, naming who owns its files and where its entry belongs.
func (s Subject) requireInScope(commits []string) error {
	scope := s.Scope()
	kept, err := workspace.FilterCommits(s.Repo, commits, scope)
	if err != nil {
		return err
	}
	in := map[string]bool{}
	for _, c := range kept {
		in[c] = true
	}
	for _, c := range commits {
		if in[c] {
			continue
		}
		owner, err := ForeignOwner(s.Repo, s.Workspace, scope, c)
		if err != nil {
			return err
		}
		message := fmt.Sprintf("the commit %s touches nothing the changelog of %q covers (%s). Every file belongs to one member: the most specific member path declared in %s wins, the root member owns what no other member claims, and a releasable's changelog also covers its own state directories. ", short(c), s.Releasable, scope.Describe(), "releasables.toml")
		if owner == NoDeclaredOwner {
			return errors.New(message + "Every file it touches is tool-owned, so no changelog covers it: commits like this are exempt from changelog coverage, and no entry is added for this one anywhere.")
		}
		return errors.New(message + "Its files belong to " + owner + ": add the entry from a directory of the owning member instead.")
	}
	return nil
}

// packagesOf are the subject's members the commits change a file of, by
// name, sorted; nil when none.
func (s Subject) packagesOf(commits []string) ([]string, error) {
	inScope := map[string]bool{}
	for _, m := range s.Scope().Members() {
		inScope[m.Name] = true
	}
	affected := map[string]bool{}
	for _, c := range commits {
		owners, err := s.Workspace.CommitOwnerNames(s.Repo, c)
		if err != nil {
			return nil, err
		}
		for name := range owners {
			if inScope[name] {
				affected[name] = true
			}
		}
	}
	if len(affected) == 0 {
		return nil, nil
	}
	return sortedKeys(affected), nil
}

// overlaps refuses a commit an existing entry of the same user-facing value
// and type already names, and notes one an entry of another type names (a
// commit may carry both a feature and a fix, bounded by the per-commit
// entry limit).
func overlaps(existing []Entry, entry Entry) ([]string, error) {
	var notes []string
	for _, c := range entry.Commits {
		for _, old := range existing {
			if !containsString(old.Commits, c) {
				continue
			}
			described := old.Description
			if described == "" {
				described = "(no description)"
			}
			if old.UserFacing == entry.UserFacing && old.Type == entry.Type {
				return nil, fmt.Errorf("the commit %s is already covered by entry %s: %s. Nothing was written. Edit that entry with `rlsbl changelog edit --id %s`, or give this one a different type when it describes a different change", short(c), old.ID, described, old.ID)
			}
			notes = append(notes, fmt.Sprintf("the commit %s also appears in entry %s (%s): a commit may carry entries of different types, up to %d entries per commit", short(c), old.ID, described, MaxEntriesPerCommit))
		}
	}
	return notes, nil
}

func containsString(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

// Selector addresses existing entries: by id, or by the commits they name.
// One of the two is set.
type Selector struct {
	ID      string
	Commits []string
}

// Match is one entry a selector addresses.
type Match struct {
	File *File
	// Index is the line's index in File.Lines.
	Index int
}

// Entry is the matched entry.
func (m Match) Entry() Entry { return m.File.Lines[m.Index].Entry }

// Describe names the match for a list of several: where it is, its id, its
// type, and its description.
func (m Match) Describe() string {
	e := m.Entry()
	described := e.Description
	if described == "" {
		described = "(no description)"
	}
	where := "unreleased"
	if m.File.Released {
		where = "v" + m.File.Version.String()
	}
	typ := e.Type
	if typ == "" {
		typ = "(none)"
	}
	return fmt.Sprintf("[%s] entry %s, type %s: %s", where, e.ID, typ, described)
}

// Find is every entry of the subject's changelog the selector addresses:
// the unreleased file first, then each released version's, highest first.
// Commits are resolved first; when requireResolvable is false (a removal,
// usually of the entry whose commits a rewrite took away) a commit that
// does not resolve is matched as written.
func (s Subject) Find(sel Selector, requireResolvable bool) ([]Match, error) {
	if (sel.ID == "") == (len(sel.Commits) == 0) {
		return nil, errors.New("name the entry by its id or by the commits it names, one of the two")
	}
	wanted := map[string]bool{}
	res := newResolver(s.Repo)
	for _, c := range sel.Commits {
		c = strings.TrimSpace(c)
		if c == "" {
			continue
		}
		full, err := res.resolve(c)
		if err != nil {
			return nil, err
		}
		if full == "" {
			if requireResolvable {
				return nil, fmt.Errorf("the commit %s does not resolve in %s", c, s.Root())
			}
			full = c
		}
		wanted[full] = true
	}
	if sel.ID == "" && len(wanted) == 0 {
		return nil, errors.New("the selector names no commit: pass at least one commit id")
	}
	files, err := ReadAll(s.Root(), s.Dir())
	if err != nil {
		return nil, err
	}
	var matches []Match
	for _, f := range files {
		for i, l := range f.Lines {
			hit := sel.ID != "" && l.Entry.ID == sel.ID
			for _, c := range l.Entry.Commits {
				if wanted[c] {
					hit = true
				}
			}
			if hit {
				matches = append(matches, Match{File: f, Index: i})
			}
		}
	}
	if len(matches) == 0 {
		if sel.ID != "" {
			return nil, fmt.Errorf("no changelog entry of %q has the id %s", s.Releasable, sel.ID)
		}
		var named []string
		for c := range wanted {
			named = append(named, short(c))
		}
		sort.Strings(named)
		return nil, fmt.Errorf("no changelog entry of %q names the commits %s", s.Releasable, strings.Join(named, ", "))
	}
	return matches, nil
}

// EditRequest is a sparse update of one entry: a nil field is left as it
// is, and an Unset field is cleared.
type EditRequest struct {
	Description      *string
	Type             *string
	UserFacing       *bool
	UnsetDescription bool
	UnsetType        bool
}

// ApplyEdit is entry with req applied. Making an entry user-facing needs a
// description and a type, already there or written by the same edit. Making
// it not user-facing leaves its description and type on the line (unused
// while it is not user-facing, and kept for a later flip back) unless the
// edit clears them, and a value written in the same edit is not stored.
func ApplyEdit(entry Entry, req EditRequest) (Entry, error) {
	if req.Description == nil && req.Type == nil && req.UserFacing == nil && !req.UnsetDescription && !req.UnsetType {
		return Entry{}, errors.New("the edit changes nothing: give a description, a type, or the user-facing value, or clear the description or the type")
	}
	if req.Description != nil && req.UnsetDescription {
		return Entry{}, errors.New("the edit both writes and clears the description; do one")
	}
	if req.Type != nil && req.UnsetType {
		return Entry{}, errors.New("the edit both writes and clears the type; do one")
	}
	out := entry
	out.Commits = append([]string(nil), entry.Commits...)
	turnsOff := req.UserFacing != nil && !*req.UserFacing
	if req.UserFacing != nil {
		out.UserFacing = *req.UserFacing
	}
	if req.UnsetType {
		out.Type = ""
	} else if req.Type != nil && !turnsOff {
		out.Type = *req.Type
	}
	if req.UnsetDescription {
		out.Description = ""
	} else if req.Description != nil && !turnsOff {
		out.Description = *req.Description
	}
	if out.UserFacing && (out.Description == "" || out.Type == "") {
		return Entry{}, errors.New("a user-facing entry needs a description and a type: write the missing ones in the same edit")
	}
	if err := Validate(out); err != nil {
		return Entry{}, err
	}
	return out, nil
}

// Replace writes entry in place of the matched line, keeping every other
// line and the file's mode (a released file stays read-only). A file
// changed since it was read is refused.
func Replace(e *strictcli.Effects, root string, m Match, entry Entry) error {
	if err := Validate(entry); err != nil {
		return err
	}
	lines := texts(m.File)
	lines[m.Index] = Serialize(entry)
	return rewrite(e, root, m.File, lines)
}

// Remove deletes the matched line, keeping every other line and the
// file's mode. A file changed since it was read is refused.
func Remove(e *strictcli.Effects, root string, m Match) error {
	lines := texts(m.File)
	lines = append(lines[:m.Index:m.Index], lines[m.Index+1:]...)
	return rewrite(e, root, m.File, lines)
}

// Amend adds a prepared entry to released version v's file, which must
// exist; the entry was prepared against that file's entries.
func (s Subject) Amend(e *strictcli.Effects, v semver.Version, entry Entry) error {
	return AppendToVersion(e, s.Root(), s.Dir(), v, entry)
}

// Add adds a prepared entry to the unreleased file.
func (s Subject) Add(e *strictcli.Effects, entry Entry) error {
	return AppendEntry(e, s.Root(), s.Dir(), entry)
}
