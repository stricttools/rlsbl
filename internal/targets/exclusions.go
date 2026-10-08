package targets

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"

	"github.com/stricttools/rlsbl/internal/git"
)

// Scaffold spells the private-path rule in each ecosystem's own exclusion
// syntax, so a newly scaffolded project passes the refusal without hand
// edits: a hatchling pyproject.toml gets the entries under exclude in
// [tool.hatch.build.targets.sdist] (uv builds the wheel from that sdist),
// .npmignore gets NpmignoreBlock, and a Go module gets a stub go.mod in each
// private directory at its root that holds tracked files. It also tells a
// member's own test runner to skip the members nested inside it: pytest
// gets one --ignore=<path> per nested member in addopts (path-exact, where
// norecursedirs would match by basename), the go command never descends
// into a directory with its own go.mod, and npm's runner is the project's
// choice, configured in a file rlsbl neither writes nor reads. The files
// are the project's: merged into, keeping every other byte, and never
// rendered whole.

// NpmignoreBlock is the private-path lines of a scaffolded .npmignore.
func NpmignoreBlock() string { return strings.Join(ExcludeEntries(), "\n") }

// GoStubDirectories are the private directories at the project root (dir,
// absolute, inside repo) a Go module needs a stub go.mod in: .strictmetadata
// always for a module at the repository root, since scaffold commits files
// there, and every private directory holding a tracked file, a module's own
// .strictmetadata (strictcli's schema dump) included. The proxy zips only committed files, so
// a directory the project does not track needs no stub (and a stub in an
// ignored directory could never be committed). The scratch directories carry
// their own go.mod and are left out.
func GoStubDirectories(repo git.Repo, dir string) ([]string, error) {
	rel, err := filepath.Rel(repo.Dir(), dir)
	if err != nil {
		return nil, err
	}
	rel = filepath.ToSlash(rel)
	tracked, err := repo.TrackedFiles()
	if err != nil {
		return nil, err
	}
	holding := map[string]bool{}
	for _, f := range tracked {
		if rel != "." {
			var ok bool
			if f, ok = strings.CutPrefix(f, rel+"/"); !ok {
				continue
			}
		}
		if top, _, nested := strings.Cut(f, "/"); nested {
			holding[top] = true
		}
	}
	var out []string
	if rel == "." || holding[".strictmetadata"] {
		out = append(out, ".strictmetadata")
	}
	for _, name := range append(append([]string(nil), PrivateRootDirs...), PrivateDirs...) {
		if name == ".strictmetadata" || name == "experiments" || name == "screenshots" {
			continue
		}
		if holding[name] {
			out = append(out, name)
		}
	}
	return out, nil
}

// BuildBackend is [build-system] build-backend of dir/pyproject.toml, or ""
// when it declares none.
func BuildBackend(dir string) (string, error) {
	p, found, err := readPyproject(dir)
	if err != nil || !found {
		return "", err
	}
	system, _ := p.table("build-system")
	backend, _ := system["build-backend"].(string)
	return backend, nil
}

// sdistExcludePath is where hatchling reads the sdist's exclusions.
const sdistExcludePath = "tool.hatch.build.targets.sdist"

// stringArray reads the array at key of doc as strings; found is false when
// the key is absent.
func stringArray(doc *tomledit.Document, key string) ([]string, bool, error) {
	node, ok := doc.Lookup(key)
	if !ok {
		return nil, false, nil
	}
	array, ok := node.(*tomledit.ArrayNode)
	if !ok {
		return nil, true, fmt.Errorf("%s is not an array", key)
	}
	var out []string
	for _, el := range array.Elements() {
		s, ok := el.(tomledit.Scalar)
		if !ok {
			return nil, true, fmt.Errorf("%s holds something that is not a string", key)
		}
		text, ok := s.Value().(string)
		if !ok {
			return nil, true, fmt.Errorf("%s holds something that is not a string", key)
		}
		out = append(out, text)
	}
	return out, true, nil
}

// appendTableText appends a new [header] table with one array of strings to
// the document text, and checks the result still parses.
func appendTableText(data []byte, header, key string, values []string) ([]byte, error) {
	var b strings.Builder
	b.Write(data)
	if len(data) > 0 && !strings.HasSuffix(string(data), "\n") {
		b.WriteString("\n")
	}
	if len(data) > 0 {
		b.WriteString("\n")
	}
	b.WriteString("[" + header + "]\n" + key + " = [\n")
	for _, v := range values {
		b.WriteString("    " + tomledit.QuoteString(v) + ",\n")
	}
	b.WriteString("]\n")
	out := []byte(b.String())
	if _, err := tomledit.Parse(out); err != nil {
		return nil, fmt.Errorf("adding [%s] would break the file: %w", header, err)
	}
	return out, nil
}

// MergeHatchSdistExclusions adds every private-path exclude entry the
// pyproject.toml text data lacks to [tool.hatch.build.targets.sdist]
// exclude, keeping the project's own entries and every other byte; path
// names the file in errors. changed is false when nothing is missing.
func MergeHatchSdistExclusions(path string, data []byte) ([]byte, bool, error) {
	doc, err := tomledit.Parse(data)
	if err != nil {
		return nil, false, fmt.Errorf("%s: %w", path, err)
	}
	key := sdistExcludePath + ".exclude"
	existing, found, err := stringArray(doc, key)
	if err != nil {
		return nil, false, fmt.Errorf("%s: %w", path, err)
	}
	var missing []string
	for _, e := range ExcludeEntries() {
		if !contains(existing, e) {
			missing = append(missing, e)
		}
	}
	if len(missing) == 0 {
		return data, false, nil
	}
	switch {
	case found:
		for _, m := range missing {
			if err := doc.AppendToArray(key, m); err != nil {
				return nil, false, fmt.Errorf("%s: %w", path, err)
			}
		}
	case doc.Has(sdistExcludePath):
		values := make([]any, len(missing))
		for i, m := range missing {
			values[i] = m
		}
		if err := doc.SetCreate(key, values); err != nil {
			return nil, false, fmt.Errorf("%s: %w", path, err)
		}
	default:
		out, err := appendTableText(data, sdistExcludePath, "exclude", missing)
		if err != nil {
			return nil, false, fmt.Errorf("%s: %w", path, err)
		}
		return out, true, nil
	}
	return doc.Bytes(), true, nil
}

// NestedPathsInside are the nested member directories (absolute) that lie
// strictly inside targetDir (absolute), relative to it and sorted.
func NestedPathsInside(targetDir string, nested []string) []string {
	base := filepath.Clean(targetDir)
	var out []string
	for _, n := range nested {
		rel, err := filepath.Rel(base, filepath.Clean(n))
		if err != nil || rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			continue
		}
		out = append(out, filepath.ToSlash(rel))
	}
	sort.Strings(out)
	return out
}

// PytestIgnoreOption is the addopts token making pytest skip rel, which
// pytest resolves against the directory it starts in: the member's own.
func PytestIgnoreOption(rel string) string { return "--ignore=" + rel }

// NestedExclusionCheck names the check refusing a missing nested-member
// exclusion.
const NestedExclusionCheck = "nested-member-runner-exclusion"

// iniOptionsPath is pytest's table in pyproject.toml, and addoptsPath its
// options.
const (
	iniOptionsPath = "tool.pytest.ini_options"
	addoptsPath    = iniOptionsPath + ".addopts"
)

// addoptsTokens are the tokens of pyproject.toml's addopts: an array as it
// stands, a string split the way a shell splits it.
func addoptsTokens(doc *tomledit.Document) (tokens []string, isString, found bool, err error) {
	node, ok := doc.Lookup(addoptsPath)
	if !ok {
		return nil, false, false, nil
	}
	if s, ok := node.(tomledit.Scalar); ok {
		text, ok := s.Value().(string)
		if !ok {
			return nil, false, true, fmt.Errorf("%s is neither a string nor an array", addoptsPath)
		}
		tokens, err := shellSplit(text)
		return tokens, true, true, err
	}
	tokens, _, err = stringArray(doc, addoptsPath)
	return tokens, false, true, err
}

// shellSplit splits text into words the way a POSIX shell does: whitespace
// separates, single quotes keep everything, double quotes keep everything
// but a backslash before " or \, and a bare backslash escapes the next
// character. An unclosed quote is refused.
func shellSplit(text string) ([]string, error) {
	var words []string
	var word strings.Builder
	inWord := false
	for i := 0; i < len(text); i++ {
		c := text[i]
		switch {
		case c == '\'':
			end := strings.IndexByte(text[i+1:], '\'')
			if end < 0 {
				return nil, fmt.Errorf("addopts %q has an unclosed single quote", text)
			}
			word.WriteString(text[i+1 : i+1+end])
			i += end + 1
			inWord = true
		case c == '"':
			i++
			for ; i < len(text) && text[i] != '"'; i++ {
				if text[i] == '\\' && i+1 < len(text) && (text[i+1] == '"' || text[i+1] == '\\') {
					i++
				}
				word.WriteByte(text[i])
			}
			if i >= len(text) {
				return nil, fmt.Errorf("addopts %q has an unclosed double quote", text)
			}
			inWord = true
		case c == '\\' && i+1 < len(text):
			i++
			word.WriteByte(text[i])
			inWord = true
		case c == ' ' || c == '\t' || c == '\n' || c == '\r':
			if inWord {
				words = append(words, word.String())
				word.Reset()
				inWord = false
			}
		default:
			word.WriteByte(c)
			inWord = true
		}
	}
	if inWord {
		words = append(words, word.String())
	}
	return words, nil
}

// MissingNestedExclusions are the pytest options targetDir's runner
// configuration still lacks for the nested members nestedRel (relative to
// targetDir), and the file they belong in. owed is false when none is
// missing. A pytest.ini (which outranks pyproject.toml) or a missing
// pyproject.toml owes every option, named in that file, since rlsbl writes
// only pyproject.toml.
func MissingNestedExclusions(targetDir string, nestedRel []string) (file string, missing []string, owed bool, err error) {
	if len(nestedRel) == 0 {
		return "", nil, false, nil
	}
	wanted := make([]string, len(nestedRel))
	for i, rel := range nestedRel {
		wanted[i] = PytestIgnoreOption(rel)
	}
	ini := filepath.Join(targetDir, "pytest.ini")
	pyprojectPath := filepath.Join(targetDir, Pyproject)
	hasIni, err := exists(ini)
	if err != nil {
		return "", nil, false, err
	}
	if hasIni {
		return ini, wanted, true, nil
	}
	p, found, err := readPyproject(targetDir)
	if err != nil {
		return "", nil, false, err
	}
	if !found {
		return pyprojectPath, wanted, true, nil
	}
	tokens, _, _, err := addoptsTokens(p.doc)
	if err != nil {
		return "", nil, false, fmt.Errorf("%s: %w", pyprojectPath, err)
	}
	for _, w := range wanted {
		if !contains(tokens, w) {
			missing = append(missing, w)
		}
	}
	return pyprojectPath, missing, len(missing) > 0, nil
}

// MergePytestIgnores adds the missing options to pyproject.toml's addopts
// in the text data, keeping its spelling (a string gains words, an array
// gains elements) and every other byte; path names the file in errors.
// changed is false when nothing is missing.
func MergePytestIgnores(path string, data []byte, options []string) ([]byte, bool, error) {
	doc, err := tomledit.Parse(data)
	if err != nil {
		return nil, false, fmt.Errorf("%s: %w", path, err)
	}
	tokens, isString, found, err := addoptsTokens(doc)
	if err != nil {
		return nil, false, fmt.Errorf("%s: %w", path, err)
	}
	var missing []string
	for _, o := range options {
		if !contains(tokens, o) && !contains(missing, o) {
			missing = append(missing, o)
		}
	}
	if len(missing) == 0 {
		return data, false, nil
	}
	switch {
	case found && isString:
		current, err := doc.GetString(addoptsPath)
		if err != nil {
			return nil, false, fmt.Errorf("%s: %w", path, err)
		}
		if err := doc.Set(addoptsPath, strings.TrimSpace(strings.Join(append([]string{strings.TrimSpace(current)}, missing...), " "))); err != nil {
			return nil, false, fmt.Errorf("%s: %w", path, err)
		}
	case found:
		for _, m := range missing {
			if err := doc.AppendToArray(addoptsPath, m); err != nil {
				return nil, false, fmt.Errorf("%s: %w", path, err)
			}
		}
	case doc.Has(iniOptionsPath):
		values := make([]any, len(missing))
		for i, m := range missing {
			values[i] = m
		}
		if err := doc.SetCreate(addoptsPath, values); err != nil {
			return nil, false, fmt.Errorf("%s: %w", path, err)
		}
	default:
		out, err := appendTableText(data, iniOptionsPath, "addopts", missing)
		if err != nil {
			return nil, false, fmt.Errorf("%s: %w", path, err)
		}
		return out, true, nil
	}
	return doc.Bytes(), true, nil
}
