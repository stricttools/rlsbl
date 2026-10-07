package declarations

import (
	"fmt"
	"reflect"
	"strconv"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
)

// Editor edits release declarations in place. Each edit changes only the
// tables and keys it names: comments, key order, and every other table keep
// their bytes. Edits may pass through states the declaration rules refuse
// (a member naming a releasable the next edit adds); Result parses the
// outcome and refuses it whole.
//
// Every edit checks what it did against what it was asked to do, by
// decoding the document again, so an edit the document library carried out
// differently from its contract is an error rather than a corrupted file.
type Editor struct {
	data []byte
}

// NewEditor starts editing data, which must parse.
func NewEditor(data []byte) (*Editor, error) {
	if _, err := Parse(data); err != nil {
		return nil, err
	}
	return &Editor{data: append([]byte(nil), data...)}, nil
}

// Result is the edited document and its declarations, refused when the
// declaration rules refuse it.
func (ed *Editor) Result() ([]byte, *Releasables, error) {
	d, err := Parse(ed.data)
	if err != nil {
		return nil, nil, fmt.Errorf("the edited declarations are refused: %w", err)
	}
	return append([]byte(nil), ed.data...), d, nil
}

// decoded is the document as the strict decode reads it, without the
// declaration rules.
func (ed *Editor) decoded() (*Releasables, error) {
	raw, err := tomledit.Unmarshal[rawReleasables](ed.data)
	if err != nil {
		return nil, fmt.Errorf("the edited declarations no longer decode: %w", err)
	}
	d, problems := raw.convert()
	if len(problems) > 0 {
		return nil, refuse(ReleasablesFile, problems)
	}
	return d, nil
}

// apply runs f on the parsed document and keeps what it renders.
func (ed *Editor) apply(f func(doc *tomledit.Document) error) error {
	doc, err := tomledit.Parse(ed.data)
	if err != nil {
		return err
	}
	if err := f(doc); err != nil {
		return err
	}
	ed.data = doc.Bytes()
	return nil
}

func (ed *Editor) memberIndex(d *Releasables, p string) (int, error) {
	for i, m := range d.Members {
		if m.Path == p {
			return i, nil
		}
	}
	var paths []string
	for _, m := range d.Members {
		paths = append(paths, strconv.Quote(m.Path))
	}
	return 0, fmt.Errorf("no member is declared at %q; the members are at %s", p, strings.Join(paths, ", "))
}

func (ed *Editor) releasableIndex(d *Releasables, name string) (int, error) {
	for i, r := range d.Releasables {
		if r.Name == name {
			return i, nil
		}
	}
	var names []string
	for _, r := range d.Releasables {
		names = append(names, r.Name)
	}
	return 0, fmt.Errorf("no releasable is named %q; the releasables are %s", name, strings.Join(names, ", "))
}

// verify compares the declarations after an edit with what the edit was
// asked to produce.
func (ed *Editor) verify(edit string, want *Releasables) error {
	got, err := ed.decoded()
	if err != nil {
		return err
	}
	if !reflect.DeepEqual(normalized(got), normalized(want)) {
		return fmt.Errorf("%s produced declarations other than the ones asked for; nothing was written", edit)
	}
	return nil
}

// normalized renders declarations for comparison: two declarations that
// render alike say the same thing.
func normalized(d *Releasables) string { return string(Render(d)) }

// clone copies declarations deeply enough for an edit's expected outcome.
func clone(d *Releasables) *Releasables {
	c := *d
	c.Releasables = append([]Releasable(nil), d.Releasables...)
	c.Members = append([]Member(nil), d.Members...)
	return &c
}

// SetMemberReleasable versions the member at path under the named
// releasable, or under none when name is empty (releasable = false).
func (ed *Editor) SetMemberReleasable(path, name string) error {
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	i, err := ed.memberIndex(d, path)
	if err != nil {
		return err
	}
	var value any = false
	if name != "" {
		value = name
	}
	if err := ed.apply(func(doc *tomledit.Document) error {
		return doc.Set(fmt.Sprintf("members[%d].releasable", i), value)
	}); err != nil {
		return err
	}
	want := clone(d)
	want.Members[i].Releasable = name
	return ed.verify("setting a member's releasable", want)
}

// RenameReleasable renames a releasable and every member's reference to it.
func (ed *Editor) RenameReleasable(oldName, newName string) error {
	if problem := NameProblem(newName); problem != "" {
		return fmt.Errorf("a releasable cannot be renamed to %q: %s", newName, problem)
	}
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	i, err := ed.releasableIndex(d, oldName)
	if err != nil {
		return err
	}
	if _, taken := d.Releasable(newName); taken {
		return fmt.Errorf("a releasable named %q is already declared", newName)
	}
	want := clone(d)
	want.Releasables[i].Name = newName
	if err := ed.apply(func(doc *tomledit.Document) error {
		if err := doc.Set(fmt.Sprintf("releasables[%d].name", i), newName); err != nil {
			return err
		}
		for j, m := range d.Members {
			if m.Releasable != oldName {
				continue
			}
			want.Members[j].Releasable = newName
			if err := doc.Set(fmt.Sprintf("members[%d].releasable", j), newName); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		return err
	}
	return ed.verify("renaming a releasable", want)
}

// SetInternalDepFloors sets the internal_dep_floors of the member at path;
// an empty list deletes the key.
func (ed *Editor) SetInternalDepFloors(path string, floors []string) error {
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	i, err := ed.memberIndex(d, path)
	if err != nil {
		return err
	}
	key := fmt.Sprintf("members[%d].internal_dep_floors", i)
	if err := ed.apply(func(doc *tomledit.Document) error {
		if len(floors) == 0 {
			return doc.Delete(key)
		}
		return doc.Set(key, append([]string(nil), floors...))
	}); err != nil {
		return err
	}
	want := clone(d)
	want.Members[i].InternalDepFloors = nil
	if len(floors) > 0 {
		want.Members[i].InternalDepFloors = append([]string(nil), floors...)
	}
	return ed.verify("setting internal_dep_floors", want)
}

// RemoveMember deletes the member at path, with its pipelines.
func (ed *Editor) RemoveMember(path string) error {
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	i, err := ed.memberIndex(d, path)
	if err != nil {
		return err
	}
	if err := ed.apply(func(doc *tomledit.Document) error {
		// Deleting an entry of an array of tables leaves the headers of the
		// tables nested in it, so the pipelines go first, by their key.
		if err := doc.Delete(fmt.Sprintf("members[%d].pipelines", i)); err != nil {
			return err
		}
		return doc.Delete(fmt.Sprintf("members[%d]", i))
	}); err != nil {
		return err
	}
	want := clone(d)
	want.Members = append(want.Members[:i:i], d.Members[i+1:]...)
	return ed.verify("removing a member", want)
}

// RemoveReleasable deletes the named releasable's table. Removing the last
// one leaves releasables = [], which a workspace with no releasables
// declares.
func (ed *Editor) RemoveReleasable(name string) error {
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	i, err := ed.releasableIndex(d, name)
	if err != nil {
		return err
	}
	if err := ed.apply(func(doc *tomledit.Document) error {
		if err := doc.Delete(fmt.Sprintf("releasables[%d]", i)); err != nil {
			return err
		}
		if len(d.Releasables) == 1 {
			return doc.Set("releasables", []string{})
		}
		return nil
	}); err != nil {
		return err
	}
	want := clone(d)
	want.Releasables = append(want.Releasables[:i:i], d.Releasables[i+1:]...)
	return ed.verify("removing a releasable", want)
}

// AddReleasable appends a releasable's table after the last one.
func (ed *Editor) AddReleasable(r Releasable) error {
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	if _, taken := d.Releasable(r.Name); taken {
		return fmt.Errorf("a releasable named %q is already declared", r.Name)
	}
	if len(d.Releasables) == 0 {
		// The empty list is a key of the document's own; the tables replace it.
		if err := ed.apply(func(doc *tomledit.Document) error { return doc.Delete("releasables") }); err != nil {
			return err
		}
	}
	if err := ed.insertAfterGroup("releasables", renderReleasable(r)); err != nil {
		return err
	}
	want := clone(d)
	want.Releasables = append(want.Releasables, r)
	return ed.verify("adding a releasable", want)
}

// AddMember appends a member's table, with its pipelines, after the last
// member.
func (ed *Editor) AddMember(m Member) error {
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	if _, taken := d.MemberAt(m.Path); taken {
		return fmt.Errorf("a member is already declared at %q", m.Path)
	}
	if err := ed.insertAfterGroup("members", renderMember(m)); err != nil {
		return err
	}
	want := clone(d)
	want.Members = append(want.Members, m)
	return ed.verify("adding a member", want)
}

// insertAfterGroup inserts text after the last top-level table whose header
// starts with key ([[key]] and the tables under it), or at the end of the
// document when there is none. An array of tables collects its entries
// wherever their headers sit, so the end of the document is a place one may
// continue.
func (ed *Editor) insertAfterGroup(key, text string) error {
	doc, err := tomledit.Parse(ed.data)
	if err != nil {
		return err
	}
	at := len(ed.data)
	found := false
	for _, node := range doc.Children() {
		var path []string
		var body []tomledit.Node
		switch n := node.(type) {
		case *tomledit.ArrayTableNode:
			path, body = n.KeyPath(), n.Children()
		case *tomledit.TableNode:
			path, body = n.KeyPath(), n.Children()
		default:
			continue
		}
		if len(path) > 0 && path[0] == key {
			// A table's span covers its header only; its body ends with the
			// line of its last child.
			at = node.Span().End.Offset
			for _, child := range body {
				if end := child.Span().End.Offset; end > at {
					at = end
				}
			}
			if nl := strings.IndexByte(string(ed.data[at:]), '\n'); nl >= 0 {
				at += nl + 1
			} else {
				at = len(ed.data)
			}
			found = true
		}
	}
	prefix := "\n"
	if !found && len(ed.data) > 0 && !strings.HasSuffix(string(ed.data), "\n") {
		prefix = "\n\n"
	}
	out := make([]byte, 0, len(ed.data)+len(text)+2)
	out = append(out, ed.data[:at]...)
	out = append(out, prefix+text...)
	out = append(out, ed.data[at:]...)
	ed.data = out
	return nil
}
