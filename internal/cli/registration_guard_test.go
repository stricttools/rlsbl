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

const strictcliPath = "github.com/stricttools/strictcli/go/strictcli"

// registeringMethods are the strictcli methods that add to an app's command
// surface.
var registeringMethods = map[string]bool{
	"Command": true, "Group": true, "Passthrough": true, "Deprecated": true, "GlobalFlag": true,
}

// registrationOutsideCLI names every call of a registering method in a
// non-test file that imports strictcli and lives outside internal/cli. The
// test harness is exempt: its throwaway application, which runs one handler
// under test, is not rlsbl's.
func registrationOutsideCLI(t *testing.T, root string, files []string) []string {
	t.Helper()
	fset := token.NewFileSet()
	var offenders []string
	for _, rel := range files {
		if strings.HasSuffix(rel, "_test.go") || strings.HasPrefix(rel, "internal/cli/") || strings.HasPrefix(rel, "internal/testsupport/") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(root, filepath.FromSlash(rel)), nil, parser.SkipObjectResolution)
		if err != nil {
			t.Fatalf("parsing %s: %v", rel, err)
		}
		imports := false
		for _, spec := range f.Imports {
			if p, err := strconv.Unquote(spec.Path.Value); err == nil && p == strictcliPath {
				imports = true
			}
		}
		if !imports {
			continue
		}
		ast.Inspect(f, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			if sel, ok := call.Fun.(*ast.SelectorExpr); ok && registeringMethods[sel.Sel.Name] {
				offenders = append(offenders, fmt.Sprintf("%s:%d: %s", rel, fset.Position(call.Pos()).Line, sel.Sel.Name))
			}
			return true
		})
	}
	return offenders
}

func TestNoFileOutsideTheCLIPackageRegistersACommand(t *testing.T) {
	hygiene.Isolate(t)
	root := testsupport.ModuleRoot(t)
	if offenders := registrationOutsideCLI(t, root, testsupport.GoFiles(t, root)); len(offenders) > 0 {
		t.Fatalf("commands are registered only in internal/cli, one file per group:\n  %s", strings.Join(offenders, "\n  "))
	}
}

func TestTheRegistrationGuardFindsARegistration(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, "internal/release/register.go"), `package release

import "`+strictcliPath+`"

func Register(app *strictcli.App) {
	app.Command("release", "Release", nil)
}
`)
	testsupport.WriteFile(t, filepath.Join(root, "internal/release/other.go"), `package release

type tree struct{}

func (tree) Group() {}

func use() { tree{}.Group() }
`)
	got := registrationOutsideCLI(t, root, []string{"internal/release/register.go", "internal/release/other.go"})
	if strings.Join(got, "|") != "internal/release/register.go:6: Command" {
		t.Fatalf("offenders = %q", got)
	}
}
