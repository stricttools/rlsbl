package gomodule

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// moduleDir is a directory holding a go.mod with content.
func moduleDir(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(dir, FileName), content)
	return dir
}

func TestTheContainmentRuleHoldsAtTheSeparatorBoundary(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		path, prefix, sep string
		want              bool
	}{
		{"github.com/o/foo", "github.com/o/foo", GoSeparator, true},
		{"github.com/o/foo/bar", "github.com/o/foo", GoSeparator, true},
		{"github.com/o/foobar", "github.com/o/foo", GoSeparator, false},
		{"github.com/stricttools/strictcli-extras/x", "github.com/stricttools/strictcli", GoSeparator, false},
		{"a.b.c", "a.b", DotSeparator, true},
		{"a.bc", "a.b", DotSeparator, false},
		{"anything", "", GoSeparator, false},
	}
	for _, c := range cases {
		if got := UnderModulePrefix(c.path, c.prefix, c.sep); got != c.want {
			t.Errorf("UnderModulePrefix(%q, %q, %q) = %v, want %v", c.path, c.prefix, c.sep, got, c.want)
		}
	}
	if got := RewriteModulePrefix("example.com/old/pkg", "example.com/old", "example.com/new", GoSeparator); got != "example.com/new/pkg" {
		t.Errorf("rewrite = %q", got)
	}
	if got := RewriteModulePrefix("example.com/oldish/pkg", "example.com/old", "example.com/new", GoSeparator); got != "example.com/oldish/pkg" {
		t.Errorf("a neighboring module was rewritten: %q", got)
	}
}

func TestTheLongestContainingModuleOwnsAnImport(t *testing.T) {
	hygiene.Isolate(t)
	modules := []string{"m/draw", "m/draw/cmd", "m"}
	if got, ok := OwningModule("m/draw/cmd/sub", modules, GoSeparator); !ok || got != "m/draw/cmd" {
		t.Errorf("owner = %q, %v", got, ok)
	}
	if _, ok := OwningModule("other/x", modules, GoSeparator); ok {
		t.Error("an import outside every module has an owner")
	}
}

func TestTheModulePathIsReadAndAMissingDirectiveIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	path, found, err := ModulePath(moduleDir(t, "module example.com/portal\n\ngo 1.21\n"))
	if err != nil || !found || path != "example.com/portal" {
		t.Fatalf("ModulePath = %q, %v, %v", path, found, err)
	}
	if _, found, err := ModulePath(t.TempDir()); err != nil || found {
		t.Fatalf("a directory without go.mod: found %v, err %v", found, err)
	}
	if _, _, err := ModulePath(moduleDir(t, "go 1.21\n")); err == nil || !strings.Contains(err.Error(), "declares no module path") {
		t.Fatalf("a go.mod without a module directive was not refused: %v", err)
	}
	if _, _, err := ModulePath(moduleDir(t, "module example.com/portal\nnonsense here\n")); err == nil {
		t.Fatal("a go.mod the go command refuses was read")
	}
	if got := LastElement("github.com/owner/widget"); got != "widget" {
		t.Errorf("LastElement = %q", got)
	}
}

func TestDirectivesKeepTheirLinesAndLocalReplacementsAreRecognized(t *testing.T) {
	hygiene.Isolate(t)
	text := "module example.com/portal\n\ngo 1.21\n\nrequire example.com/widget v0.2.0\n\nrequire (\n\texample.com/gadget v1.0.0 // indirect\n)\n\nreplace example.com/widget => ../widget\n\nreplace example.com/gadget => example.com/fork v1.0.1\n"
	f, err := Parse("go.mod", []byte(text))
	if err != nil {
		t.Fatal(err)
	}
	requires, replaces := Directives(f)
	if len(requires) != 2 || requires[0] != (Require{Path: "example.com/widget", Version: "v0.2.0", Line: 5}) || requires[1].Line != 8 {
		t.Fatalf("requires = %+v", requires)
	}
	if len(replaces) != 2 || !replaces[0].IsLocal() || replaces[1].IsLocal() || replaces[0].Line != 11 || replaces[1].NewVersion != "v1.0.1" {
		t.Fatalf("replaces = %+v", replaces)
	}
}

func TestAModuleWithoutAToolchainLineIsNamedAndTheNamedFixClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, "go.mod"), "module example.com/portal\n\ngo 1.21\n")
	testsupport.WriteFile(t, filepath.Join(root, "widget", "go.mod"), "module example.com/portal/widget\n\ngo 1.21\n\ntoolchain go1.26.6\n")
	testsupport.WriteFile(t, filepath.Join(root, "gadget", "go.mod"), "module example.com/portal/gadget\n\ngo 1.21\n// toolchain go1.26.6\n")
	dirs := []string{root, filepath.Join(root, "widget"), filepath.Join(root, "gadget"), filepath.Join(root, "nothing")}
	problems, err := ToolchainProblems(root, dirs)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 2 || !strings.HasPrefix(problems[0], "go.mod declares no toolchain line") || !strings.HasPrefix(problems[1], "gadget/go.mod") || !strings.Contains(problems[0], ToolchainFix) {
		t.Fatalf("problems = %q", problems)
	}
	// The fix `go mod edit -toolchain=go1.26.6` writes this line.
	for _, dir := range []string{root, filepath.Join(root, "gadget")} {
		data, err := os.ReadFile(filepath.Join(dir, "go.mod"))
		if err != nil {
			t.Fatal(err)
		}
		testsupport.WriteFile(t, filepath.Join(dir, "go.mod"), string(data)+"\ntoolchain go1.26.6\n")
	}
	if problems, err := ToolchainProblems(root, dirs); err != nil || len(problems) != 0 {
		t.Fatalf("after the fix: %q, %v", problems, err)
	}
}

func TestARetractionIsAppendedKeepingTheFileAndNotRepeated(t *testing.T) {
	hygiene.Isolate(t)
	before := "module example.com/portal\n\ngo 1.21 // the oldest Go\n\n"
	out, changed, err := AddRetraction("go.mod", []byte(before), "v1.2.3")
	if err != nil || !changed {
		t.Fatalf("changed %v, err %v", changed, err)
	}
	if want := "module example.com/portal\n\ngo 1.21 // the oldest Go\n\nretract v1.2.3\n"; string(out) != want {
		t.Fatalf("got:\n%s", out)
	}
	if _, changed, err := AddRetraction("go.mod", out, "v1.2.3"); err != nil || changed {
		t.Fatalf("a second retraction: changed %v, err %v", changed, err)
	}
	ranged := "module example.com/portal\n\nretract [v1.0.0, v1.5.0]\n"
	if _, changed, err := AddRetraction("go.mod", []byte(ranged), "v1.2.3"); err != nil || changed {
		t.Fatalf("a version inside a retracted range: changed %v, err %v", changed, err)
	}
	if _, _, err := AddRetraction("go.mod", []byte(before), "1.2.3"); err == nil {
		t.Fatal("a version without its v was accepted")
	}
}

func TestGoWorkUsesAreRead(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	if _, found, err := WorkUses(root); err != nil || found {
		t.Fatalf("no go.work: found %v, err %v", found, err)
	}
	testsupport.WriteFile(t, filepath.Join(root, "go.work"), "go 1.21\n\nuse (\n\t.\n\t./widget\n)\n")
	uses, found, err := WorkUses(root)
	if err != nil || !found || !slices.Equal(uses, []string{".", "./widget"}) {
		t.Fatalf("uses = %q, %v, %v", uses, found, err)
	}
}
