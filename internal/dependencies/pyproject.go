package dependencies

import (
	"errors"
	"fmt"
	"os"

	tomledit "github.com/stricttools/go-toml-edit"
)

// Family is one section family a dependency can be declared in.
type Family string

// The families. Which ones a caller visits is always stated: the PyPI build
// rewrites what reaches published metadata, `rewrite uv-path-sources`
// rewrites every declaration, since a dev-only path source is as
// unbuildable for a consumer's checkout as a runtime one.
const (
	// FamilyDependencies is [project].dependencies.
	FamilyDependencies Family = "dependencies"
	// FamilyOptional is every [project.optional-dependencies] extra.
	FamilyOptional Family = "optional-dependencies"
	// FamilyGroups is every PEP 735 [dependency-groups] group.
	FamilyGroups Family = "dependency-groups"
)

// PublishedFamilies are the sections whose contents reach a consumer
// through published metadata.
var PublishedFamilies = []Family{FamilyDependencies, FamilyOptional}

// AllFamilies are every section a dependency can be declared in.
var AllFamilies = []Family{FamilyDependencies, FamilyOptional, FamilyGroups}

// SourceType is how a [tool.uv.sources] entry resolves a dependency from a
// local checkout.
type SourceType string

// The local source types. Registry-neutral sources (git, url, index) are not
// local: they resolve to something a consumer can fetch.
const (
	SourcePath      SourceType = "path"
	SourceWorkspace SourceType = "workspace"
)

// Pyproject is a pyproject.toml held for reading and for edits that keep
// every byte they do not change.
type Pyproject struct {
	// Path names the file in errors.
	Path string
	doc  *tomledit.Document
}

// ReadPyproject reads the pyproject.toml at path. A missing file is an error
// naming it, and so is one that does not parse.
func ReadPyproject(path string) (*Pyproject, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("there is no %s", path)
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}
	return ParsePyproject(path, data)
}

// ParsePyproject parses pyproject.toml text; path names it in errors.
func ParsePyproject(path string, data []byte) (*Pyproject, error) {
	doc, err := tomledit.Parse(data)
	if err != nil {
		return nil, fmt.Errorf("%s does not parse as TOML: %w", path, err)
	}
	return &Pyproject{Path: path, doc: doc}, nil
}

// Bytes is the document as it stands, every untouched byte kept.
func (p *Pyproject) Bytes() []byte { return p.doc.Bytes() }

// HasProject reports whether the document has a [project] table.
func (p *Pyproject) HasProject() bool {
	_, ok := p.record("project")
	return ok
}

// keyPath renders keys as one path in go-toml-edit's syntax, each key
// quoted as it needs.
func keyPath(keys ...string) string {
	segments := make([]tomledit.PathSegment, len(keys))
	for i, k := range keys {
		// A segment's zero value addresses a key, not an index.
		segments[i] = tomledit.PathSegment{Key: k}
	}
	return tomledit.JoinPath(segments)
}

// indexPath is path's element at index.
func indexPath(path string, index int) string {
	return fmt.Sprintf("%s[%d]", path, index)
}

// record is the table at keys, and false when any key is absent or not a
// table.
func (p *Pyproject) record(keys ...string) (*tomledit.Record, bool) {
	current := p.doc.Root()
	for _, k := range keys {
		entry, ok := current.Get(k)
		if !ok {
			return nil, false
		}
		if current, ok = entry.Record(); !ok {
			return nil, false
		}
	}
	return current, true
}

// keysOf are the keys of the table at keys, in document order.
func (p *Pyproject) keysOf(keys ...string) []string {
	r, ok := p.record(keys...)
	if !ok {
		return nil
	}
	var out []string
	for e := range r.Entries() {
		out = append(out, e.Key())
	}
	return out
}

// value is the decoded value at keys; found is false when there is none.
func (p *Pyproject) value(keys ...string) (any, bool, error) {
	decoded, err := tomledit.Decode[map[string]any](p.doc)
	if err != nil {
		return nil, false, fmt.Errorf("%s: %w", p.Path, err)
	}
	var current any = *decoded
	for _, k := range keys {
		t, ok := current.(map[string]any)
		if !ok {
			return nil, false, nil
		}
		if current, ok = t[k]; !ok {
			return nil, false, nil
		}
	}
	return current, true, nil
}

// DependencyArray is one array of requirement strings, live in the
// document.
type DependencyArray struct {
	// Label names the section: "dependencies",
	// "optional-dependencies.<extra>", or "dependency-groups.<group>".
	Label string
	// path addresses the array in the document.
	path string
	// Entries are the array's elements: a string for a requirement, any
	// other value (a PEP 735 include-group table) as it decodes.
	Entries []any
}

// DependencyArrays are the dependency arrays of the families, in document
// order. A section that is present and not an array is refused.
func (p *Pyproject) DependencyArrays(families []Family) ([]DependencyArray, error) {
	visits := map[Family]bool{}
	for _, f := range families {
		visits[f] = true
	}
	var out []DependencyArray
	add := func(label string, keys ...string) error {
		raw, found, err := p.value(keys...)
		if err != nil || !found {
			return err
		}
		entries, ok := raw.([]any)
		if !ok {
			return fmt.Errorf("%s: %s is not an array of requirements", p.Path, label)
		}
		out = append(out, DependencyArray{Label: label, path: keyPath(keys...), Entries: entries})
		return nil
	}
	if visits[FamilyDependencies] {
		if err := add("dependencies", "project", "dependencies"); err != nil {
			return nil, err
		}
	}
	if visits[FamilyOptional] {
		for _, extra := range p.keysOf("project", "optional-dependencies") {
			if err := add("optional-dependencies."+extra, "project", "optional-dependencies", extra); err != nil {
				return nil, err
			}
		}
	}
	if visits[FamilyGroups] {
		for _, group := range p.keysOf("dependency-groups") {
			if err := add("dependency-groups."+group, "dependency-groups", group); err != nil {
				return nil, err
			}
		}
	}
	return out, nil
}

// DeclaredEntry is one requirement string of a dependency array.
type DeclaredEntry struct {
	// Section is the array's label.
	Section string
	// Index is the entry's position in its array.
	Index       int
	Original    string
	Requirement Requirement
}

// Entries are every requirement of the families that parses, in document
// order.
func (p *Pyproject) Entries(families []Family) ([]DeclaredEntry, error) {
	arrays, err := p.DependencyArrays(families)
	if err != nil {
		return nil, err
	}
	var out []DeclaredEntry
	for _, a := range arrays {
		for i, raw := range a.Entries {
			text, ok := raw.(string)
			if !ok {
				continue
			}
			req, ok := ParseRequirement(text)
			if !ok {
				continue
			}
			out = append(out, DeclaredEntry{Section: a.Label, Index: i, Original: text, Requirement: req})
		}
	}
	return out, nil
}

// DirectReferences are the requirements of the families that name a
// direct reference ("name @ <url>") instead of a version.
func (p *Pyproject) DirectReferences(families []Family) ([]DeclaredEntry, error) {
	entries, err := p.Entries(families)
	if err != nil {
		return nil, err
	}
	var out []DeclaredEntry
	for _, e := range entries {
		if e.Requirement.DirectReference {
			out = append(out, e)
		}
	}
	return out, nil
}

// EntriesNaming are the requirements of the families whose PEP 503
// normalized name is in names, whatever their form.
func (p *Pyproject) EntriesNaming(names map[string]bool, families []Family) ([]DeclaredEntry, error) {
	entries, err := p.Entries(families)
	if err != nil {
		return nil, err
	}
	var out []DeclaredEntry
	for _, e := range entries {
		if names[e.Requirement.Normalized()] {
			out = append(out, e)
		}
	}
	return out, nil
}

// rewriteEntries replaces each requirement of the families that pick
// accepts with what it returns, and counts the replacements.
func (p *Pyproject) rewriteEntries(families []Family, pick func(r Requirement) (string, bool)) (int, error) {
	arrays, err := p.DependencyArrays(families)
	if err != nil {
		return 0, err
	}
	changed := 0
	for _, a := range arrays {
		for i, raw := range a.Entries {
			text, ok := raw.(string)
			if !ok {
				continue
			}
			req, ok := ParseRequirement(text)
			if !ok {
				continue
			}
			replacement, ok := pick(req)
			if !ok {
				continue
			}
			if err := p.doc.Set(indexPath(a.path, i), replacement); err != nil {
				return changed, fmt.Errorf("%s: rewriting %s entry %d: %w", p.Path, a.Label, i, err)
			}
			changed++
		}
	}
	return changed, nil
}

// FloorEntries rewrites every requirement of the families whose normalized
// name floors names to "name[extras]>=<floor>" with its marker, whatever
// its current form: a bare "sibling", "sibling>=0.1", and "sibling @
// file:///..." all take the floor, because the floor must be the locked
// version, not what the manifest said while the dependency resolved from a
// checkout. It returns the number of entries rewritten.
func (p *Pyproject) FloorEntries(floors map[string]string, families []Family) (int, error) {
	return p.rewriteEntries(families, func(r Requirement) (string, bool) {
		version, ok := floors[r.Normalized()]
		if !ok {
			return "", false
		}
		return r.WithFloor(version), true
	})
}

// RewriteDirectReferences rewrites every direct reference of the families
// whose normalized name constraints names to that constraint (">=1.2.0"),
// keeping its extras and marker. A requirement already naming a version is
// left alone. It returns the number rewritten.
func (p *Pyproject) RewriteDirectReferences(constraints map[string]string, families []Family) (int, error) {
	return p.rewriteEntries(families, func(r Requirement) (string, bool) {
		constraint, ok := constraints[r.Normalized()]
		if !r.DirectReference || !ok {
			return "", false
		}
		return r.WithConstraint(constraint), true
	})
}

// UvSource is one [tool.uv.sources] entry that resolves its package from a
// local checkout.
type UvSource struct {
	// Name is the key as declared.
	Name string
	Type SourceType
}

// localSourceType is the local source type of one source table, and false
// for a registry-neutral one.
func localSourceType(entry any) (SourceType, bool) {
	t, ok := entry.(map[string]any)
	if !ok {
		return "", false
	}
	if _, isPath := t["path"]; isPath {
		return SourcePath, true
	}
	if ws, _ := t["workspace"].(bool); ws {
		return SourceWorkspace, true
	}
	return "", false
}

// sourceElements are a source declaration's tables: the table itself, or
// each element of a list of marker-gated tables.
func sourceElements(spec any) []any {
	if list, ok := spec.([]any); ok {
		return list
	}
	return []any{spec}
}

// UvLocalSources are the [tool.uv.sources] entries that resolve from a path
// or a workspace member, in document order. A source declared as a list of
// marker-gated tables counts when any of its elements is local.
func (p *Pyproject) UvLocalSources() ([]UvSource, error) {
	var out []UvSource
	for _, name := range p.keysOf("tool", "uv", "sources") {
		spec, _, err := p.value("tool", "uv", "sources", name)
		if err != nil {
			return nil, err
		}
		for _, element := range sourceElements(spec) {
			if t, ok := localSourceType(element); ok {
				out = append(out, UvSource{Name: name, Type: t})
				break
			}
		}
	}
	return out, nil
}

// RemoveUvSources removes the local sources of the named packages (names as
// declared) and returns how many names lost one. A list of marker-gated
// tables is pruned, not deleted: only its local elements go, so an index
// variant for the platforms the checkout does not cover stays; a list whose
// every element is local is deleted like a plain table. A sources table
// left empty is removed, and an emptied [tool.uv] and [tool] with it, so
// no header is left for nothing.
func (p *Pyproject) RemoveUvSources(names []string) (int, error) {
	declared := map[string]bool{}
	for _, n := range p.keysOf("tool", "uv", "sources") {
		declared[n] = true
	}
	removed := 0
	for _, name := range names {
		if !declared[name] {
			continue
		}
		path := keyPath("tool", "uv", "sources", name)
		spec, _, err := p.value("tool", "uv", "sources", name)
		if err != nil {
			return removed, err
		}
		list, isList := spec.([]any)
		if !isList {
			if _, local := localSourceType(spec); !local {
				continue
			}
			if err := p.doc.Delete(path); err != nil {
				return removed, fmt.Errorf("%s: removing %s: %w", p.Path, path, err)
			}
			removed++
			continue
		}
		var doomed []int
		for i, element := range list {
			if _, local := localSourceType(element); local {
				doomed = append(doomed, i)
			}
		}
		if len(doomed) == 0 {
			continue
		}
		if len(doomed) == len(list) {
			if err := p.doc.Delete(path); err != nil {
				return removed, fmt.Errorf("%s: removing %s: %w", p.Path, path, err)
			}
		} else {
			// Back to front, so the remaining indices stay valid.
			for j := len(doomed) - 1; j >= 0; j-- {
				if err := p.doc.Delete(indexPath(path, doomed[j])); err != nil {
					return removed, fmt.Errorf("%s: removing %s: %w", p.Path, indexPath(path, doomed[j]), err)
				}
			}
		}
		removed++
	}
	if removed == 0 {
		return 0, nil
	}
	for _, keys := range [][]string{{"tool", "uv", "sources"}, {"tool", "uv"}, {"tool"}} {
		r, ok := p.record(keys...)
		if !ok || r.Len() > 0 {
			break
		}
		if err := p.doc.Delete(keyPath(keys...)); err != nil {
			return removed, fmt.Errorf("%s: removing the emptied %s: %w", p.Path, keyPath(keys...), err)
		}
	}
	return removed, nil
}

// RewritePublishedDirectReferences rewrites the published sections of
// pyproject.toml text so each direct reference to a package constraints
// names becomes that registry constraint, keeping every other byte: the
// PyPI build of a workspace member publishes this text instead of a
// manifest pointing at a sibling's checkout. It returns the text unchanged
// when nothing was rewritten.
func RewritePublishedDirectReferences(path string, data []byte, constraints map[string]string) ([]byte, int, error) {
	if len(constraints) == 0 {
		return data, 0, nil
	}
	p, err := ParsePyproject(path, data)
	if err != nil {
		return nil, 0, err
	}
	if !p.HasProject() {
		return data, 0, nil
	}
	normalized := map[string]string{}
	for name, c := range constraints {
		normalized[NormalizePypiName(name)] = c
	}
	n, err := p.RewriteDirectReferences(normalized, PublishedFamilies)
	if err != nil || n == 0 {
		return data, 0, err
	}
	return p.Bytes(), n, nil
}
