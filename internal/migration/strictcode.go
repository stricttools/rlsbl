package migration

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"sort"

	tomledit "github.com/stricttools/go-toml-edit"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/targets"
)

// StrictcodeFile is strictcode's declarations file, at the repository root.
const StrictcodeFile = "strictcode.toml"

// pythonTool is one [python_tools.<rule>] declaration.
type pythonTool struct {
	cwd   string
	paths []string
}

// ruffMembers are the paths of the members ruff-lint ran on: every member
// with a pypi target, declared or detected.
func (b *builder) ruffMembers() []string {
	var out []string
	for _, m := range b.d.Members {
		ts := m.Targets
		if ts == nil {
			detected, err := targets.Detect(filepath.Join(b.root, filepath.FromSlash(m.Path)))
			if err != nil {
				b.p.add("detecting the targets of %s: %v", m.Name, err)
				continue
			}
			ts = detected
		}
		for _, t := range ts {
			if t.Name == declarations.TargetPyPI {
				out = append(out, m.Path)
				break
			}
		}
	}
	return out
}

// ruffCovered reports whether ruff-lint's coverage moves to strictcode's
// lint rule: unless the repository switched rlsbl:ruff-lint off.
func (b *builder) ruffCovered() bool {
	return b.ruffLint == nil || b.ruffLint.current != "off"
}

// pythonTools composes each Python tool rule's one declaration: from the
// checks.<rule> blocks, and for lint the members ruff-lint covered. One
// working directory is kept as it is; several become the root, every path
// joined to its own.
func (b *builder) pythonTools() map[string]pythonTool {
	decls := map[string][]toolDeclaration{}
	for rule, list := range b.tools {
		decls[rule] = append(decls[rule], list...)
	}
	if b.ruffCovered() {
		for _, member := range b.ruffMembers() {
			covered := false
			for _, d := range decls["lint"] {
				if d.member == member {
					covered = true
				}
			}
			if !covered {
				decls["lint"] = append(decls["lint"], toolDeclaration{member: member, cwd: member, paths: []string{declarations.RootPath}, file: "the ruff-lint check"})
			}
		}
	}
	out := map[string]pythonTool{}
	for rule, list := range decls {
		cwds := map[string]bool{}
		for _, d := range list {
			cwds[d.cwd] = true
		}
		paths := map[string]bool{}
		cwd := declarations.RootPath
		if len(cwds) == 1 {
			cwd = list[0].cwd
			for _, d := range list {
				for _, p := range d.paths {
					paths[p] = true
				}
			}
		} else {
			for _, d := range list {
				for _, p := range d.paths {
					paths[joinRel(d.cwd, p)] = true
				}
			}
			b.note("[python_tools.%s] runs from the repository root over every declared path: the members declared it from %d different directories, and strictcode.toml holds one declaration per rule", rule, len(cwds))
		}
		out[rule] = pythonTool{cwd: cwd, paths: sortedKeys(paths)}
	}
	return out
}

// convertStrictcode writes what moved to strictcode into strictcode.toml,
// beside what the file already holds: the Python tool declarations, the
// library rules' lists, the dead-modules suppressions, and the strictspec
// certificate. A key the file already holds is refused, never overwritten.
func (b *builder) convertStrictcode() {
	tools := b.pythonTools()
	lists := []struct {
		rule, field string
		values      map[string]sourcedList
	}{
		{"library-forbidden-imports", "forbidden", b.lint.forbidden},
		{"library-forbidden-imports", "allow", b.lint.allow},
		{"library-stdout", "allow", b.lint.stdoutAllow},
	}
	empty := len(tools) == 0 && len(b.deadModules) == 0 && b.certificate == nil
	for _, l := range lists {
		if len(l.values) > 0 {
			empty = false
		}
	}
	if empty {
		return
	}
	data, err := os.ReadFile(filepath.Join(b.root, StrictcodeFile))
	existed := err == nil
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		b.p.add("reading %s: %v", StrictcodeFile, err)
		return
	}
	doc, err := tomledit.Parse(data)
	if err != nil {
		b.p.add("%s is not valid TOML: %v", StrictcodeFile, err)
		return
	}
	var sources []string
	refuseHeld := func(key string) bool {
		if doc.Has(key) {
			b.p.add("%s already declares %s, which the migration would write from the old layout; merge the two by hand (delete the key from %s, and a dry run shows what replaces it), then migrate", StrictcodeFile, key, StrictcodeFile)
			return true
		}
		return false
	}
	apply := func(err error) {
		if err != nil {
			b.p.add("composing %s: %v", StrictcodeFile, err)
		}
	}
	for _, rule := range sortedKeys(tools) {
		t := tools[rule]
		key := "python_tools." + rule
		if refuseHeld(key) {
			continue
		}
		apply(doc.NewTable(key))
		apply(doc.Set(key+".paths", anyList(t.paths)))
		if t.cwd != declarations.RootPath {
			apply(doc.Set(key+".cwd", t.cwd))
		}
	}
	for _, l := range lists {
		for _, language := range sortedKeys(l.values) {
			table := "rules." + l.rule + "." + l.field
			key := table + "." + language
			if refuseHeld(key) {
				continue
			}
			if !doc.Has(table) {
				apply(doc.NewTable(table))
			}
			apply(doc.Set(key, anyList(l.values[language].values)))
			sources = append(sources, l.values[language].file)
		}
	}
	for _, s := range b.deadModules {
		apply(doc.NewArrayTable("rules.dead-modules.suppressions"))
		apply(doc.Set("rules.dead-modules.suppressions[-1].path", s.path))
		apply(doc.Set("rules.dead-modules.suppressions[-1].reason", s.reason))
		sources = append(sources, s.file)
	}
	if c := b.certificate; c != nil && !refuseHeld("strictspec_certificate") {
		apply(doc.NewTable("strictspec_certificate"))
		apply(doc.Set("strictspec_certificate.certificate", c.certificate))
		if c.adjudication != "" {
			apply(doc.Set("strictspec_certificate.adjudication", c.adjudication))
		}
		sources = append(sources, c.file)
	}
	for _, list := range b.tools {
		for _, d := range list {
			sources = append(sources, d.file)
		}
	}
	change := "created with the declarations that moved to strictcode"
	if existed {
		change = "the declarations that moved to strictcode, added beside the file's content"
	}
	b.addWrite(write{path: StrictcodeFile, sources: dedupeStrings(sources), change: change, data: doc.Bytes()})
}

func anyList(values []string) []any {
	out := make([]any, len(values))
	for i, v := range values {
		out[i] = v
	}
	return out
}

func dedupeStrings(values []string) []string {
	sorted := append([]string(nil), values...)
	sort.Strings(sorted)
	return dedupe(sorted)
}
