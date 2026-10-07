package rewrite_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/rewrite"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// run runs fn as a mutating command, under --dry-run when dryRun is set.
func run(t *testing.T, dryRun bool, fn func(ctx *strictcli.Context) error) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, fn)
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func requireContains(t *testing.T, text string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in:\n%s", want, text)
		}
	}
}

const schemaDump = "{\n  \"commands\": [\n    {\n      \"project_id\": \"github.com/o/foo\"\n    }\n  ],\n  \"project_id\": \"github.com/o/foo\",\n  \"version\": \"0.1.0\"\n}\n"

// moduleRepo is a repository owning github.com/o/foo, with a neighbor
// sharing its prefix, a nested module, a vendored copy, a scratch
// directory, and an ignored third-party clone.
func moduleRepo(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(".gitignore", "third_party/\n")
	repo.Write("go.mod", "module github.com/o/foo // the module\n\ngo 1.26\n\nrequire github.com/o/foobar v1.0.0\n\nreplace github.com/o/foo/v2 => ../foo-v2\n")
	repo.Write("main.go", "package main\n\nimport (\n\t\"fmt\"\n\n\tx \"github.com/o/foo/internal/x\"\n\t\"github.com/o/foobar/y\"\n\t`github.com/o/foo/z`\n)\n\n// github.com/o/foo is the module.\nvar path = \"github.com/o/foo\"\n\nfunc main() { fmt.Println(x.X, y.Y, path) }\n")
	repo.Write("internal/assets/assets.go", "package assets\n\nimport _ \"github.com/o/foo/internal/x\"\n")
	repo.Write("cmd/go.mod", "module github.com/o/foo/cmd\n\ngo 1.26\n\nrequire github.com/o/foo v0.1.0\n")
	repo.Write("cmd/main.go", "package main\n\nimport (\n\t_ \"github.com/o/foo/cmd/internal\"\n\t_ \"github.com/o/foo/internal/x\"\n)\n")
	repo.Write("vendor/github.com/o/foo/v.go", "package foo\n\nimport _ \"github.com/o/foo/internal/x\"\n")
	repo.Write("experiments/probe.go", "package probe\n\nimport _ \"github.com/o/foo/internal/x\"\n")
	repo.Write("third_party/clone.go", "package clone\n\nimport _ \"github.com/o/foo/internal/x\"\n")
	repo.Write("tools/nested/vendor.go", "package nested\n\nimport _ \"github.com/o/foo/internal/x\"\n")
	repo.Write(".strictmetadata/.cli-schema/schema.json", schemaDump)
	repo.Write("neighbor/.strictmetadata/.cli-schema/schema.json", "{\n  \"project_id\": \"github.com/o/foobar\"\n}\n")
	return repo
}

func rename(t *testing.T, root string, dryRun bool, from, to string) (strictcli.Result, error) {
	t.Helper()
	var runErr error
	res := run(t, dryRun, func(ctx *strictcli.Context) error {
		runErr = rewrite.RunGoModulePath(ctx, root, from, to)
		return runErr
	})
	return res, runErr
}

func TestTheDryRunPlansEveryFileAndWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo := moduleRepo(t)
	before := read(t, repo.Path("main.go"))
	res, err := rename(t, repo.Dir, true, "github.com/o/foo", "github.com/n/bar")
	if err != nil {
		t.Fatalf("%v\n%s", err, res.Stderr)
	}
	requireContains(t, res.Stdout,
		"go.mod: rewrite: 2 occurrences in this go.mod",
		"line 1: github.com/o/foo -> github.com/n/bar",
		"line 7: github.com/o/foo/v2 -> github.com/n/bar/v2",
		"cmd/go.mod: rewrite: 1 occurrence in this go.mod",
		"main.go: rewrite: 2 occurrences in this go source",
		"internal/assets/assets.go: rewrite: 1 occurrence",
		"tools/nested/vendor.go: rewrite: 1 occurrence",
		".strictmetadata/.cli-schema/schema.json: rewrite: 1 occurrence in this strictcli schema dump",
		"left alone, as modules of their own: github.com/o/foo/cmd",
		"(total): summary: 9 occurrences across 7 files",
	)
	for _, absent := range []string{"vendor/", "experiments/", "third_party/", "neighbor/"} {
		if strings.Contains(res.Stdout, absent) {
			t.Errorf("the plan names %s:\n%s", absent, res.Stdout)
		}
	}
	if read(t, repo.Path("main.go")) != before {
		t.Error("the dry run wrote")
	}
}

func TestTheApplyRewritesOnlyWhatThePlanNamed(t *testing.T) {
	hygiene.Isolate(t)
	repo := moduleRepo(t)
	res, err := rename(t, repo.Dir, false, "github.com/o/foo", "github.com/n/bar")
	if err != nil {
		t.Fatalf("%v\n%s", err, res.Stderr)
	}
	requireContains(t, res.Stdout, "Renamed github.com/o/foo -> github.com/n/bar across 7 files.")
	main := read(t, repo.Path("main.go"))
	requireContains(t, main, `x "github.com/n/bar/internal/x"`, "`github.com/n/bar/z`", `"github.com/o/foobar/y"`, "// github.com/o/foo is the module.", `var path = "github.com/o/foo"`)
	requireContains(t, read(t, repo.Path("go.mod")), "module github.com/n/bar // the module", "require github.com/o/foobar v1.0.0", "replace github.com/n/bar/v2 => ../foo-v2")
	cmdMod := read(t, repo.Path("cmd/go.mod"))
	requireContains(t, cmdMod, "module github.com/o/foo/cmd", "require github.com/n/bar v0.1.0")
	requireContains(t, read(t, repo.Path("cmd/main.go")), `"github.com/o/foo/cmd/internal"`, `"github.com/n/bar/internal/x"`)
	dump := read(t, repo.Path(".strictmetadata/.cli-schema/schema.json"))
	if dump != strings.Replace(schemaDump, `  "project_id": "github.com/o/foo",`, `  "project_id": "github.com/n/bar",`, 1) {
		t.Errorf("the dump changed beyond its project_id:\n%s", dump)
	}
	for _, untouched := range []string{"vendor/github.com/o/foo/v.go", "experiments/probe.go", "third_party/clone.go", "neighbor/.strictmetadata/.cli-schema/schema.json"} {
		if strings.Contains(read(t, repo.Path(untouched)), "github.com/n/bar") {
			t.Errorf("%s was rewritten", untouched)
		}
	}
	// A second run finds nothing to rename and says so, naming the paths
	// declared.
	_, err = rename(t, repo.Dir, false, "github.com/o/foo", "github.com/n/bar")
	if err == nil || !strings.Contains(err.Error(), "nothing references 'github.com/o/foo'") || !strings.Contains(err.Error(), "github.com/n/bar, github.com/o/foo/cmd") {
		t.Fatalf("%v", err)
	}
}

func TestTheNestedModuleIsRenamedByItsOwnInvocation(t *testing.T) {
	hygiene.Isolate(t)
	repo := moduleRepo(t)
	if _, err := rename(t, repo.Dir, false, "github.com/o/foo/cmd", "github.com/o/foocmd"); err != nil {
		t.Fatal(err)
	}
	requireContains(t, read(t, repo.Path("cmd/go.mod")), "module github.com/o/foocmd", "require github.com/o/foo v0.1.0")
	requireContains(t, read(t, repo.Path("cmd/main.go")), `"github.com/o/foocmd/internal"`, `"github.com/o/foo/internal/x"`)
}

func TestAConsumerRewritesReferencesWithoutOwningTheModule(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write("go.mod", "module example.com/consumer\n\ngo 1.26\n\nrequire github.com/o/foo v1.0.0\n")
	repo.Write("a.go", "package a\n\nimport _ \"github.com/o/foo/pkg\"\n")
	res, err := rename(t, repo.Dir, true, "github.com/o/foo", "github.com/n/bar")
	if err != nil {
		t.Fatal(err)
	}
	requireContains(t, res.Stdout, "this repository consumes the module rather than owning it")
}

func TestModulePathsAreValidated(t *testing.T) {
	hygiene.Isolate(t)
	if err := rewrite.ValidateModulePaths("github.com/o/foo", "github.com/o/foo"); err == nil || !strings.Contains(err.Error(), "the same path") {
		t.Errorf("%v", err)
	}
	if err := rewrite.ValidateModulePaths("github.com/o/foo", "github.com/o/ bar"); err == nil || !strings.Contains(err.Error(), "--to-module must not contain whitespace") {
		t.Errorf("%v", err)
	}
	repo := moduleRepo(t)
	_, err := rename(t, repo.Dir, true, "github.com/o/fooo", "github.com/n/bar")
	if err == nil || !strings.Contains(err.Error(), "Check --from-module for a typo") {
		t.Errorf("%v", err)
	}
}

func TestAFileThatDoesNotParseIsNamed(t *testing.T) {
	hygiene.Isolate(t)
	repo := moduleRepo(t)
	repo.Write("broken/broken.go", "package broken\n\nimport (\n\t\"github.com/o/foo/internal/x\"\n")
	_, err := rename(t, repo.Dir, true, "github.com/o/foo", "github.com/n/bar")
	if err == nil || !strings.Contains(err.Error(), "broken/broken.go does not parse as Go source") {
		t.Fatalf("%v", err)
	}
}

// observe plans a rename of repo without writing anything.
func observe(t *testing.T, m *rewrite.ModuleRename) []rewrite.ModuleFile {
	t.Helper()
	var files []rewrite.ModuleFile
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		var err error
		files, _, err = m.ObserveModuleFiles(e)
		return err
	})
	return files
}

func TestACountThatMovedBetweenThePreviewAndTheApplyRefuses(t *testing.T) {
	hygiene.Isolate(t)
	repo := moduleRepo(t)
	m := &rewrite.ModuleRename{Root: repo.Dir, From: "github.com/o/foo", To: "github.com/n/bar"}
	files := observe(t, m)
	var mainFile rewrite.ModuleFile
	for _, f := range files {
		if f.Rel == "main.go" {
			mainFile = f
		}
	}
	repo.Write("main.go", strings.Replace(read(t, repo.Path("main.go")), "\"fmt\"\n", "\"fmt\"\n\t_ \"github.com/o/foo/extra\"\n", 1))
	res := run(t, false, func(ctx *strictcli.Context) error { return m.ApplyModuleFile(ctx.Effects(), mainFile) })
	if res.ExitCode != 1 || !strings.Contains(res.Stderr, "the preview counted 2 occurrence(s) in main.go but it now has 3") {
		t.Fatalf("exit %d: %s", res.ExitCode, res.Stderr)
	}
	if strings.Contains(read(t, repo.Path("main.go")), "github.com/n/bar") {
		t.Error("the moved file was written")
	}
	if err := os.Remove(repo.Path("main.go")); err != nil {
		t.Fatal(err)
	}
	res = run(t, false, func(ctx *strictcli.Context) error { return m.ApplyModuleFile(ctx.Effects(), mainFile) })
	if res.ExitCode != 1 || !strings.Contains(res.Stderr, "main.go, which the plan named, could not be read at apply time") {
		t.Fatalf("exit %d: %s", res.ExitCode, res.Stderr)
	}
}

func TestTheModeOfARewrittenFileIsKept(t *testing.T) {
	hygiene.Isolate(t)
	repo := moduleRepo(t)
	if err := os.Chmod(repo.Path("main.go"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := rename(t, repo.Dir, false, "github.com/o/foo", "github.com/n/bar"); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(filepath.Join(repo.Dir, "main.go"))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Errorf("%v, %v", info.Mode(), err)
	}
}
