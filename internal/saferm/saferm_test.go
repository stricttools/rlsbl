package saferm_test

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/saferm"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestArgsAlwaysCarryTheErrorModeAndTheDescription(t *testing.T) {
	hygiene.Isolate(t)
	args, err := saferm.Args(saferm.Request{Path: ".rlsbl/retry.toml", Description: "the retry file is spent"})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"delete", "--description", "the retry file is spent", "--on-error", "abort", "--", ".rlsbl/retry.toml"}
	if !slices.Equal(args, want) {
		t.Fatalf("args = %q", args)
	}
	args, err = saferm.Args(saferm.Request{Path: "old", Description: "residue", Recursive: true, SkipMissing: true})
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(args[:3], []string{"delete", "-r", "-f"}) {
		t.Fatalf("recursive and skip-missing do not come first: %q", args)
	}
	if _, err := saferm.Args(saferm.Request{Path: "x"}); err == nil {
		t.Fatal("a deletion without a description was accepted")
	}
	if _, err := saferm.Args(saferm.Request{Description: "why"}); err == nil {
		t.Fatal("a deletion without a path was accepted")
	}
}

func TestDeleteRunsSafermInTheDirectory(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.FakeSaferm(t)
	dir := t.TempDir()
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(e *strictcli.Effects) error {
		return saferm.Delete(e, dir, saferm.Request{Path: "stale.txt", Description: "stale"})
	})
	if got := fake.Calls(); !slices.Equal(got, []string{"delete --description stale --on-error abort -- stale.txt"}) {
		t.Fatalf("saferm calls = %q", got)
	}
}

func TestADryRunRecordsTheDeletion(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.FakeSaferm(t)
	dir := t.TempDir()
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: true}, func(ctx *strictcli.Context) error {
		return saferm.Delete(ctx.Effects(), dir, saferm.Request{Path: "stale.txt", Description: "stale"})
	})
	if res.ExitCode != 0 {
		t.Fatalf("exit %d: %s", res.ExitCode, res.Stderr)
	}
	if got := fake.Calls(); len(got) != 0 {
		t.Fatalf("a dry run ran saferm: %q", got)
	}
}

// The refusal names its fix (install saferm); installing it clears it.
func TestAMissingSafermIsRefusedUntilItIsInstalled(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.PathOnly(t, "git")
	dir := t.TempDir()
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(ctx *strictcli.Context) error {
		return saferm.Delete(ctx.Effects(), dir, saferm.Request{Path: "x", Description: "why"})
	})
	if res.ExitCode == 0 || !strings.Contains(res.Stderr, "saferm is not on PATH") {
		t.Fatalf("exit %d, stderr %q", res.ExitCode, res.Stderr)
	}
	testsupport.FakeSaferm(t)
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(e *strictcli.Effects) error {
		return saferm.Delete(e, dir, saferm.Request{Path: "x", Description: "why"})
	})
}

func TestDeleteRefusesARelativeDirectory(t *testing.T) {
	hygiene.Isolate(t)
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(ctx *strictcli.Context) error {
		return saferm.Delete(ctx.Effects(), "relative", saferm.Request{Path: "x", Description: "why"})
	})
	if res.ExitCode == 0 {
		t.Fatal("a relative directory was accepted")
	}
}

// Every saferm argv is built here: a file elsewhere holding both the
// program's name and its delete verb as literals is spelling one by hand.
func TestNoOtherPackageBuildsASafermArgv(t *testing.T) {
	hygiene.Isolate(t)
	root := testsupport.ModuleRoot(t)
	fset := token.NewFileSet()
	var offenders []string
	for _, rel := range testsupport.GoFiles(t, root) {
		if strings.HasPrefix(rel, "internal/saferm/") || strings.HasSuffix(rel, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(root, filepath.FromSlash(rel)), nil, parser.SkipObjectResolution)
		if err != nil {
			t.Fatal(err)
		}
		var program, verb bool
		ast.Inspect(f, func(n ast.Node) bool {
			if lit, ok := n.(*ast.BasicLit); ok {
				program = program || lit.Value == `"saferm"`
				verb = verb || lit.Value == `"delete"`
			}
			return true
		})
		if program && verb {
			offenders = append(offenders, rel)
		}
	}
	if len(offenders) > 0 {
		t.Fatalf("these files spell a saferm deletion by hand instead of calling saferm.Delete: %q", offenders)
	}
}
