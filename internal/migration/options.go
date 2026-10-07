package migration

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/options"
)

// optionsDir is the options directory, repository-relative.
const optionsDir = declarations.MetadataDir + "/options"

// renamedOptions are the rlsbl options whose checks were renamed.
var renamedOptions = map[string]string{
	"changelog-format-version-gate": "changelog-format-version",
	"config-schema":                 "declarations-valid",
}

// strictcodeOption is where an rlsbl option of a check that moved to
// strictcode goes: strictcode's rule and the subject its entries are filed
// under, and whether the option takes a path scope.
type strictcodeOption struct {
	rule    string
	subject string
	scoped  bool
}

// movedOptions are the rlsbl options of the checks that moved to strictcode.
var movedOptions = map[string]strictcodeOption{
	"lint":                        {"lint", "code", true},
	"format":                      {"format", "code", true},
	"type-check":                  {"type-check", "code", true},
	"lint-scope-guard":            {"lint-scope-guard", "code", false},
	"format-scope-guard":          {"format-scope-guard", "code", false},
	"type-check-scope-guard":      {"type-check-scope-guard", "code", false},
	"deps-unused":                 {"deps-unused", "dependencies", false},
	"deps-undeclared":             {"deps-undeclared", "dependencies", false},
	"deps-runtime-test-only":      {"deps-runtime-test-only", "dependencies", false},
	"deps-dev-in-lib":             {"deps-dev-in-production", "dependencies", false},
	"deps-stale":                  {"deps-stale", "dependencies", false},
	"dead-modules":                {"dead-modules", "code", false},
	"dead-modules-stale":          {"stale-suppression", "code", false},
	"circular-deps":               {"import-cycles", "code", false},
	"dead-workspace-packages":     {"dead-workspace-packages", "code", false},
	"strictspec-certificate-gate": {"strictspec-certificate", "release", false},
}

// ruffLintEntry is the repository's rlsbl:ruff-lint entry.
type ruffLintEntry struct {
	current, ideal, reason string
}

// optionsEntry is one [[entry]] of a subject document.
type optionsEntry struct {
	id, current, ideal, reason, scope string
}

// optionsDocument is one subject document of the options directory.
type optionsDocument struct {
	rel     string
	entries []optionsEntry
	changed bool
	existed bool
}

// convertOptions rewrites rlsbl's entries in .strictmetadata/options/: a
// renamed option's entry takes the new name, an option that moved to
// strictcode becomes strictcode's (filed under its subject), rlsbl:ruff-lint
// decides whether the members it covered get strictcode:lint entries, and an
// entry of a removed option with no new home is refused. Every other entry
// is kept as it is.
func (b *builder) convertOptions() {
	registry, err := options.Shipped()
	if err != nil {
		b.p.add("reading rlsbl's options registry: %v", err)
		return
	}
	docs, err := b.readOptions()
	if err != nil {
		b.p.add("%v", err)
		return
	}
	bySubject := map[string]*optionsDocument{}
	for _, d := range docs {
		bySubject[strings.TrimSuffix(filepath.Base(d.rel), ".toml")] = d
	}
	subjectDoc := func(subject string) *optionsDocument {
		if d, ok := bySubject[subject]; ok {
			return d
		}
		d := &optionsDocument{rel: optionsDir + "/" + subject + ".toml"}
		bySubject[subject] = d
		docs = append(docs, d)
		return d
	}
	for _, d := range append([]*optionsDocument(nil), docs...) {
		var kept []optionsEntry
		for _, e := range d.entries {
			name, ours := strings.CutPrefix(e.id, options.Prefix)
			if !ours {
				kept = append(kept, e)
				continue
			}
			if _, ok := registry.Declaration(name); ok {
				kept = append(kept, e)
				continue
			}
			d.changed = true
			if renamed, ok := renamedOptions[name]; ok {
				decl, _ := registry.Declaration(renamed)
				moved := e
				moved.id = options.Prefix + renamed
				target := subjectDoc(decl.Subject)
				if target == d {
					kept = append(kept, moved)
				} else {
					target.entries = append(target.entries, moved)
					target.changed = true
				}
				b.note("%s: %s is now %s", d.rel, e.id, moved.id)
				continue
			}
			if dest, ok := movedOptions[name]; ok {
				if e.scope != "" && !dest.scoped {
					b.p.add("%s: the %s entry is scoped to %s, and strictcode:%s takes no scope (it applies to the whole repository). Hand edit: delete the scope line, or the entry, then migrate", d.rel, e.id, e.scope, dest.rule)
					continue
				}
				moved := e
				moved.id = "strictcode:" + dest.rule
				target := subjectDoc(dest.subject)
				if target == d {
					kept = append(kept, moved)
				} else {
					target.entries = append(target.entries, moved)
					target.changed = true
				}
				b.note("%s: %s is now %s, in %s", d.rel, e.id, moved.id, target.rel)
				continue
			}
			switch name {
			case "ruff-lint":
				b.ruffLint = &ruffLintEntry{current: e.current, ideal: e.ideal, reason: e.reason}
				b.note("%s: %s is removed; the lint rule of strictcode takes over ruff-lint", d.rel, e.id)
			case "library-lint":
				b.p.add("%s: rlsbl:library-lint split into three strictcode rules (library-forbidden-imports, library-stdout, library-entry-point), so its entry has no one new home. Hand edit: delete the entry, then migrate, and set the strictcode:library-* entries that carry its intent", d.rel)
			default:
				b.p.add("%s: %s names an option rlsbl removed with its check, and it has no new home. Hand edit: delete the entry, then migrate", d.rel, e.id)
			}
		}
		d.entries = kept
	}
	if b.ruffCovered() {
		b.addRuffLintEntries(subjectDoc("code"), bySubject)
	}
	if tag, ok := b.values.agreed("tag", b.p); ok && tag == "false" {
		b.addTaggingEntry(registry, subjectDoc, bySubject)
	}
	for _, d := range docs {
		if !d.changed {
			continue
		}
		b.addWrite(write{path: d.rel, sources: optionSources(d), change: "rlsbl's option entries renamed, moved to strictcode, or removed", data: renderOptions(d)})
	}
}

func optionSources(d *optionsDocument) []string {
	if d.existed {
		return []string{d.rel}
	}
	return nil
}

// addRuffLintEntries gives every member ruff-lint covered a strictcode:lint
// entry, scoped to the member in a workspace, unless one is there already.
func (b *builder) addRuffLintEntries(code *optionsDocument, all map[string]*optionsDocument) {
	current, ideal := "error", "error"
	reason := "ruff-lint checked this member before the check moved into strictcode's lint rule"
	if b.ruffLint != nil {
		current, ideal = b.ruffLint.current, b.ruffLint.ideal
		if strings.TrimSpace(b.ruffLint.reason) != "" {
			reason = b.ruffLint.reason
		}
	}
	held := map[string]bool{}
	for _, d := range all {
		for _, e := range d.entries {
			if e.id == "strictcode:lint" {
				held[e.scope] = true
			}
		}
	}
	for _, member := range b.ruffMembers() {
		scope := member
		if b.d.Layout == declarations.LayoutStandalone {
			scope = ""
		}
		if held[scope] || held[""] {
			continue
		}
		held[scope] = true
		code.entries = append(code.entries, optionsEntry{id: "strictcode:lint", current: current, ideal: ideal, reason: reason, scope: scope})
		code.changed = true
	}
}

// taggingOption is the option that replaced the config's tag key.
const taggingOption = "ecosystem-tagging"

// addTaggingEntry writes the rlsbl:ecosystem-tagging entry that switches
// ecosystem tagging off, as the configs' tag = false did, unless the
// repository has an entry for it already.
func (b *builder) addTaggingEntry(registry *options.Registry, subjectDoc func(string) *optionsDocument, all map[string]*optionsDocument) {
	id := options.Prefix + taggingOption
	for _, d := range all {
		for _, e := range d.entries {
			if e.id == id {
				return
			}
		}
	}
	decl, ok := registry.Declaration(taggingOption)
	if !ok {
		b.p.add("rlsbl's options registry declares no %s, which the configs' tag = false becomes", id)
		return
	}
	d := subjectDoc(decl.Subject)
	d.entries = append(d.entries, optionsEntry{id: id, current: "off", ideal: "off", reason: "the old config switched ecosystem tagging off (tag = false)"})
	d.changed = true
}

// readOptions reads every subject document of the options directory.
func (b *builder) readOptions() ([]*optionsDocument, error) {
	entries, err := os.ReadDir(filepath.Join(b.root, filepath.FromSlash(optionsDir)))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", optionsDir, err)
	}
	var out []*optionsDocument
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".toml") || name == "manifest.toml" {
			continue
		}
		rel := optionsDir + "/" + name
		data, err := os.ReadFile(filepath.Join(b.root, filepath.FromSlash(rel)))
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", rel, err)
		}
		doc, err := tomledit.Unmarshal[map[string]any](data)
		if err != nil {
			return nil, fmt.Errorf("%s is not valid TOML: %w", rel, err)
		}
		o, _ := newObject(rel, *doc, b.p)
		d := &optionsDocument{rel: rel, existed: true}
		for _, k := range o.keys() {
			if k != "format_version" && k != "entry" {
				return nil, fmt.Errorf("%s: %s is not a key of an options document", rel, k)
			}
		}
		items, _ := o.list("entry")
		for i, item := range items {
			eo, ok := o.child("entry", i, item)
			if !ok {
				continue
			}
			var e optionsEntry
			e.id, _ = eo.str("id")
			e.current, _ = eo.str("current")
			e.ideal, _ = eo.str("ideal")
			e.reason, _ = eo.str("reason")
			e.scope, _ = eo.str("scope")
			for _, k := range eo.keys() {
				switch k {
				case "id", "current", "ideal", "reason", "scope":
				default:
					return nil, fmt.Errorf("%s: %s.%s is not a key of an options entry", rel, eo.where, k)
				}
			}
			d.entries = append(d.entries, e)
		}
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].rel < out[j].rel })
	return out, nil
}

// renderOptions writes a subject document whole: format_version and each
// entry, in order.
func renderOptions(d *optionsDocument) []byte {
	var b strings.Builder
	b.WriteString("format_version = 1\n")
	for _, e := range d.entries {
		b.WriteString("\n[[entry]]\n")
		fmt.Fprintf(&b, "id = %s\n", tomlString(e.id))
		fmt.Fprintf(&b, "current = %s\n", tomlString(e.current))
		fmt.Fprintf(&b, "ideal = %s\n", tomlString(e.ideal))
		fmt.Fprintf(&b, "reason = %s\n", tomlString(e.reason))
		if e.scope != "" {
			fmt.Fprintf(&b, "scope = %s\n", tomlString(e.scope))
		}
	}
	return []byte(b.String())
}
