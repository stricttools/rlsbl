package gomodule

import (
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

const ldflagsGoMod = "module example.com/portal\n\ngo 1.21\n"

// file is one module-relative file of a fixture module.
type file struct{ rel, content string }

// ldflagsModule writes files into a fresh module directory (with a go.mod
// unless one is given), every one of them tracked.
func ldflagsModule(t *testing.T, files ...file) LdflagsModule {
	t.Helper()
	dir := t.TempDir()
	rels := []string{}
	hasGoMod := false
	for _, f := range files {
		testsupport.WriteFile(t, filepath.Join(dir, filepath.FromSlash(f.rel)), f.content)
		rels = append(rels, f.rel)
		hasGoMod = hasGoMod || f.rel == "go.mod"
	}
	if !hasGoMod {
		testsupport.WriteFile(t, filepath.Join(dir, "go.mod"), ldflagsGoMod)
		rels = append(rels, "go.mod")
	}
	sort.Strings(rels)
	return LdflagsModule{Dir: dir, Tracked: rels, Listed: rels}
}

func evaluate(t *testing.T, m LdflagsModule) LdflagsVerdict {
	t.Helper()
	v, err := EvaluateLdflags([]LdflagsModule{m})
	if err != nil {
		t.Fatal(err)
	}
	return v
}

const goreleaserVersion = "builds:\n  - main: .\n    ldflags:\n      - -s -w -X main.Version={{.Version}}\n"

func goreleaser(content string) file { return file{".goreleaser.yml", content} }

func mainGo(content string) file { return file{"main.go", content} }

func TestAFlagNamingAMissingSymbolIsAProblemAndEitherNamedFixClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	v := evaluate(t, ldflagsModule(t, goreleaser(goreleaserVersion), mainGo("package main\n\nvar version = \"dev\"\n\nfunc main() { println(version) }\n")))
	if len(v.Problems) != 1 || !strings.Contains(v.Problems[0], "declares no package-level `Version`") || !strings.Contains(v.Problems[0], "`version`") || !strings.Contains(v.Problems[0], "SILENTLY") {
		t.Fatalf("problems = %q", v.Problems)
	}
	// Renaming the Go variable clears it.
	v = evaluate(t, ldflagsModule(t, goreleaser(goreleaserVersion), mainGo("package main\n\nvar Version = \"dev\"\n\nfunc main() { println(Version) }\n")))
	if !v.OK() || v.Verified != 1 || len(v.Warnings) != 0 {
		t.Fatalf("after renaming the variable: %+v", v)
	}
	// So does changing the -X target.
	v = evaluate(t, ldflagsModule(t, goreleaser(strings.Replace(goreleaserVersion, "main.Version", "main.version", 1)), mainGo("package main\n\nvar version = \"dev\"\n\nfunc main() { println(version) }\n")))
	if !v.OK() || v.Verified != 1 {
		t.Fatalf("after changing the target: %+v", v)
	}
}

func TestWhatTheLinkerCannotSetIsAProblem(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ decl, want string }{
		{"const Version = \"dev\"", "a const"},
		{"var Version int", "a var of type `int`"},
		{"var Version = fmt.Sprint(1)", "a non-constant expression"},
		{"func Version() string { return \"\" }", "a function"},
		{"type Version string", "a type"},
	}
	for _, c := range cases {
		src := "package main\n\nimport \"fmt\"\n\nvar _ = fmt.Sprint\n\n" + c.decl + "\n\nfunc main() {}\n"
		v := evaluate(t, ldflagsModule(t, goreleaser(goreleaserVersion), mainGo(src)))
		if len(v.Problems) != 1 || !strings.Contains(v.Problems[0], c.want) {
			t.Errorf("%s: problems = %q", c.decl, v.Problems)
		}
	}
}

func TestEverySettableStringVarPasses(t *testing.T) {
	hygiene.Isolate(t)
	for _, decl := range []string{
		"var Version string",
		"var Version string = \"dev\"",
		"var Version = `dev`",
		"var (\n\tName    = \"portal\"\n\tVersion = \"dev\"\n)",
	} {
		src := "package main\n\n" + decl + "\n\nfunc main() { println(Version) }\n"
		if v := evaluate(t, ldflagsModule(t, goreleaser(goreleaserVersion), mainGo(src))); !v.OK() || v.Verified != 1 {
			t.Errorf("%s: %+v", decl, v)
		}
	}
}

func TestALocalVariableDoesNotSatisfyTheFlag(t *testing.T) {
	hygiene.Isolate(t)
	src := "package main\n\nfunc main() {\n\tVersion := \"dev\"\n\tprintln(Version)\n}\n"
	if v := evaluate(t, ldflagsModule(t, goreleaser(goreleaserVersion), mainGo(src))); v.OK() {
		t.Fatalf("a local variable satisfied the flag: %+v", v)
	}
}

func TestAnUnreadSymbolWarnsAndAReaderAnywhereClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	build := file{"build.sh", "go build -ldflags \"-X example.com/portal/internal/version.Value=$V\" ./cmd/portal\n"}
	version := file{"internal/version/version.go", "package version\n\nvar Value string\n"}
	v := evaluate(t, ldflagsModule(t, build, version, file{"cmd/portal/main.go", "package main\n\nfunc main() {}\n"}))
	if !v.OK() || len(v.Warnings) != 1 || !strings.Contains(v.Warnings[0], "nothing in this module reads it") {
		t.Fatalf("%+v", v)
	}
	reader := file{"cmd/portal/main.go", "package main\n\nimport \"example.com/portal/internal/version\"\n\nfunc main() { println(version.Value) }\n"}
	if v := evaluate(t, ldflagsModule(t, build, version, reader)); !v.OK() || len(v.Warnings) != 0 {
		t.Fatalf("a reader in another package did not count: %+v", v)
	}
}

func TestABareMainResolvesThroughTheBuildCommand(t *testing.T) {
	hygiene.Isolate(t)
	portal := file{"cmd/portal/main.go", "package main\n\nvar Version string\n\nfunc main() { println(Version) }\n"}
	makefile := file{"Makefile", "VERSION ?= dev\nLDFLAGS = -X main.Version=$(VERSION)\n\nbuild:\n\tgo build -ldflags \"$(LDFLAGS)\" ./cmd/portal\n"}
	if v := evaluate(t, ldflagsModule(t, makefile, portal, file{"cmd/other/main.go", "package main\n\nfunc main() {}\n"})); !v.OK() || v.Verified != 1 {
		t.Fatalf("%+v", v)
	}
	yaml := file{".goreleaser.yaml", "builds:\n  - main: ./cmd/portal/main.go\n    ldflags: -X main.Version={{.Version}}\n"}
	if v := evaluate(t, ldflagsModule(t, yaml, portal, mainGo("package main\n\nfunc main() {}\n"))); !v.OK() || v.Verified != 1 {
		t.Fatalf("goreleaser's main file: %+v", v)
	}
}

func TestUnresolvableTargetsAreNotesNotGuesses(t *testing.T) {
	hygiene.Isolate(t)
	build := file{"build.sh", "go build -ldflags \"-X {{ .Env.MODULE }}/cmd/x.Version=1 -X github.com/elsewhere/lib.Version=1\" .\n"}
	v := evaluate(t, ldflagsModule(t, build, mainGo("package main\n\nfunc main() {}\n")))
	if !v.OK() || len(v.Notes) != 2 || !strings.Contains(v.Notes[0], "build-time template") || !strings.Contains(v.Notes[1], "outside this module") {
		t.Fatalf("%+v", v)
	}
}

func TestOnlyTrackedBuildFilesOutsideExcludedPlacesAreRead(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		rel  string
		want bool
	}{
		{".goreleaser.yml", true},
		{"goreleaser.yaml", true},
		{"Makefile", true},
		{"scripts/release.mk", true},
		{"scripts/build.sh", true},
		{".github/workflows/ci.yml", true},
		{".github/ci.yml", false},
		{".rlsbl/bases/.goreleaser.yml", false},
		{".strictmetadata/.scaffold-bases/x.sh", false},
		{"dist/config.yaml", false},
		{"main.go", false},
	}
	for _, c := range cases {
		if got := IsBuildFile(c.rel); got != c.want {
			t.Errorf("IsBuildFile(%q) = %v", c.rel, got)
		}
	}
	m := ldflagsModule(t, file{"build.sh", "go build -ldflags \"-X main.Missing=1\" .\n"}, mainGo("package main\n\nfunc main() {}\n"))
	m.Tracked = []string{"go.mod", "main.go"}
	if v := evaluate(t, m); !v.OK() || v.Verified != 0 {
		t.Fatalf("an untracked build file was read: %+v", v)
	}
}

func TestFindOccurrencesReadsEveryFlagSpelling(t *testing.T) {
	hygiene.Isolate(t)
	text := "a -X main.A=1 -X=main.B=2 '-X' x\n--X main.C=3 -ldflags=\"-X 'main.D=4'\"\n"
	var got []string
	for _, o := range FindOccurrences(text, "f") {
		got = append(got, o.Target)
	}
	if strings.Join(got, ",") != "main.A,main.B,main.D" {
		t.Fatalf("targets = %q", got)
	}
	if p, s, ok := SplitTarget("github.com/o/r/internal/version.Value"); !ok || p != "github.com/o/r/internal/version" || s != "Value" {
		t.Fatalf("SplitTarget = %q %q %v", p, s, ok)
	}
}

func TestNoModuleSkips(t *testing.T) {
	hygiene.Isolate(t)
	if v, _ := EvaluateLdflags(nil); v.SkipReason == "" {
		t.Fatal("no module did not skip")
	}
	if v, _ := EvaluateLdflags([]LdflagsModule{{Dir: t.TempDir()}}); v.SkipReason != "no go.mod found in any Go target" {
		t.Fatalf("%+v", v)
	}
}
