package rewrite

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"go/parser"
	"go/token"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/targets"
)

// The roles a file plays in a module-path rename.
const (
	roleGoMod      = "go.mod"
	roleGoSource   = "go source"
	roleSchemaDump = "strictcli schema dump"
)

// scratchDirs are the directories at the repository root that hold
// throwaway probes and produced repositories, never this repository's code.
var scratchDirs = []string{"experiments", "screenshots"}

// ModuleFile is one file's pending module-path rewrite, as observed.
type ModuleFile struct {
	// Path is absolute; Rel is relative to the repository root.
	Path string
	Rel  string
	// Role is go.mod, go source, or strictcli schema dump.
	Role        string
	Occurrences int
	// Sites are one line per occurrence.
	Sites []string
}

// ModuleRename moves one module path to another across a repository.
type ModuleRename struct {
	// Root is the repository root, absolute.
	Root string
	From string
	To   string
	// nested are the declared module paths strictly under From: modules of
	// their own, which own their tokens and imports and are left alone.
	nested []string
}

// ValidateModulePaths refuses module paths no rename can run between.
func ValidateModulePaths(from, to string) error {
	for _, p := range []struct{ flag, value string }{{"--from-module", from}, {"--to-module", to}} {
		if strings.IndexFunc(p.value, unicode.IsSpace) >= 0 {
			return fmt.Errorf("%s must not contain whitespace: %q", p.flag, p.value)
		}
	}
	if from == to {
		return errors.New("--from-module and --to-module are the same path; nothing to rename")
	}
	return nil
}

// sweptFile reports whether a repository-relative file is one the rename
// may touch: never under a vendor/ or .git/ directory at any depth, and
// never under a scratch directory at the root. Every other directory is
// visited, including build, dist, static, and assets, which are ordinary Go
// package names.
func sweptFile(rel string) bool {
	parts := strings.Split(rel, "/")
	for _, s := range scratchDirs {
		if len(parts) > 1 && parts[0] == s {
			return false
		}
	}
	for _, part := range parts[:len(parts)-1] {
		if part == "vendor" || part == ".git" {
			return false
		}
	}
	return true
}

// isSchemaDump reports whether a repository-relative file is a committed
// strictcli schema dump.
func isSchemaDump(rel string) bool {
	return rel == targets.SchemaDumpPath || strings.HasSuffix(rel, "/"+targets.SchemaDumpPath)
}

// declaredModule is the module path the go.mod at path declares, read line
// by line as the go command reads the directive; "" when it declares none
// or cannot be read.
func declaredModule(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	for _, raw := range strings.Split(string(data), "\n") {
		line := strings.TrimSpace(strings.SplitN(raw, "//", 2)[0])
		if rest, ok := strings.CutPrefix(line, "module"); ok && (rest == "" || rest[0] == ' ' || rest[0] == '\t') {
			return strings.Trim(strings.TrimSpace(rest), `"`)
		}
	}
	return ""
}

// belongs reports whether a module path or import path is From's own: under
// it, and owned by no nested module.
func (m *ModuleRename) belongs(p string) bool {
	owner, ok := gomodule.OwningModule(p, append([]string{m.From}, m.nested...), gomodule.GoSeparator)
	return ok && owner == m.From
}

// moved is p re-rooted from From onto To.
func (m *ModuleRename) moved(p string) string {
	return gomodule.RewriteModulePrefix(p, m.From, m.To, gomodule.GoSeparator)
}

// tokenRune reports whether r continues a module-path token. slash says
// whether "/" counts: a token continuing past the end of From at a "/" is
// still From's (From/v2), while a "/" before it starts another token.
func tokenRune(r rune, slash bool) bool {
	return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '.' || r == '_' || r == '-' || (slash && r == '/')
}

// tokenAt is the whole module-path token starting at start.
func tokenAt(text string, start int) string {
	end := start
	for end < len(text) {
		r, size := utf8.DecodeRuneInString(text[end:])
		if !tokenRune(r, true) && r != '~' {
			break
		}
		end += size
	}
	return text[start:end]
}

// rewriteGoMod rewrites From's module-path tokens in go.mod text: the module
// directive and every require, replace, exclude, and retract reference.
// Anything after "//" on a line is a comment and is left as it is.
func (m *ModuleRename) rewriteGoMod(text string) (string, int, []string) {
	var out strings.Builder
	count := 0
	var sites []string
	for i, raw := range strings.SplitAfter(text, "\n") {
		stripped := strings.TrimRight(raw, "\r\n")
		eol := raw[len(stripped):]
		code, comment := stripped, ""
		if at := strings.Index(stripped, "//"); at >= 0 {
			code, comment = stripped[:at], stripped[at:]
		}
		var hits []int
		for pos := 0; pos <= len(code)-len(m.From); {
			at := strings.Index(code[pos:], m.From)
			if at < 0 {
				break
			}
			at += pos
			before, _ := utf8.DecodeLastRuneInString(code[:at])
			after, _ := utf8.DecodeRuneInString(code[at+len(m.From):])
			boundary := (at == 0 || !tokenRune(before, true)) && (at+len(m.From) == len(code) || !tokenRune(after, false))
			if !boundary {
				pos = at + 1
				continue
			}
			if token := tokenAt(code, at); m.belongs(token) {
				hits = append(hits, at)
				sites = append(sites, fmt.Sprintf("line %d: %s -> %s", i+1, token, m.moved(token)))
			}
			pos = at + len(m.From)
		}
		for j := len(hits) - 1; j >= 0; j-- {
			code = code[:hits[j]] + m.To + code[hits[j]+len(m.From):]
		}
		count += len(hits)
		out.WriteString(code + comment + eol)
	}
	return out.String(), count, sites
}

// schemaProjectID is the top-level project_id member of a strictcli schema
// dump in strictcli's canonical encoding: two spaces of indent at the start
// of a line, so a deeper project_id (a flag named so) never matches.
var schemaProjectID = regexp.MustCompile(`(?m)^  "project_id": "((?:[^"\\]|\\.)*)"(,?)$`)

// rewriteSchemaDump rewrites a schema dump's project_id when it is one of
// From's module paths, as a textual patch: decoding and encoding the whole
// document would change strictcli's encoding of everything else in it.
func (m *ModuleRename) rewriteSchemaDump(text string) (string, int, []string) {
	match := schemaProjectID.FindStringSubmatchIndex(text)
	if match == nil {
		return text, 0, nil
	}
	var current string
	if err := json.Unmarshal([]byte(`"`+text[match[2]:match[3]]+`"`), &current); err != nil {
		return text, 0, nil
	}
	if !gomodule.ImportUnderModule(current, m.From) || !m.belongs(current) {
		return text, 0, nil
	}
	renamed := m.moved(current)
	var encoded bytes.Buffer
	enc := json.NewEncoder(&encoded)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(renamed); err != nil {
		return text, 0, nil
	}
	line := strings.Count(text[:match[0]], "\n") + 1
	replacement := `  "project_id": ` + strings.TrimSuffix(encoded.String(), "\n") + text[match[4]:match[5]]
	return text[:match[0]] + replacement + text[match[1]:], 1, []string{fmt.Sprintf("line %d: project_id %s -> %s", line, current, renamed)}
}

// rewriteGoSource rewrites the import specs of From's packages in Go source,
// located with go/parser and rewritten at the exact bytes of each spec's
// string literal, keeping its quote form. Nothing outside an import spec is
// touched. A file that does not parse is refused, naming it.
func (m *ModuleRename) rewriteGoSource(relPath, text string) (string, int, []string, error) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, relPath, text, parser.ImportsOnly)
	if err != nil {
		return "", 0, nil, fmt.Errorf("%s does not parse as Go source, so its import sites cannot be located: %v. Fix the file, or leave the rename until it parses", relPath, err)
	}
	type site struct {
		offset  int
		literal string
		path    string
		line    int
	}
	var sites []site
	for _, spec := range f.Imports {
		p, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			return "", 0, nil, fmt.Errorf("%s: the import %s does not unquote: %v", relPath, spec.Path.Value, err)
		}
		if !gomodule.ImportUnderModule(p, m.From) || !m.belongs(p) {
			continue
		}
		pos := fset.Position(spec.Path.Pos())
		sites = append(sites, site{offset: pos.Offset, literal: spec.Path.Value, path: p, line: pos.Line})
	}
	var descriptions []string
	out := text
	for i := len(sites) - 1; i >= 0; i-- {
		s := sites[i]
		if s.offset+len(s.literal) > len(out) || out[s.offset:s.offset+len(s.literal)] != s.literal {
			return "", 0, nil, fmt.Errorf("%s: line %d does not hold the import literal %s the parser reported", relPath, s.line, s.literal)
		}
		quote := s.literal[:1]
		out = out[:s.offset] + quote + m.moved(s.path) + quote + out[s.offset+len(s.literal):]
	}
	for _, s := range sites {
		descriptions = append(descriptions, fmt.Sprintf("line %d: %s -> %s", s.line, s.path, m.moved(s.path)))
	}
	return out, len(sites), descriptions, nil
}

// rewrite rewrites one file's text in the role it plays.
func (m *ModuleRename) rewrite(role, relPath, text string) (string, int, []string, error) {
	switch role {
	case roleGoMod:
		out, n, sites := m.rewriteGoMod(text)
		return out, n, sites, nil
	case roleSchemaDump:
		out, n, sites := m.rewriteSchemaDump(text)
		return out, n, sites, nil
	}
	return m.rewriteGoSource(relPath, text)
}

// fileRole is the role of a repository-relative file, and "" for a file
// the rename never reads.
func fileRole(rel string) string {
	switch {
	case path.Base(rel) == "go.mod":
		return roleGoMod
	case isSchemaDump(rel):
		return roleSchemaDump
	case strings.HasSuffix(rel, ".go"):
		return roleGoSource
	}
	return ""
}

// ObserveModuleFiles plans the rename: one ModuleFile per file that would
// change, go.mod files first, then Go sources, then schema dumps. The files
// considered are what git lists as tracked or untracked and not ignored, so
// an ignored third-party clone is never touched. It also returns every
// module path the repository's go.mod files declare.
func (m *ModuleRename) ObserveModuleFiles(r git.Runner) ([]ModuleFile, []string, error) {
	repo, err := git.Open(r, m.Root)
	if err != nil {
		return nil, nil, err
	}
	listed, err := repo.TrackedAndUntrackedFiles()
	if err != nil {
		return nil, nil, err
	}
	byRole := map[string][]string{}
	for _, f := range listed {
		if !sweptFile(f) {
			continue
		}
		if role := fileRole(f); role != "" {
			byRole[role] = append(byRole[role], f)
		}
	}
	declared := map[string]bool{}
	for _, f := range byRole[roleGoMod] {
		if p := declaredModule(filepath.Join(m.Root, filepath.FromSlash(f))); p != "" {
			declared[p] = true
		}
	}
	m.nested = nil
	var modules []string
	for p := range declared {
		modules = append(modules, p)
		if p != m.From && gomodule.ImportUnderModule(p, m.From) {
			m.nested = append(m.nested, p)
		}
	}
	sort.Strings(modules)
	sort.Strings(m.nested)
	var files []ModuleFile
	for _, role := range []string{roleGoMod, roleGoSource, roleSchemaDump} {
		names := byRole[role]
		sort.Strings(names)
		for _, f := range names {
			abs := filepath.Join(m.Root, filepath.FromSlash(f))
			data, err := os.ReadFile(abs)
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			if err != nil {
				return nil, nil, fmt.Errorf("reading %s: %w", f, err)
			}
			_, n, sites, err := m.rewrite(role, f, string(data))
			if err != nil {
				return nil, nil, err
			}
			if n > 0 {
				files = append(files, ModuleFile{Path: abs, Rel: f, Role: role, Occurrences: n, Sites: sites})
			}
		}
	}
	return files, modules, nil
}

// ModuleItem is the preview item of one file's rewrite.
func ModuleItem(f ModuleFile) previewapply.Item {
	return previewapply.Item{
		Key:     f.Rel,
		State:   "rewrite",
		Summary: fmt.Sprintf("%s in this %s", plural(f.Occurrences, "occurrence"), f.Role),
		Facts:   f.Sites,
		Actions: []string{fmt.Sprintf("apply would rewrite %s here.", plural(f.Occurrences, "occurrence"))},
		Data:    f,
	}
}

// ApplyModuleFile writes one file's rewrite, deriving it from disk again and
// refusing when its count moved since the preview.
func (m *ModuleRename) ApplyModuleFile(e *strictcli.Effects, f ModuleFile) error {
	data, err := os.ReadFile(f.Path)
	if err != nil {
		return fmt.Errorf("%s, which the plan named, could not be read at apply time (%v): the working tree changed underneath the plan; run the command with --dry-run again, which plans from the tree as it is now", f.Rel, err)
	}
	out, n, _, err := m.rewrite(f.Role, f.Rel, string(data))
	if err != nil {
		return err
	}
	if err := previewapply.CountMoved(f.Rel, f.Occurrences, n); err != nil {
		return err
	}
	return replaceFile(e, f.Path, []byte(out))
}

// observeGoModulePath is the whole plan of `rewrite go-module-path`.
func (m *ModuleRename) observeGoModulePath(o previewapply.Observer) (previewapply.Preview, error) {
	files, modules, err := m.ObserveModuleFiles(o)
	if err != nil {
		return previewapply.Preview{}, err
	}
	if len(files) == 0 {
		listing := "(no go.mod declares a module)"
		if len(modules) > 0 {
			listing = strings.Join(modules, ", ")
		}
		return previewapply.Preview{}, fmt.Errorf("nothing references '%s' anywhere in this repository -- no go.mod token and no import site. Check --from-module for a typo. Module paths declared here: %s.", m.From, listing)
	}
	var items []previewapply.Item
	total := 0
	for _, f := range files {
		items = append(items, ModuleItem(f))
		total += f.Occurrences
	}
	var facts []string
	owns := false
	for _, p := range modules {
		if p == m.From {
			owns = true
		}
	}
	if !owns {
		facts = append(facts, fmt.Sprintf("no go.mod here declares '%s': this repository consumes the module rather than owning it, so only references are rewritten.", m.From))
	}
	if len(m.nested) > 0 {
		facts = append(facts, fmt.Sprintf("left alone, as modules of their own: %s (rename each with its own invocation).", strings.Join(m.nested, ", ")))
	}
	items = append(items, previewapply.Item{
		Key:     "(total)",
		State:   "summary",
		Summary: fmt.Sprintf("%s across %s: %s -> %s", plural(total, "occurrence"), plural(len(files), "file"), m.From, m.To),
		Facts:   facts,
	})
	return previewapply.NewPreview(items...)
}

// RunGoModulePath is `rlsbl rewrite go-module-path`: rename the module path
// from to to across the repository rooted at root, previewed under --dry-run.
// A sweep that finds nothing is refused: the likely cause is a mistyped
// --from-module, and "nothing to do" would read as a rename that happened.
func RunGoModulePath(ctx *strictcli.Context, root, from, to string) error {
	if err := ValidateModulePaths(from, to); err != nil {
		return err
	}
	m := &ModuleRename{Root: root, From: from, To: to}
	written := 0
	_, err := previewapply.Reconcile(ctx, previewapply.Reconciler{
		Observe: m.observeGoModulePath,
		Apply: func(e *strictcli.Effects, it previewapply.Item) error {
			f, ok := it.Data.(ModuleFile)
			if !ok {
				return nil
			}
			if err := m.ApplyModuleFile(e, f); err != nil {
				return err
			}
			written++
			ctx.Info(fmt.Sprintf("  %s: rewrote %s", f.Rel, plural(f.Occurrences, "occurrence")))
			return nil
		},
		ShowKeys: true,
	})
	if err != nil {
		return err
	}
	if !ctx.DryRun() {
		ctx.Info(fmt.Sprintf("Renamed %s -> %s across %s.", from, to, plural(written, "file")))
	}
	return nil
}
