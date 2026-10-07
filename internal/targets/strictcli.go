package targets

import (
	"errors"
	"fmt"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/stricttools/rlsbl/internal/gomodule"
)

// The languages a strictcli program is written in.
const (
	StrictcliPython     = "python"
	StrictcliGo         = "go"
	StrictcliTypeScript = "typescript"
)

// SchemaDumpPath is where a strictcli program's committed help document
// (the stdout of `<app> help --json`) sits, relative to the directory of the
// project it describes.
const SchemaDumpPath = ".strictmetadata/.cli-schema/schema.json"

// strictcliGoModules are every module path strictcli's Go implementation
// was published under: it moved from github.com/smm-h/strictcli/go to
// github.com/stricttools/strictcli/go at v0.36.0, and a program pinned on
// either side of the move is a strictcli program.
var strictcliGoModules = []string{"github.com/smm-h/strictcli", "github.com/stricttools/strictcli"}

// StrictcliProgram is a project's strictcli entry point: for python the
// first [project.scripts] name, for go the main package directory as `go
// run` takes it ("." or "./cmd/x/"), for typescript the bin script path.
type StrictcliProgram struct {
	EntryPoint string
	Language   string
}

// IsStrictcliModulePath reports whether a Go module or import path is
// strictcli's or below it, under either module path it was published at,
// and not a module whose name merely starts the same
// (github.com/stricttools/strictcli-extras).
func IsStrictcliModulePath(p string) bool {
	for _, m := range strictcliGoModules {
		if gomodule.ImportUnderModule(p, m) {
			return true
		}
	}
	return false
}

// DetectStrictcli finds whether the project in dir is a strictcli program,
// asking pyproject.toml, then go.mod, then package.json, and returns its
// entry point. found is false when none of them depends on strictcli. A
// project that depends on strictcli but whose entry point cannot be told is
// an error, never "not a strictcli program".
func DetectStrictcli(r Runner, dir string) (StrictcliProgram, bool, error) {
	for _, detect := range []func() (StrictcliProgram, bool, error){
		func() (StrictcliProgram, bool, error) { return detectPythonStrictcli(dir) },
		func() (StrictcliProgram, bool, error) { return detectGoStrictcli(r, dir) },
		func() (StrictcliProgram, bool, error) { return detectTypeScriptStrictcli(dir) },
	} {
		program, found, err := detect()
		if err != nil || found {
			return program, found, err
		}
	}
	return StrictcliProgram{}, false, nil
}

// detectPythonStrictcli: [project].dependencies naming strictcli, and the
// first [project.scripts] entry; a project declaring no script is a library
// and no program.
func detectPythonStrictcli(dir string) (StrictcliProgram, bool, error) {
	p, found, err := readPyproject(dir)
	if err != nil || !found {
		return StrictcliProgram{}, false, err
	}
	project, ok := p.table("project")
	if !ok {
		return StrictcliProgram{}, false, nil
	}
	deps, _ := project["dependencies"].([]any)
	uses := false
	for _, d := range deps {
		if s, ok := d.(string); ok && requirementNames(s, "strictcli") {
			uses = true
		}
	}
	if !uses {
		return StrictcliProgram{}, false, nil
	}
	scripts := p.keysOf("project", "scripts")
	if len(scripts) == 0 {
		// A library built on strictcli declares no program.
		return StrictcliProgram{}, false, nil
	}
	return StrictcliProgram{EntryPoint: scripts[0], Language: StrictcliPython}, true, nil
}

// requirementNames reports whether a PEP 508 requirement names the
// distribution name: the name itself, followed by nothing or by a version
// specifier, extras, a marker, or a URL.
func requirementNames(requirement, name string) bool {
	rest, ok := strings.CutPrefix(strings.TrimSpace(requirement), name)
	if !ok {
		return false
	}
	rest = strings.TrimSpace(rest)
	return rest == "" || strings.ContainsAny(rest[:1], "<>=!~[;@(")
}

// GoStrictcliRequirements are the strictcli module paths go.mod's require
// directives name, in order. The module directive never counts: the
// strictcli library's own go.mod declares a strictcli module path.
func GoStrictcliRequirements(dir string) ([]string, error) {
	f, found, err := gomodule.Read(dir)
	if err != nil || !found {
		return nil, err
	}
	requires, _ := gomodule.Directives(f)
	var out []string
	for _, r := range requires {
		if IsStrictcliModulePath(r.Path) {
			out = append(out, r.Path)
		}
	}
	return out, nil
}

// goEntryPoint spells a main package directory as `go run` takes it.
func goEntryPoint(dir string) string {
	if dir == "." {
		return "."
	}
	return dir + "/"
}

// detectGoStrictcli: go.mod requiring strictcli; the one main package, or
// with several, the one whose own files import strictcli.
func detectGoStrictcli(r Runner, dir string) (StrictcliProgram, bool, error) {
	requires, err := GoStrictcliRequirements(dir)
	if err != nil || len(requires) == 0 {
		return StrictcliProgram{}, false, err
	}
	mains, err := gomodule.MainPackages(r, dir)
	if err != nil {
		return StrictcliProgram{}, false, err
	}
	if len(mains) == 1 {
		return StrictcliProgram{EntryPoint: goEntryPoint(mains[0].Dir), Language: StrictcliGo}, true, nil
	}
	var importing []gomodule.Package
	for _, p := range mains {
		imports, err := packageImportsStrictcli(filepath.Join(dir, filepath.FromSlash(p.Dir)))
		if err != nil {
			return StrictcliProgram{}, false, err
		}
		if imports {
			importing = append(importing, p)
		}
	}
	if len(importing) == 1 {
		return StrictcliProgram{EntryPoint: goEntryPoint(importing[0].Dir), Language: StrictcliGo}, true, nil
	}
	detected := "(none)"
	if len(mains) > 0 {
		quoted := make([]string, len(mains))
		for i, p := range mains {
			quoted[i] = strconv.Quote(p.Dir)
		}
		detected = strings.Join(quoted, ", ")
	}
	return StrictcliProgram{}, false, fmt.Errorf("go.mod in %s requires strictcli, but its entry point cannot be told: main packages found: %s; main packages importing strictcli directly: %d. Lay the project out so one main package imports strictcli", dir, detected, len(importing))
}

// packageImportsStrictcli reports whether a .go file directly in dir
// imports a strictcli package; only the import declarations are parsed.
func packageImportsStrictcli(dir string) (bool, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return false, err
	}
	fset := token.NewFileSet()
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".go") {
			continue
		}
		path := filepath.Join(dir, e.Name())
		f, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
		if err != nil {
			return false, fmt.Errorf("reading the imports of %s: %w", path, err)
		}
		for _, spec := range f.Imports {
			p, err := strconv.Unquote(spec.Path.Value)
			if err == nil && IsStrictcliModulePath(p) {
				return true, nil
			}
		}
	}
	return false, nil
}

// detectTypeScriptStrictcli: package.json depending on the strictcli npm
// package and declaring one bin, whose script path is the entry point (the
// dump runs it with node, needing no install or link).
func detectTypeScriptStrictcli(dir string) (StrictcliProgram, bool, error) {
	p, found, err := readPackageJSON(dir)
	if err != nil || !found {
		return StrictcliProgram{}, false, err
	}
	uses := false
	for _, section := range []string{"dependencies", "devDependencies", "peerDependencies"} {
		var deps map[string]any
		if _, err := p.field(section, &deps); err != nil {
			return StrictcliProgram{}, false, err
		}
		if _, ok := deps["strictcli"]; ok {
			uses = true
		}
	}
	if !uses {
		return StrictcliProgram{}, false, nil
	}
	if script, found, err := p.stringField("bin"); err == nil && found {
		return StrictcliProgram{EntryPoint: script, Language: StrictcliTypeScript}, true, nil
	}
	m, _, err := p.member("bin")
	if err != nil {
		return StrictcliProgram{}, false, err
	}
	declared := "(none)"
	if m.valueEnd > m.valueStart {
		bins, err := binEntries(p)
		if err != nil {
			return StrictcliProgram{}, false, err
		}
		if len(bins) == 1 {
			return StrictcliProgram{EntryPoint: bins[0].script, Language: StrictcliTypeScript}, true, nil
		}
		if len(bins) > 0 {
			names := make([]string, len(bins))
			for i, b := range bins {
				names[i] = strconv.Quote(b.command)
			}
			declared = strings.Join(names, ", ")
		}
	}
	return StrictcliProgram{}, false, fmt.Errorf("package.json in %s depends on strictcli, but its entry point cannot be told: bin entries declared: %s. Declare one bin so the schema dump knows what to run", dir, declared)
}

// binEntry is one command of package.json's "bin" object.
type binEntry struct{ command, script string }

// binEntries are the commands of package.json's "bin" object, in document
// order.
func binEntries(p packageJSON) ([]binEntry, error) {
	m, found, err := p.member("bin")
	if err != nil || !found {
		return nil, err
	}
	members, err := topLevelMembers(p.raw[m.valueStart:m.valueEnd])
	if err != nil {
		return nil, fmt.Errorf("%s: \"bin\" is neither a string nor an object of command names to scripts", p.path)
	}
	sub := packageJSON{path: p.path, raw: p.raw[m.valueStart:m.valueEnd], members: members}
	var out []binEntry
	for _, member := range members {
		script, _, err := sub.stringField(member.key)
		if err != nil {
			return nil, errors.New(p.path + ": every \"bin\" entry must name a script path")
		}
		out = append(out, binEntry{command: member.key, script: script})
	}
	return out, nil
}
