package options

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// Dir is the options directory, relative to a repository's root.
const Dir = strictspec.OptionsDir

// RefusedError is the refusal of a repository's options: every line names a
// document, an entry, and what is wrong with it.
type RefusedError struct {
	// Dir is the options directory, absolute.
	Dir   string
	Lines []string
}

func (e *RefusedError) Error() string {
	return fmt.Sprintf("rlsbl refuses the options in %s, so it runs nothing with them. Fix each entry named below (`rlsbl options set` rewrites an rlsbl entry; a hand edit is validated the same way):\n  %s", e.Dir, strings.Join(e.Lines, "\n  "))
}

// Options are the accepted entries of rlsbl's namespace in one repository.
type Options struct {
	reg     *Registry
	entries []strictspec.OptionsEntry
}

// Defaults are the options of a place with no entries: every option at its
// default.
func Defaults(reg *Registry) *Options { return &Options{reg: reg} }

// Registry is the registry the options were judged against.
func (o *Options) Registry() *Registry { return o.reg }

// Load reads the entries of the repository rooted at root (absolute) and
// refuses them, naming every problem, when strictspec or rlsbl's path-scope
// rule refuses any. d are the repository's declarations, which a path scope
// names a member of; nil when the repository has none, where no path scope
// can name anything.
func Load(reg *Registry, root string, d *declarations.Releasables) (*Options, error) {
	loaded, err := strictspec.LoadOptionsEntries(root)
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", filepath.Join(root, filepath.FromSlash(Dir)), err)
	}
	if lines := refusals(reg, loaded.Invalid, loaded.Entries, d); len(lines) > 0 {
		return nil, &RefusedError{Dir: filepath.Join(root, filepath.FromSlash(Dir)), Lines: lines}
	}
	var own []strictspec.OptionsEntry
	for _, e := range loaded.Entries {
		if strings.HasPrefix(e.ID, Prefix) {
			own = append(own, e)
		}
	}
	return &Options{reg: reg, entries: own}, nil
}

// refusals are every refusal of the documents and of rlsbl's entries, each a
// line naming the document it concerns.
func refusals(reg *Registry, invalid []strictspec.FileDiagnostics, entries []strictspec.OptionsEntry, d *declarations.Releasables) []string {
	var lines []string
	for _, inv := range invalid {
		for _, diag := range inv.Diagnostics {
			lines = append(lines, fmt.Sprintf("%s/%s: %s: %s", Dir, inv.File, diag.Code, diag.Message))
		}
	}
	_, diags := strictspec.ValidateOptionsNamespace(Tool, reg.checkedRegistry(), entries)
	for _, diag := range diags {
		lines = append(lines, fmt.Sprintf("%s: %s", diag.Code, diag.Message))
	}
	for _, e := range entries {
		if !strings.HasPrefix(e.ID, Prefix) || !e.HasScope {
			continue
		}
		decl, ok := reg.Declaration(strings.TrimPrefix(e.ID, Prefix))
		if !ok || decl.Scope != PathScope {
			continue
		}
		if problem := pathScopeProblem(e.Scope, d); problem != "" {
			lines = append(lines, fmt.Sprintf("%s/%s: entry[%d]: %s is scoped to %q, %s", Dir, e.File, e.Index, e.ID, e.Scope, problem))
		}
	}
	return lines
}

// pathScopeProblem says why scope names no member, and is empty when it
// names one. Only a workspace has members for a scope to choose between: a
// standalone repository's one project is covered by an entry without a
// scope, and two spellings of one entry would be one too many.
func pathScopeProblem(scope string, d *declarations.Releasables) string {
	switch {
	case d == nil:
		return fmt.Sprintf("but this repository declares no members (it has no %s), so there is no member for a path scope to name. Remove the entry's scope.", declarations.ReleasablesFile)
	case !d.IsWorkspace():
		return "but this repository is standalone: its one project is covered by an entry without a scope. Remove the entry's scope."
	}
	var paths []string
	for _, m := range d.Members {
		if m.Path == scope {
			return ""
		}
		paths = append(paths, m.Path)
	}
	sort.Strings(paths)
	return fmt.Sprintf("which names no member of this workspace. A path scope is a member's path as %s declares it: %s.", declarations.ReleasablesFile, strings.Join(paths, ", "))
}

// Value is an option's value for one member and where it came from.
type Value struct {
	Value string
	// Source names the entry that set the value, or the default.
	Source string
	// Entry is the entry that set the value; nil at the default.
	Entry *strictspec.OptionsEntry
}

func entrySource(e strictspec.OptionsEntry) string {
	where := fmt.Sprintf("%s in %s/%s", e.ID, Dir, e.File)
	if e.HasScope {
		where += fmt.Sprintf(" (scope %q)", e.Scope)
	}
	return where
}

// Value is the value option name (without the prefix) has for the member
// at memberPath: an entry scoped to that path wins over an entry without a
// scope, and without either the option runs at its default. An option the
// registry does not declare is an error.
func (o *Options) Value(name, memberPath string) (Value, error) {
	decl, ok := o.reg.Declaration(name)
	if !ok {
		return Value{}, fmt.Errorf("%s%s is not an option rlsbl declares; `rlsbl options registry` lists every option", Prefix, name)
	}
	var chosen *strictspec.OptionsEntry
	for i := range o.entries {
		e := &o.entries[i]
		if e.ID != Prefix+name {
			continue
		}
		if !e.HasScope {
			if chosen == nil {
				chosen = e
			}
			continue
		}
		if decl.Scope == PathScope && e.Scope == memberPath {
			chosen = e
			break
		}
	}
	if chosen == nil {
		return Value{Value: decl.Default, Source: Prefix + name + " default"}, nil
	}
	entry := *chosen
	return Value{Value: entry.Current, Source: entrySource(entry), Entry: &entry}, nil
}

// IsOff reports whether option name is off for the member at memberPath.
func (o *Options) IsOff(name, memberPath string) (bool, error) {
	v, err := o.Value(name, memberPath)
	if err != nil {
		return false, err
	}
	return v.Value == Off, nil
}

// CheckValue is the value option name gives its check for the member at
// memberPath, and false to run the check at its registered severity: a check
// that is not an rlsbl option (a declared external check), or an option at a
// default that is not off. An adoption option with no entry is off, and its
// check is shown as off with that source.
func (o *Options) CheckValue(name, memberPath string) (strictcli.CheckValue, bool) {
	if _, ok := o.reg.Declaration(name); !ok {
		return strictcli.CheckValue{}, false
	}
	v, err := o.Value(name, memberPath)
	if err != nil || (v.Entry == nil && v.Value != Off) {
		return strictcli.CheckValue{}, false
	}
	return strictcli.CheckValue{Value: v.Value, Source: v.Source}, true
}

// Resolver is the check value resolver for the member at memberPath, the
// function strictcli's SetCheckValueResolver takes.
func (o *Options) Resolver(memberPath string) func(string) (strictcli.CheckValue, bool) {
	return func(name string) (strictcli.CheckValue, bool) { return o.CheckValue(name, memberPath) }
}
