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
// handler's arguments under a dashed name: a call passing an argument map
// (or an elected value's Fields) and then a string literal holding a dash,
// or an index of an argument map by such a literal. An argument map is
// recognized by its type, not its name: any parameter of the enclosing
// functions declared map[string]any or map[string]interface{}. strictcli
// delivers every flag under its underscored name, so a dashed read panics on
// a key that is never there.
func dashedFlagKeys(t *testing.T, root string, files []string) []string {
	t.Helper()
	fset := token.NewFileSet()
	var offenders []string
	dashed := func(e ast.Expr) (string, bool) {
		lit, ok := e.(*ast.BasicLit)
		if !ok || !isStringLiteral(lit) {
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
		report := func(e ast.Expr, s string) {
			offenders = append(offenders, fmt.Sprintf("%s:%d: %q", rel, fset.Position(e.Pos()).Line, s))
		}
		var walk func(n ast.Node, maps map[string]bool)
		walk = func(n ast.Node, maps map[string]bool) {
			isArguments := func(e ast.Expr) bool {
				switch x := e.(type) {
				case *ast.Ident:
					return maps[x.Name]
				case *ast.SelectorExpr:
					return x.Sel.Name == "Fields"
				}
				return false
			}
			ast.Inspect(n, func(n ast.Node) bool {
				switch x := n.(type) {
				case *ast.FuncDecl:
					if x.Body != nil {
						walk(x.Body, withArgumentMaps(maps, x.Type))
					}
					return false
				case *ast.FuncLit:
					walk(x.Body, withArgumentMaps(maps, x.Type))
					return false
				case *ast.CallExpr:
					for i, arg := range x.Args {
						if !isArguments(arg) {
							continue
						}
						for _, later := range x.Args[i+1:] {
							if s, ok := dashed(later); ok {
								report(later, s)
							}
						}
					}
				case *ast.IndexExpr:
					if s, ok := dashed(x.Index); ok && isArguments(x.X) {
						report(x.Index, s)
					}
				}
				return true
			})
		}
		walk(f, map[string]bool{})
	}
	return offenders
}

// withArgumentMaps is maps with the parameters of fn whose type is
// map[string]any or map[string]interface{} added.
func withArgumentMaps(maps map[string]bool, fn *ast.FuncType) map[string]bool {
	out := map[string]bool{}
	for name := range maps {
		out[name] = true
	}
	if fn.Params == nil {
		return out
	}
	for _, field := range fn.Params.List {
		m, ok := field.Type.(*ast.MapType)
		if !ok {
			continue
		}
		key, ok := m.Key.(*ast.Ident)
		if !ok || key.Name != "string" {
			continue
		}
		switch v := m.Value.(type) {
		case *ast.Ident:
			if v.Name != "any" {
				continue
			}
		case *ast.InterfaceType:
			if v.Methods != nil && len(v.Methods.List) > 0 {
				continue
			}
		default:
			continue
		}
		for _, name := range field.Names {
			out[name.Name] = true
		}
	}
	return out
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

func other(args map[string]interface{}, labels map[string]string) {
	_ = get(args, "dry-run")
	_ = labels["not-arguments"]
	func(inner map[string]any) {
		_ = inner["tag-format"]
		_ = args["run-id"]
	}(nil)
}

func unrelated(kw []string) {
	_ = get(kw, "not-arguments")
}
`)
	got := dashedFlagKeys(t, root, []string{"internal/cli/handler.go"})
	want := `internal/cli/handler.go:4: "auto-commit"|internal/cli/handler.go:6: "run-id"|internal/cli/handler.go:7: "tag-format"|` +
		`internal/cli/handler.go:12: "dry-run"|internal/cli/handler.go:15: "tag-format"|internal/cli/handler.go:16: "run-id"`
	if strings.Join(got, "|") != want {
		t.Fatalf("offenders = %q", got)
	}
}

// isStringLiteral is whether lit is an interpreted or raw string literal.
func isStringLiteral(lit *ast.BasicLit) bool {
	return strings.HasPrefix(lit.Value, `"`) || strings.HasPrefix(lit.Value, "`")
}
