package cli

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// dashedFlagKeys names every place a non-test file of internal/cli reads a
// handler's arguments under a dashed name: a call passing kw (or an elected
// value's Fields) and then a string literal holding a dash, or an index of
// kw by such a literal. strictcli delivers every flag under its underscored
// name, so a dashed read panics on a key that is never there.
func dashedFlagKeys(t *testing.T, root string, files []string) []string {
	t.Helper()
	fset := token.NewFileSet()
	var offenders []string
	isArguments := func(e ast.Expr) bool {
		switch x := e.(type) {
		case *ast.Ident:
			return x.Name == "kw"
		case *ast.SelectorExpr:
			return x.Sel.Name == "Fields"
		}
		return false
	}
	dashed := func(e ast.Expr) (string, bool) {
		lit, ok := e.(*ast.BasicLit)
		if !ok || lit.Kind != token.STRING {
			return "", false
		}
		s, err := strconv.Unquote(lit.Value)
		return s, err == nil && strings.Contains(s, "-")
	}
	for _, rel := range files {
		if !strings.HasPrefix(rel, "internal/cli/") || strings.HasSuffix(rel, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(root, filepath.FromSlash(rel)), nil, parser.SkipObjectResolution)
		if err != nil {
			t.Fatalf("parsing %s: %v", rel, err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			switch x := n.(type) {
			case *ast.CallExpr:
				for i, arg := range x.Args {
					if !isArguments(arg) {
						continue
					}
					for _, later := range x.Args[i+1:] {
						if s, ok := dashed(later); ok {
							offenders = append(offenders, fmt.Sprintf("%s:%d: %q", rel, fset.Position(later.Pos()).Line, s))
						}
					}
				}
			case *ast.IndexExpr:
				if s, ok := dashed(x.Index); ok && isArguments(x.X) {
					offenders = append(offenders, fmt.Sprintf("%s:%d: %q", rel, fset.Position(x.Index.Pos()).Line, s))
				}
			}
			return true
		})
	}
	return offenders
}

func TestNoHandlerReadsAFlagUnderItsDashedName(t *testing.T) {
	hygiene.Isolate(t)
	root := testsupport.ModuleRoot(t)
	if offenders := dashedFlagKeys(t, root, testsupport.GoFiles(t, root)); len(offenders) > 0 {
		t.Fatalf("strictcli delivers flags under their underscored names; read these with underscores:\n  %s", strings.Join(offenders, "\n  "))
	}
}

func TestTheFlagKeyGuardFindsADashedRead(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, "internal/cli/handler.go"), `package cli

func run(kw map[string]any, e elected) {
	_ = get(kw, "auto-commit")
	_ = get(kw, "auto_commit")
	_ = kw["run-id"]
	_ = get(e.Fields, "tag-format")
	_ = get(nil, "not-arguments")
}
`)
	got := dashedFlagKeys(t, root, []string{"internal/cli/handler.go"})
	want := `internal/cli/handler.go:4: "auto-commit"|internal/cli/handler.go:6: "run-id"|internal/cli/handler.go:7: "tag-format"`
	if strings.Join(got, "|") != want {
		t.Fatalf("offenders = %q", got)
	}
}
