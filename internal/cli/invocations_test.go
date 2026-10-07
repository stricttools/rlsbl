package cli

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// The guard below checks every rlsbl command line a text names, in a help
// text, an error or any other string of the Go sources, and the
// documentation pages, against the help document of the application: the
// command must be registered, and every flag it passes must be one that
// command declares (or a framework flag). A text naming a command of the
// Python rlsbl, which the record migration names for an old layout's
// unfinished work, says so with "the Python rlsbl" and is left out.

// frameworkFlags are the flags strictcli gives every command.
var frameworkFlags = map[string]bool{"dry-run": true, "approve-consequential": true, "quiet": true, "verbose": true, "json": true, "help": true}

// schemaNode is a command or group of the help document.
type schemaNode struct {
	Commands map[string]*schemaNode `json:"commands"`
	Groups   map[string]*schemaNode `json:"groups"`
	Help     string                 `json:"help"`
	Flags    []schemaFlag           `json:"flags"`
}

// schemaFlag is a flag of the help document, with its elected choices.
type schemaFlag struct {
	Name      string         `json:"name"`
	Help      string         `json:"help"`
	Negatable *bool          `json:"negatable"`
	Presence  string         `json:"presence"`
	Nullable  *bool          `json:"nullable"`
	ElectBy   *string        `json:"elect_by"`
	Choices   []schemaChoice `json:"choices"`
}

// schemaChoice is one choice of a flag; an elected choice carries flags.
type schemaChoice struct {
	Name  string       `json:"name"`
	Help  string       `json:"help"`
	Flags []schemaFlag `json:"flags"`
}

// helpDocument is the application's help document.
func helpDocument(t *testing.T) *schemaNode {
	t.Helper()
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"help", "--json"})
	if r.ExitCode != 0 {
		t.Fatalf("help --json exited %d: %s", r.ExitCode, r.Stderr)
	}
	var doc schemaNode
	if err := json.Unmarshal([]byte(r.Stdout), &doc); err != nil {
		t.Fatalf("help --json printed no JSON: %v", err)
	}
	return &doc
}

// flagNames adds to names every flag spelling the flags accept: each flag,
// its negation when negatable, its clearing when nullable, and each choice of a flag elected by member
// flags, with the flags the choices carry (a choice's own value flag, named
// value, is the choice's spelling).
func flagNames(flags []schemaFlag, names map[string]bool) {
	for _, f := range flags {
		names[f.Name] = true
		if f.Negatable != nil && *f.Negatable {
			names["no-"+f.Name] = true
		}
		if f.Nullable != nil && *f.Nullable {
			names["unset-"+f.Name] = true
		}
		if f.ElectBy != nil && *f.ElectBy == "member-flags" {
			for _, c := range f.Choices {
				names[c.Name] = true
			}
		}
		for _, c := range f.Choices {
			for _, cf := range c.Flags {
				if cf.Name != "value" {
					flagNames([]schemaFlag{cf}, names)
				}
			}
		}
	}
}

// frameworkCommands are the commands strictcli gives every application
// without listing them in the help document.
var frameworkCommands = map[string]bool{"help": true}

// splitCommandLine splits a command line into its words, a quoted word
// (double or single quotes) being one word, and drops the brackets and
// ellipses of a usage line's optional parts.
func splitCommandLine(line string) []string {
	var words []string
	var word strings.Builder
	var quote rune
	inWord := false
	for _, r := range line {
		switch {
		case quote != 0:
			if r == quote {
				quote = 0
			} else {
				word.WriteRune(r)
			}
		case r == '"' || r == '\'':
			quote, inWord = r, true
			word.WriteRune('"')
		case r == ' ' || r == '\t':
			if inWord {
				words = append(words, word.String())
				word.Reset()
				inWord = false
			}
		default:
			word.WriteRune(r)
			inWord = true
		}
	}
	if inWord {
		words = append(words, word.String())
	}
	var cleaned []string
	for _, w := range words {
		w = strings.TrimSuffix(strings.Trim(w, "[]"), "...")
		w = strings.Trim(w, "[]")
		if w != "" {
			cleaned = append(cleaned, w)
		}
	}
	return cleaned
}

// commandLineProblem is what is wrong with the command line rlsbl args
// names, or "" when the help document registers it with every flag it
// passes, and with the choice each of its required boolean flags demands
// spelled (--watch or --no-watch, say), so the line runs as printed. A
// token formatted at run time (holding %) ends the check.
func commandLineProblem(doc *schemaNode, args []string) string {
	node, path := doc, "rlsbl"
	isCommand := false
	for _, tok := range args {
		if strings.Contains(tok, "%") {
			return ""
		}
		if strings.HasPrefix(tok, "-") {
			name, _, _ := strings.Cut(strings.TrimLeft(tok, "-"), "=")
			if frameworkFlags[name] {
				continue
			}
			names := map[string]bool{}
			flagNames(node.Flags, names)
			if !names[name] {
				return fmt.Sprintf("`%s` declares no flag %s", path, tok)
			}
			continue
		}
		if isCommand {
			continue
		}
		if node == doc && frameworkCommands[tok] {
			return ""
		}
		if next, ok := node.Commands[tok]; ok {
			node, path, isCommand = next, path+" "+tok, true
			continue
		}
		if next, ok := node.Groups[tok]; ok {
			node, path = next, path+" "+tok
			continue
		}
		if strings.HasPrefix(tok, "<") || strings.HasPrefix(tok, "\"") {
			return ""
		}
		return fmt.Sprintf("`%s` has no command %q", path, tok)
	}
	if isCommand {
		for _, f := range node.Flags {
			if f.Presence != "required" || f.Negatable == nil || !*f.Negatable {
				continue
			}
			if !spellsFlag(args, f.Name) && !spellsFlag(args, "no-"+f.Name) {
				return fmt.Sprintf("`%s` requires --%s or --no-%s, which the line does not spell", path, f.Name, f.Name)
			}
		}
	}
	return ""
}

// spellsFlag is whether args pass the flag name.
func spellsFlag(args []string, name string) bool {
	for _, a := range args {
		if a == "--"+name || strings.HasPrefix(a, "--"+name+"=") {
			return true
		}
	}
	return false
}

// commandSpan is a code span naming an rlsbl command line.
var commandSpan = regexp.MustCompile("`rlsbl( [^`]*)?`")

// commandLines are the rlsbl command lines text names in code spans.
func commandLines(text string) [][]string {
	var lines [][]string
	for _, m := range commandSpan.FindAllStringSubmatch(text, -1) {
		lines = append(lines, splitCommandLine(m[1]))
	}
	return lines
}

// fencedCommandLines are the rlsbl command lines in the fenced code blocks
// of a markdown page.
func fencedCommandLines(page string) [][]string {
	var lines [][]string
	inFence := false
	for _, line := range strings.Split(page, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "```") {
			inFence = !inFence
			continue
		}
		if inFence {
			trimmed = strings.TrimPrefix(trimmed, "$ ")
			if rest, ok := strings.CutPrefix(trimmed, "rlsbl "); ok {
				if i := strings.Index(rest, " #"); i >= 0 {
					rest = rest[:i]
				}
				lines = append(lines, splitCommandLine(rest))
			}
		}
	}
	return lines
}

// helpTexts are every help text of the help document, each labelled.
func helpTexts(node *schemaNode, path string, texts map[string]string) {
	texts[path] = node.Help
	var flags func(prefix string, fs []schemaFlag)
	flags = func(prefix string, fs []schemaFlag) {
		for _, f := range fs {
			texts[prefix+" --"+f.Name] = f.Help
			for _, c := range f.Choices {
				texts[prefix+" --"+f.Name+" "+c.Name] = c.Help
				flags(prefix, c.Flags)
			}
		}
	}
	flags(path, node.Flags)
	for name, c := range node.Commands {
		helpTexts(c, path+" "+name, texts)
	}
	for name, g := range node.Groups {
		helpTexts(g, path+" "+name, texts)
	}
}

// commandLineOffenders lists every command line the texts name that the
// help document does not register as written.
func commandLineOffenders(doc *schemaNode, texts map[string]string, fenced bool) []string {
	var offenders []string
	for label, text := range texts {
		if strings.Contains(text, "the Python rlsbl") {
			continue
		}
		lines := commandLines(text)
		if fenced {
			lines = append(lines, fencedCommandLines(text)...)
		}
		for _, args := range lines {
			if problem := commandLineProblem(doc, args); problem != "" {
				offenders = append(offenders, fmt.Sprintf("%s: `rlsbl %s`: %s", label, strings.Join(args, " "), problem))
			}
		}
	}
	return offenders
}

// goStringLiterals are the string literals of the non-test Go files, each
// labelled with its position.
func goStringLiterals(t *testing.T, root string) map[string]string {
	t.Helper()
	texts := map[string]string{}
	fset := token.NewFileSet()
	for _, rel := range testsupport.GoFiles(t, root) {
		if strings.HasSuffix(rel, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(root, filepath.FromSlash(rel)), nil, parser.SkipObjectResolution)
		if err != nil {
			t.Fatalf("parsing %s: %v", rel, err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			lit, ok := n.(*ast.BasicLit)
			if !ok || !isStringLiteral(lit) {
				return true
			}
			if text, err := strconv.Unquote(lit.Value); err == nil {
				texts[fmt.Sprintf("%s:%d", rel, fset.Position(lit.Pos()).Line)] = text
			}
			return true
		})
	}
	return texts
}

func TestEveryCommandLineATextNamesIsRegistered(t *testing.T) {
	hygiene.Isolate(t)
	root := testsupport.ModuleRoot(t)
	doc := helpDocument(t)
	help := map[string]string{}
	helpTexts(doc, "rlsbl", help)
	offenders := commandLineOffenders(doc, help, false)
	offenders = append(offenders, commandLineOffenders(doc, goStringLiterals(t, root), false)...)
	pages := map[string]string{}
	docs := filepath.Join(root, ".strictmetadata", "docs")
	entries, err := os.ReadDir(docs)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".md") {
			data, err := os.ReadFile(filepath.Join(docs, e.Name()))
			if err != nil {
				t.Fatal(err)
			}
			pages[".strictmetadata/docs/"+e.Name()] = string(data)
		}
	}
	offenders = append(offenders, commandLineOffenders(doc, pages, true)...)
	if len(offenders) > 0 {
		t.Fatalf("these texts name rlsbl command lines the application does not accept as written:\n  %s", strings.Join(offenders, "\n  "))
	}
}

func TestTheCommandLineGuardFindsAnUnknownCommandAndFlag(t *testing.T) {
	hygiene.Isolate(t)
	doc := helpDocument(t)
	texts := map[string]string{
		"a": "run `rlsbl release reconcile --apply`",
		"b": "run `rlsbl transition record --fact x`",
		"c": "run `rlsbl changelog edit --id <id> --no-user-facing --unset-type --dry-run`",
		"d": "run `rlsbl release %s` or `rlsbl watch %s`",
		"e": "with the Python rlsbl 0.131.0, `rlsbl release reconcile --apply`",
		"f": "```\nrlsbl monorepo add tools/cli --releasable false --frobnicate\n```",
	}
	got := commandLineOffenders(doc, texts, true)
	want := map[string]bool{
		"a: `rlsbl release reconcile --apply`: `rlsbl release reconcile` declares no flag --apply":                              true,
		"b: `rlsbl transition record --fact x`: `rlsbl transition` has no command \"record\"":                                   true,
		"f: `rlsbl monorepo add tools/cli --releasable false --frobnicate`: `rlsbl monorepo add` declares no flag --frobnicate": true,
	}
	if len(got) != len(want) {
		t.Fatalf("offenders = %q", got)
	}
	for _, o := range got {
		if !want[o] {
			t.Fatalf("unexpected offender %q among %q", o, got)
		}
	}
}
