package testsupport

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// bannedWord is the word no Go file of rlsbl may carry, assembled so this
// file does not carry it either. Name the thing instead: a type, a class, or
// the word that says what it is.
var bannedWord = "k" + "ind"

// bannedWordFindings names every place a listed Go file carries the banned
// word, case-insensitively: its file name, an identifier, a comment, or a
// literal.
func bannedWordFindings(t *testing.T, root string, files []string) []string {
	t.Helper()
	has := func(s string) bool { return strings.Contains(strings.ToLower(s), bannedWord) }
	fset := token.NewFileSet()
	var findings []string
	for _, rel := range files {
		if has(rel) {
			findings = append(findings, fmt.Sprintf("%s: the file name", rel))
		}
		f, err := parser.ParseFile(fset, filepath.Join(root, filepath.FromSlash(rel)), nil, parser.ParseComments|parser.SkipObjectResolution)
		if err != nil {
			t.Fatalf("parsing %s: %v", rel, err)
		}
		at := func(pos token.Pos) string {
			return fmt.Sprintf("%s:%d", rel, fset.Position(pos).Line)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.Ident:
				if has(n.Name) {
					findings = append(findings, fmt.Sprintf("%s: identifier %s", at(n.Pos()), n.Name))
				}
			case *ast.BasicLit:
				if has(n.Value) {
					findings = append(findings, fmt.Sprintf("%s: literal %s", at(n.Pos()), n.Value))
				}
			}
			return true
		})
		for _, group := range f.Comments {
			for _, c := range group.List {
				if has(c.Text) {
					findings = append(findings, fmt.Sprintf("%s: comment", at(c.Pos())))
				}
			}
		}
	}
	return findings
}

func TestNoGoFileCarriesTheBannedWord(t *testing.T) {
	hygiene.Isolate(t)
	root := ModuleRoot(t)
	if findings := bannedWordFindings(t, root, GoFiles(t, root)); len(findings) > 0 {
		t.Fatalf("the word %q is banned from rlsbl's Go files (name the thing: a type, a class, or the word that says what it is):\n  %s", bannedWord, strings.Join(findings, "\n  "))
	}
}

func TestTheBannedWordGuardFindsEveryPlace(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	word := strings.ToUpper(bannedWord[:1]) + bannedWord[1:]
	WriteFile(t, filepath.Join(root, "clean.go"), "package a\n\n// Type names what it is.\nvar Type = \"plain\"\n")
	WriteFile(t, filepath.Join(root, "dirty.go"), "package a\n\n// The "+bannedWord+" of thing.\nvar "+word+" = \"a "+bannedWord+"\"\n")
	WriteFile(t, filepath.Join(root, bannedWord+".go"), "package a\n")
	got := bannedWordFindings(t, root, []string{"clean.go", "dirty.go", bannedWord + ".go"})
	want := []string{
		"dirty.go:4: identifier " + word,
		"dirty.go:4: literal \"a " + bannedWord + "\"",
		"dirty.go:3: comment",
		bannedWord + ".go: the file name",
	}
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("findings = %q, want %q", got, want)
	}
}
