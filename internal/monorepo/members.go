package monorepo

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// Remove deletes the declaration of the member at path (the path as
// releasables.toml writes it) and writes the declarations. The member's
// files are left on disk. The root member, the last member of a releasable,
// a member another member's depends_on names, and a member the
// lifecycle-and-license record holds open or pending entries for are
// refused, each naming its way out.
func Remove(e *strictcli.Effects, ws *workspace.Workspace, path string) (declarations.Member, error) {
	m, ok := ws.Declarations.MemberAt(path)
	if !ok {
		var known []string
		for _, other := range ws.Members() {
			known = append(known, fmt.Sprintf("%q (%s)", other.Path, other.Name))
		}
		return declarations.Member{}, fmt.Errorf("no member is declared at %q. The members, by path: %s. Pass one of those paths as written", path, strings.Join(known, ", "))
	}
	if m.IsRoot() {
		return declarations.Member{}, fmt.Errorf("the root member %q owns every file no other member claims, and every repository declares it; it cannot be removed", m.Name)
	}
	if m.Versioned() && len(ws.MembersOf(m.Releasable)) == 1 {
		return declarations.Member{}, fmt.Errorf("the member %q is the only member versioned under the releasable %q, which would then release nothing, and the declarations refuse a releasable no member is versioned under. Version another member under it first (`rlsbl monorepo add <path> --releasable %s`), or delete the releasable's [[releasables]] table together with this member's [[members]] table in %s", m.Name, m.Releasable, m.Releasable, declarations.ReleasablesFile)
	}
	var dependents []string
	for _, other := range ws.Members() {
		for _, dep := range other.DependsOn {
			if dep == m.Name {
				dependents = append(dependents, other.Name)
			}
		}
	}
	if len(dependents) > 0 {
		return declarations.Member{}, fmt.Errorf("the members %s name %q in their depends_on; delete it from their depends_on in %s first", strings.Join(dependents, ", "), m.Name, declarations.ReleasablesFile)
	}
	rec, err := lifecycle.Load(ws.Root)
	if err != nil {
		return declarations.Member{}, err
	}
	if entries := openEntriesOf(rec, m.Name); len(entries) > 0 {
		return declarations.Member{}, fmt.Errorf("%s holds %s of the member %q, and an open or pending entry names a declared subject; close or delete them first", lifecycle.RecordFile, strings.Join(entries, " and "), m.Name)
	}
	data, err := os.ReadFile(filepath.Join(ws.Root, filepath.FromSlash(declarations.ReleasablesFile)))
	if err != nil {
		return declarations.Member{}, fmt.Errorf("reading %s: %w", declarations.ReleasablesFile, err)
	}
	ed, err := declarations.NewEditor(data)
	if err != nil {
		return declarations.Member{}, err
	}
	if err := ed.RemoveMember(m.Path); err != nil {
		return declarations.Member{}, err
	}
	edited, _, err := ed.Result()
	if err != nil {
		return declarations.Member{}, err
	}
	if _, err := declarations.Write(e, ws.Root, edited); err != nil {
		return declarations.Member{}, err
	}
	return m, nil
}

// openEntriesOf describes the record's open and pending entries whose
// subject is subject.
func openEntriesOf(rec *lifecycle.Record, subject string) []string {
	var out []string
	for _, l := range rec.Lifecycle() {
		if l.Subject == subject && l.Open() {
			out = append(out, "an open lifecycle period")
		}
	}
	for _, l := range rec.Licenses() {
		if l.Subject == subject && l.Open() {
			out = append(out, "an open license period")
		}
	}
	for _, id := range rec.Identities() {
		switch {
		case id.Subject != subject:
		case id.Pending():
			out = append(out, "a pending "+string(id.Facet)+" identity")
		case id.Open():
			out = append(out, "an open "+string(id.Facet)+" identity")
		}
	}
	return out
}

// ListedMember is one member as `monorepo list` reports it: what the member
// declares.
type ListedMember struct {
	Name string `json:"name"`
	Path string `json:"path"`
	// Releasable is null for a member versioned under no releasable.
	Releasable *string `json:"releasable"`
	Library    bool    `json:"library"`
	DevOnly    bool    `json:"dev_only"`
	TestOnly   bool    `json:"test_only"`
}

// List is every member, in declaration order.
func List(ws *workspace.Workspace) []ListedMember {
	out := []ListedMember{}
	for _, m := range ws.Members() {
		l := ListedMember{Name: m.Name, Path: m.Path, Library: m.Library, DevOnly: m.DevOnly, TestOnly: m.TestOnly}
		if m.Versioned() {
			r := m.Releasable
			l.Releasable = &r
		}
		out = append(out, l)
	}
	return out
}

// memberNames are the names of every member, sorted.
func memberNames(ws *workspace.Workspace) []string {
	var names []string
	for _, m := range ws.Members() {
		names = append(names, m.Name)
	}
	sort.Strings(names)
	return names
}
