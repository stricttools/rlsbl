package testsupport

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"unicode"
	"unicode/utf8"

	"github.com/stricttools/testisolation/go/hygiene"
)

// hygienePath is the import path of the isolation package.
const hygienePath = "github.com/stricttools/testisolation/go/hygiene"

// isolationOffenders names every Test function in the listed test files whose
// first statement is not hygiene.Isolate called on its own *testing.T.
// TestMain cannot bind the isolation (it has no *testing.T), so every test
// binds it itself, first, before anything can read the real environment.
func isolationOffenders(t *testing.T, root string, files []string) []string {
	t.Helper()
	fset := token.NewFileSet()
	var offenders []string
	for _, rel := range files {
		if !strings.HasSuffix(rel, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(root, filepath.FromSlash(rel)), nil, parser.SkipObjectResolution)
		if err != nil {
			t.Fatalf("parsing %s: %v", rel, err)
		}
		local := importedAs(f, hygienePath)
		for _, decl := range f.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Recv != nil {
				continue
			}
			param, ok := testFunctionParam(fn)
			if !ok {
				continue
			}
			if local == "" || param == "" || param == "_" || !firstStatementIsolates(fn.Body, local, param) {
				offenders = append(offenders, fmt.Sprintf("%s: %s", rel, fn.Name.Name))
			}
		}
	}
	return offenders
}

// importedAs is the name a file refers to the package at path by, or "" when
// the file does not import it under a usable name.
func importedAs(f *ast.File, path string) string {
	for _, spec := range f.Imports {
		p, err := strconv.Unquote(spec.Path.Value)
		if err != nil || p != path {
			continue
		}
		if spec.Name == nil {
			return filepath.Base(path)
		}
		if spec.Name.Name == "_" || spec.Name.Name == "." {
			return ""
		}
		return spec.Name.Name
	}
	return ""
}

// testFunctionParam reports whether fn is a test function go test runs
// (TestXxx taking one *testing.T, TestMain excluded) and names its parameter.
func testFunctionParam(fn *ast.FuncDecl) (string, bool) {
	name := fn.Name.Name
	if !strings.HasPrefix(name, "Test") || name == "TestMain" {
		return "", false
	}
	if rest := name[len("Test"):]; rest != "" {
		r, _ := utf8.DecodeRuneInString(rest)
		if unicode.IsLower(r) {
			return "", false
		}
	}
	params := fn.Type.Params.List
	if len(params) != 1 || len(params[0].Names) > 1 {
		return "", false
	}
	star, ok := params[0].Type.(*ast.StarExpr)
	if !ok {
		return "", false
	}
	sel, ok := star.X.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != "T" {
		return "", false
	}
	if pkg, ok := sel.X.(*ast.Ident); !ok || pkg.Name != "testing" {
		return "", false
	}
	if len(params[0].Names) == 0 {
		return "", true
	}
	return params[0].Names[0].Name, true
}

// firstStatementIsolates reports whether body begins with <local>.Isolate(<param>, ...).
func firstStatementIsolates(body *ast.BlockStmt, local, param string) bool {
	if body == nil || len(body.List) == 0 {
		return false
	}
	stmt, ok := body.List[0].(*ast.ExprStmt)
	if !ok {
		return false
	}
	call, ok := stmt.X.(*ast.CallExpr)
	if !ok || len(call.Args) == 0 {
		return false
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != "Isolate" {
		return false
	}
	if pkg, ok := sel.X.(*ast.Ident); !ok || pkg.Name != local {
		return false
	}
	arg, ok := call.Args[0].(*ast.Ident)
	return ok && arg.Name == param
}

func TestEveryTestBindsTheIsolationFirst(t *testing.T) {
	hygiene.Isolate(t)
	root := ModuleRoot(t)
	if offenders := isolationOffenders(t, root, GoFiles(t, root)); len(offenders) > 0 {
		t.Fatalf("these tests do not call hygiene.Isolate(t) as their first statement, so they can reach the real HOME, git identity, credentials, or a network transport:\n  %s", strings.Join(offenders, "\n  "))
	}
}

// The guard refuses a test that binds the isolation late, on another value,
// or not at all, and accepts one that binds it first.
func TestTheIsolationGuardRefusesAnUnisolatedTest(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	WriteFile(t, filepath.Join(root, "a_test.go"), `package a

import (
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestBound(t *testing.T) {
	hygiene.Isolate(t)
}

func TestLate(t *testing.T) {
	t.Log("before")
	hygiene.Isolate(t)
}

func TestNever(t *testing.T) {}

func TestOtherValue(t *testing.T) {
	var u testing.TB
	hygiene.Isolate(u)
}

func TestMain(m *testing.M) {}

func helper(t *testing.T) {}

func Testable(t *testing.T) {}
`)
	WriteFile(t, filepath.Join(root, "b_test.go"), `package a

import "testing"

func TestWithoutTheImport(t *testing.T) {
	hygiene.Isolate(t)
}
`)
	got := isolationOffenders(t, root, []string{"a_test.go", "b_test.go"})
	want := []string{"a_test.go: TestLate", "a_test.go: TestNever", "a_test.go: TestOtherValue", "b_test.go: TestWithoutTheImport"}
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("offenders = %q, want %q", got, want)
	}
}
