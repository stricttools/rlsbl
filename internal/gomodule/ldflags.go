package gomodule

import (
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// `go build -ldflags "-X importpath.Symbol=value"` overwrites a package-level
// string variable at link time. When the named symbol does not exist the
// link succeeds silently and the flag does nothing, so a build configuration
// saying -X main.Version while the source declares var version ships binaries
// that report their fallback version forever. Every -X occurrence in a
// module's tracked build configuration (goreleaser's document, Makefiles,
// shell scripts, CI workflow YAML) is resolved to a directory and the symbol
// looked up in the Go source there:
//
//   - a problem: the symbol is not declared, or is declared as something the
//     linker cannot set (a const, a function, a type, a var that is not a
//     string, a var initialized to a non-constant expression);
//   - a warning: the symbol can be set, but nothing in the module reads it;
//   - a note: the occurrence cannot be resolved (a build-time template in the
//     import path, a package outside the module, a bare main with several
//     main packages to choose from), which is reported rather than guessed.
//
// The relationship is checked, never a naming convention: main.version with
// var version and main.Version with var Version are both correct.

// excludedBuildPrefixes are never build configuration: the old layout's
// scaffold bases and workspace directory, the new layout's metadata, and
// goreleaser's own output.
var excludedBuildPrefixes = []string{".rlsbl/bases/", ".rlsbl-monorepo/", ".strictmetadata/", "dist/"}

// goreleaserNames are goreleaser's document in both spellings, with or
// without the dot.
var goreleaserNames = map[string]bool{
	".goreleaser.yml": true, ".goreleaser.yaml": true, "goreleaser.yml": true, "goreleaser.yaml": true,
}

var (
	goBuild      = regexp.MustCompile(`\bgo\s+(?:build|install)\b([^\n;&|]*)`)
	packageArg   = regexp.MustCompile(`^\.(/[\w.@-]+)*$`)
	goIdentifier = regexp.MustCompile(`^[A-Za-z_]\w*$`)
)

// Occurrence is one -X flag found in a build file.
type Occurrence struct {
	// File is relative to the module directory.
	File string
	// Line is 1-based.
	Line int
	// Target is the raw importpath.Symbol text.
	Target string
	// ImportPath and Symbol are empty when the target carries a build-time
	// template or does not end in an identifier.
	ImportPath string
	Symbol     string
}

// declaration is what a package declares under one name.
type declaration struct {
	file        string
	line        int
	description string
	injectable  bool
	// name is the declaring identifier, which is not a read of the symbol.
	name *ast.Ident
}

// LdflagsVerdict is every -X target of some modules compared against their
// source.
type LdflagsVerdict struct {
	// Problems are targets naming nothing the linker can set.
	Problems []string
	// Warnings are targets the linker can set but nothing reads.
	Warnings []string
	// Notes are targets left unverified, each saying why.
	Notes []string
	// SkipReason is why nothing was compared, or empty.
	SkipReason string
	// Verified counts the targets resolved to a symbol the linker can set.
	Verified int
}

// OK reports whether no target names a symbol the linker cannot set.
func (v LdflagsVerdict) OK() bool { return len(v.Problems) == 0 }

// LdflagsModule is one module to check, with the file listings git gives.
type LdflagsModule struct {
	// Dir is the module directory, absolute, holding a go.mod.
	Dir string
	// Tracked are the files git tracks under Dir, relative to it and
	// "/"-separated: only tracked files are the project's build
	// configuration.
	Tracked []string
	// Listed are the tracked files plus the untracked files no ignore rule
	// excludes, relative to Dir: the Go source a symbol may be declared or
	// read in.
	Listed []string
	// NestedMembers are the directories of workspace members nested inside
	// the module, relative to Dir; they own their files outright.
	NestedMembers []string
}

// IsBuildFile reports whether the module-relative path is a file that can
// carry linker flags.
func IsBuildFile(rel string) bool {
	rel = filepath.ToSlash(rel)
	for _, prefix := range excludedBuildPrefixes {
		if strings.HasPrefix(rel, prefix) || strings.Contains("/"+rel, "/"+prefix) {
			return false
		}
	}
	base := strings.ToLower(path.Base(rel))
	switch {
	case goreleaserNames[base]:
		return true
	case base == "makefile" || base == "gnumakefile" || strings.HasSuffix(base, ".mk"):
		return true
	case strings.HasSuffix(base, ".sh") || strings.HasSuffix(base, ".bash"):
		return true
	}
	parts := strings.Split(rel, "/")
	inWorkflows := false
	for i := 0; i+1 < len(parts); i++ {
		if parts[i] == ".github" {
			for _, p := range parts[i+1 : len(parts)-1] {
				if p == "workflows" {
					inWorkflows = true
				}
			}
		}
	}
	return inWorkflows && (strings.HasSuffix(base, ".yml") || strings.HasSuffix(base, ".yaml"))
}

// SplitTarget splits importpath.Symbol at the last dot, as the linker does,
// and is false for a target carrying a build-time template or not ending in
// an identifier.
func SplitTarget(target string) (importPath, symbol string, ok bool) {
	if strings.Contains(target, "{{") {
		return "", "", false
	}
	i := strings.LastIndex(target, ".")
	if i <= 0 || !goIdentifier.MatchString(target[i+1:]) {
		return "", "", false
	}
	return target[:i], target[i+1:], true
}

func isWordByte(c byte) bool {
	return c == '_' || (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
}

func isSpaceByte(c byte) bool {
	return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\v' || c == '\f'
}

// scanTarget reads a -X target starting at line[start:]: a run of build-time
// templates ({{ ... }}) and characters other than whitespace, quotes, and
// '='. It returns where the run ends.
func scanTarget(line string, start int) int {
	j := start
	for j < len(line) {
		if strings.HasPrefix(line[j:], "{{") {
			if k := strings.Index(line[j+2:], "}}"); k >= 0 && !strings.ContainsAny(line[j+2:j+2+k], "{}") {
				j += 2 + k + 2
				continue
			}
		}
		c := line[j]
		if isSpaceByte(c) || c == '"' || c == '\'' || c == '=' {
			break
		}
		j++
	}
	return j
}

// FindOccurrences is every -X flag in text: "-X" not preceded by a word
// character or '-', then '=' or whitespace, an optional quote, the target,
// and '='.
func FindOccurrences(text, rel string) []Occurrence {
	var found []Occurrence
	for n, line := range strings.Split(text, "\n") {
		for i := 0; i+1 < len(line); i++ {
			if line[i] != '-' || line[i+1] != 'X' {
				continue
			}
			if i > 0 && (isWordByte(line[i-1]) || line[i-1] == '-') {
				continue
			}
			j := i + 2
			if j >= len(line) || !(line[j] == '=' || isSpaceByte(line[j])) {
				continue
			}
			j++
			for j < len(line) && isSpaceByte(line[j]) {
				j++
			}
			if j < len(line) && (line[j] == '"' || line[j] == '\'') {
				j++
			}
			end := scanTarget(line, j)
			if end == j || end >= len(line) || line[end] != '=' {
				continue
			}
			target := line[j:end]
			importPath, symbol, _ := SplitTarget(target)
			found = append(found, Occurrence{File: rel, Line: n + 1, Target: target, ImportPath: importPath, Symbol: symbol})
			i = end
		}
	}
	return found
}

// normalizeMain reads a goreleaser main (a directory or a .go file) as a
// module-relative directory.
func normalizeMain(value string) string {
	text := strings.TrimSpace(value)
	if text == "" {
		text = "."
	}
	if strings.HasSuffix(text, ".go") {
		text = path.Dir(text)
	}
	return path.Clean(text)
}

// goreleaserBuild is one build goreleaser's document declares.
type goreleaserBuild struct {
	ldflags string
	main    string
}

// goreleaserBuilds are the builds of a goreleaser document, and false when
// the text does not read as one; a bare main is then resolved the way a
// shell script's is.
func goreleaserBuilds(text string) ([]goreleaserBuild, bool) {
	var doc map[string]any
	if err := yaml.Unmarshal([]byte(text), &doc); err != nil {
		return nil, false
	}
	list, ok := doc["builds"].([]any)
	if !ok {
		return nil, false
	}
	var builds []goreleaserBuild
	for _, item := range list {
		b, ok := item.(map[string]any)
		if !ok {
			continue
		}
		var flags string
		switch v := b["ldflags"].(type) {
		case string:
			flags = v
		case []any:
			parts := make([]string, len(v))
			for i, p := range v {
				parts[i] = fmt.Sprint(p)
			}
			flags = strings.Join(parts, " ")
		}
		mainDir, _ := b["main"].(string)
		builds = append(builds, goreleaserBuild{ldflags: flags, main: normalizeMain(mainDir)})
	}
	return builds, len(builds) > 0
}

// explicitBuildTarget is the package a go build or go install line names,
// the occurrence's own line first, then the rest of the file (a Makefile
// holding its flags in a variable).
func explicitBuildTarget(text string, lineIndex int) (string, bool) {
	lines := strings.Split(text, "\n")
	order := []int{lineIndex}
	for i := range lines {
		if i != lineIndex {
			order = append(order, i)
		}
	}
	for _, i := range order {
		if i >= len(lines) {
			continue
		}
		for _, m := range goBuild.FindAllStringSubmatch(lines[i], -1) {
			var packages []string
			for _, token := range strings.Fields(m[1]) {
				token = strings.Trim(token, `"'`)
				if packageArg.MatchString(token) && !strings.HasSuffix(token, "...") {
					packages = append(packages, token)
				}
			}
			if len(packages) > 0 {
				return normalizeMain(packages[len(packages)-1]), true
			}
		}
	}
	return "", false
}

// insideAny reports whether rel lies in one of dirs.
func insideAny(rel string, dirs map[string]bool) bool {
	for d := range dirs {
		if rel == d || strings.HasPrefix(rel, d+"/") {
			return true
		}
	}
	return false
}

// foreignDirs are the module-relative directories that are not the
// module's: nested Go modules (a directory below the root holding its own
// go.mod, which the module zip leaves out too) and nested workspace members.
func foreignDirs(listing []string, nested []string) map[string]bool {
	dirs := map[string]bool{}
	for _, rel := range listing {
		if path.Base(rel) == FileName && path.Dir(rel) != "." {
			dirs[path.Dir(rel)] = true
		}
	}
	for _, n := range nested {
		dirs[path.Clean(filepath.ToSlash(n))] = true
	}
	return dirs
}

// skippedSourceDirs hold other people's code, fixtures, and tool state at any
// depth; scratchDirs hold throwaway probes at the module root.
var (
	skippedSourceDirs = map[string]bool{".git": true, "vendor": true, "testdata": true, "node_modules": true}
	scratchDirs       = map[string]bool{"experiments": true, "screenshots": true}
)

// goSource is one module's parsed Go source.
type goSource struct {
	dir     string
	fset    *token.FileSet
	files   []string
	parsed  map[string]*ast.File
	symbols map[string]map[string]declaration
}

func newGoSource(dir string, listed []string, nested []string) *goSource {
	foreign := foreignDirs(listed, nested)
	var files []string
	for _, rel := range listed {
		rel = filepath.ToSlash(rel)
		if !strings.HasSuffix(rel, ".go") || strings.HasSuffix(rel, "_test.go") || insideAny(rel, foreign) {
			continue
		}
		parts := strings.Split(rel, "/")
		skip := len(parts) > 1 && scratchDirs[parts[0]]
		for _, p := range parts[:len(parts)-1] {
			if skippedSourceDirs[p] {
				skip = true
			}
		}
		if !skip {
			files = append(files, rel)
		}
	}
	sort.Strings(files)
	return &goSource{dir: dir, fset: token.NewFileSet(), files: files, parsed: map[string]*ast.File{}, symbols: map[string]map[string]declaration{}}
}

// parse is the syntax tree of a module file, best effort: a file that does
// not parse in full still yields the declarations before its error, and one
// that cannot be read yields none.
func (s *goSource) parse(rel string) *ast.File {
	if f, ok := s.parsed[rel]; ok {
		return f
	}
	f, _ := parser.ParseFile(s.fset, filepath.Join(s.dir, filepath.FromSlash(rel)), nil, parser.SkipObjectResolution)
	s.parsed[rel] = f
	return f
}

func (s *goSource) filesIn(dir string) []string {
	var out []string
	for _, rel := range s.files {
		if path.Dir(rel) == dir {
			out = append(out, rel)
		}
	}
	return out
}

func (s *goSource) line(pos token.Pos) int { return s.fset.Position(pos).Line }

// symbolsIn are the package-level declarations of the package in dir, the
// first declaration of a name winning.
func (s *goSource) symbolsIn(dir string) map[string]declaration {
	if t, ok := s.symbols[dir]; ok {
		return t
	}
	table := map[string]declaration{}
	add := func(name *ast.Ident, d declaration) {
		if _, seen := table[name.Name]; !seen {
			d.name = name
			table[name.Name] = d
		}
	}
	for _, rel := range s.filesIn(dir) {
		f := s.parse(rel)
		if f == nil {
			continue
		}
		for _, decl := range f.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				if d.Recv == nil {
					add(d.Name, declaration{file: rel, line: s.line(d.Pos()), description: "a function"})
				}
			case *ast.GenDecl:
				for _, spec := range d.Specs {
					switch sp := spec.(type) {
					case *ast.ValueSpec:
						for i, name := range sp.Names {
							if d.Tok == token.CONST {
								add(name, declaration{file: rel, line: s.line(sp.Pos()), description: "a const"})
								continue
							}
							var value ast.Expr
							if i < len(sp.Values) {
								value = sp.Values[i]
							}
							description, injectable := varSettable(sp.Type, value)
							add(name, declaration{file: rel, line: s.line(sp.Pos()), description: description, injectable: injectable})
						}
					case *ast.TypeSpec:
						add(sp.Name, declaration{file: rel, line: s.line(sp.Pos()), description: "a type"})
					}
				}
			}
		}
	}
	s.symbols[dir] = table
	return table
}

// varSettable says what a var is and whether the linker can set it: a
// package-level string var, uninitialized or initialized to a string
// literal.
func varSettable(typ ast.Expr, value ast.Expr) (string, bool) {
	literal := false
	if lit, ok := value.(*ast.BasicLit); ok && lit.Kind == token.STRING {
		literal = true
	}
	switch {
	case typ != nil && types.ExprString(typ) != "string":
		return fmt.Sprintf("a var of type `%s`", types.ExprString(typ)), false
	case value != nil && !literal:
		return "a var initialized to a non-constant expression", false
	case typ == nil && value == nil:
		return "a var whose type cannot be read as string", false
	}
	return "a package-level string var", true
}

func (s *goSource) stringVarNames(dir string) []string {
	var names []string
	for name, d := range s.symbolsIn(dir) {
		if d.injectable {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}

// isRead reports whether anything in the module names symbol besides its
// declaration. Module-wide, because a symbol injected into a library
// package is read by the binary importing it (version.Value).
func (s *goSource) isRead(symbol string, d declaration) bool {
	for _, rel := range s.files {
		f := s.parse(rel)
		if f == nil {
			continue
		}
		found := false
		for _, decl := range f.Decls {
			if g, ok := decl.(*ast.GenDecl); ok && g.Tok == token.IMPORT {
				continue
			}
			ast.Inspect(decl, func(n ast.Node) bool {
				if found {
					return false
				}
				if id, ok := n.(*ast.Ident); ok && id.Name == symbol && id != d.name {
					found = true
				}
				return true
			})
			if found {
				return true
			}
		}
	}
	return false
}

// mainPackageDirs are the module-relative directories declaring package
// main, in file order.
func (s *goSource) mainPackageDirs() []string {
	var dirs []string
	seen := map[string]bool{}
	for _, rel := range s.files {
		f := s.parse(rel)
		if f == nil || f.Name == nil || f.Name.Name != "main" {
			continue
		}
		dir := path.Dir(rel)
		if !seen[dir] {
			seen[dir] = true
			dirs = append(dirs, dir)
		}
	}
	return dirs
}

func dirLabel(dir string) string {
	if dir == "." {
		return "the module root"
	}
	return "`" + dir + "`"
}

// resolveDirectory is the module-relative directory an occurrence's target
// names, or the reason it names none.
func resolveDirectory(occ Occurrence, text string, builds []goreleaserBuild, haveBuilds bool, modulePath string, s *goSource) (string, string) {
	if occ.ImportPath == "" {
		if strings.Contains(occ.Target, "{{") {
			return "", "the import path carries a build-time template, so it names no directory"
		}
		return "", "the target does not end in a Go identifier, so it names no symbol"
	}
	if occ.ImportPath != "main" {
		switch {
		case modulePath == "":
			return "", "this module's go.mod declares no module path, so a qualified import path cannot be resolved to a directory"
		case occ.ImportPath == modulePath:
			return ".", ""
		case strings.HasPrefix(occ.ImportPath, modulePath+"/"):
			return occ.ImportPath[len(modulePath)+1:], ""
		}
		return "", fmt.Sprintf("`%s` is outside this module (%s), so its source is not here to compare against", occ.ImportPath, modulePath)
	}
	if haveBuilds {
		matching := map[string]bool{}
		for _, b := range builds {
			if strings.Contains(b.ldflags, occ.Target) {
				matching[b.main] = true
			}
		}
		if len(matching) == 0 {
			for _, b := range builds {
				matching[b.main] = true
			}
		}
		if len(matching) == 1 {
			for m := range matching {
				return m, ""
			}
		}
		return "", "several goreleaser builds declare different main packages and none of them owns this flag, so `main` names no one directory"
	}
	if dir, ok := explicitBuildTarget(text, occ.Line-1); ok {
		return dir, ""
	}
	mains := s.mainPackageDirs()
	for _, m := range mains {
		if m == "." {
			return ".", ""
		}
	}
	switch len(mains) {
	case 1:
		return mains[0], ""
	case 0:
		return "", "this module declares no `package main`, so a bare `main` import path names no directory"
	}
	return "", "this file names no package to build and the module has several main packages (" + strings.Join(mains, ", ") + "), so `main` names no one directory"
}

// EvaluateLdflags compares every -X target in the modules' tracked build
// configuration against their Go source. A module directory without a
// go.mod is passed over.
func EvaluateLdflags(modules []LdflagsModule) (LdflagsVerdict, error) {
	if len(modules) == 0 {
		return LdflagsVerdict{SkipReason: "no Go module in this project"}, nil
	}
	var v LdflagsVerdict
	warned := map[string]bool{}
	count := 0
	for _, m := range modules {
		f, found, err := Read(m.Dir)
		if err != nil {
			return LdflagsVerdict{}, err
		}
		if !found {
			continue
		}
		count++
		modulePath := ""
		if f.Module != nil {
			modulePath = f.Module.Mod.Path
		}
		if err := evaluateModule(m, modulePath, &v, warned); err != nil {
			return LdflagsVerdict{}, err
		}
	}
	if count == 0 {
		return LdflagsVerdict{SkipReason: "no go.mod found in any Go target"}, nil
	}
	return v, nil
}

func evaluateModule(m LdflagsModule, modulePath string, v *LdflagsVerdict, warned map[string]bool) error {
	tracked := make([]string, len(m.Tracked))
	for i, rel := range m.Tracked {
		tracked[i] = filepath.ToSlash(rel)
	}
	sort.Strings(tracked)
	foreign := foreignDirs(tracked, m.NestedMembers)
	source := newGoSource(m.Dir, m.Listed, m.NestedMembers)
	for _, rel := range tracked {
		if !IsBuildFile(rel) || insideAny(rel, foreign) {
			continue
		}
		data, err := os.ReadFile(filepath.Join(m.Dir, filepath.FromSlash(rel)))
		if errors.Is(err, os.ErrNotExist) {
			// Tracked but deleted in the working tree: nothing to read.
			continue
		}
		if err != nil {
			return fmt.Errorf("reading %s: %w", rel, err)
		}
		text := string(data)
		if !strings.Contains(text, "-X") {
			continue
		}
		occurrences := FindOccurrences(text, rel)
		if len(occurrences) == 0 {
			continue
		}
		var builds []goreleaserBuild
		haveBuilds := false
		if goreleaserNames[strings.ToLower(path.Base(rel))] {
			builds, haveBuilds = goreleaserBuilds(text)
		}
		for _, occ := range occurrences {
			dir, reason := resolveDirectory(occ, text, builds, haveBuilds, modulePath, source)
			if reason == "" && (dir == ".." || strings.HasPrefix(dir, "../")) {
				reason = fmt.Sprintf("it resolves to %s, outside this module", dir)
			}
			if reason != "" {
				v.Notes = append(v.Notes, fmt.Sprintf("%s:%d: `%s` was not verified: %s.", occ.File, occ.Line, occ.Target, reason))
				continue
			}
			d, ok := source.symbolsIn(dir)[occ.Symbol]
			if !ok {
				inventory := "That directory declares no package-level string var at all."
				if names := source.stringVarNames(dir); len(names) > 0 {
					quoted := make([]string, len(names))
					for i, n := range names {
						quoted[i] = "`" + n + "`"
					}
					inventory = "Package-level string vars declared there: " + strings.Join(quoted, ", ") + "."
				}
				v.Problems = append(v.Problems, fmt.Sprintf("%s:%d: the linker is told to set `%s` (-X), but %s declares no package-level `%s`. A -X flag naming a symbol that does not exist links SILENTLY -- nothing is set, and every built binary keeps the fallback value in the code. %s Rename the Go variable to `%s`, or change the -X target to a name the package declares.",
					occ.File, occ.Line, occ.Target, dirLabel(dir), occ.Symbol, inventory, occ.Symbol))
				continue
			}
			if !d.injectable {
				v.Problems = append(v.Problems, fmt.Sprintf("%s:%d: the linker is told to set `%s` (-X), but %s:%d declares `%s` as %s -- the linker can only set a package-level `var` of type string that is uninitialized or initialized to a constant string, so this flag links SILENTLY and sets nothing. Declare it as `var %s string` in %s, or change the -X target to a symbol that is one.",
					occ.File, occ.Line, occ.Target, d.file, d.line, occ.Symbol, d.description, occ.Symbol, dirLabel(dir)))
				continue
			}
			v.Verified++
			key := m.Dir + "\x00" + dir + "\x00" + occ.Symbol
			if warned[key] || source.isRead(occ.Symbol, d) {
				continue
			}
			warned[key] = true
			v.Warnings = append(v.Warnings, fmt.Sprintf("%s:%d: `%s` is declared at %s:%d and the linker can set it, but nothing in this module reads it -- the injected value goes nowhere and the binary reports whatever the code uses instead. Read `%s` where the version is reported, or drop the -X flag.",
				occ.File, occ.Line, occ.Target, d.file, d.line, occ.Symbol))
		}
	}
	return nil
}
