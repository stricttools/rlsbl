package dependencies_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/dependencies"
)

func locks(t *testing.T, dir string) dependencies.LocksVerdict {
	t.Helper()
	v, err := dependencies.EvaluateLocks(dir)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

// projectLock is a uv.lock whose entry for the project at "." records
// requiresDist and the dev groups.
func projectLock(version, requiresDist, requiresDev string) string {
	text := "version = 1\n\n[[package]]\nname = \"consumer\"\nversion = \"" + version + "\"\nsource = { editable = \".\" }\n"
	if requiresDist != "" || requiresDev != "" {
		text += "\n[package.metadata]\n"
		if requiresDist != "" {
			text += "requires-dist = [" + requiresDist + "]\n"
		}
		if requiresDev != "" {
			text += "\n[package.metadata.requires-dev]\n" + requiresDev + "\n"
		}
	}
	return text
}

func TestALockThatResolvesTheManifestPasses(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	pyprojectWith(t, dir, []string{"requests>=2, <3", "rich"}, map[string][]string{"cli": {"click>=8"}}, map[string][]string{"dev": {"pytest>=8.0.0-rc1"}})
	write(t, dir, "uv.lock", projectLock("0.1.0",
		`{ name = "click", specifier = ">=8" }, { name = "requests", specifier = "<3,>=2" }, { name = "rich" }`,
		`dev = [{ name = "pytest", specifier = ">=8.0.0rc1" }]`))
	v := locks(t, dir)
	if !v.OK() || !strings.Contains(joined(v.Notes), "pypi: uv.lock resolves pyproject.toml") {
		t.Fatalf("%+v", v)
	}
}

func TestEverySortOfPypiDriftIsReported(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	pyprojectWith(t, dir, []string{"requests>=2.1", "added"}, nil, map[string][]string{"dev": {"pytest"}})
	write(t, dir, "uv.lock", projectLock("0.0.9",
		`{ name = "requests", specifier = ">=2" }, { name = "removed" }`,
		`dev = [{ name = "pytest" }, { name = "gone" }]`))
	problems := joined(locks(t, dir).Problems)
	for _, want := range []string{
		"declares version 0.1.0 but uv.lock records 0.0.9",
		"pyproject.toml declares added, which uv.lock does not record",
		"uv.lock still records removed under pyproject.toml",
		"constrains requests as >=2.1, but uv.lock resolved it from >=2",
		"uv.lock still records gone under dependency group 'dev'",
		"Run `uv lock`",
	} {
		if !strings.Contains(problems, want) {
			t.Errorf("missing %q in\n%s", want, problems)
		}
	}
}

func TestALockWithoutTheProjectOrUnreadableIsAProblem(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	pyprojectWith(t, dir, nil, nil, nil)
	write(t, dir, "uv.lock", "version = 1\n\n[[package]]\nname = \"consumer\"\nversion = \"0.1.0\"\nsource = { editable = \"elsewhere\" }\n")
	if v := locks(t, dir); !strings.Contains(joined(v.Problems), "has no package entry for this project (expected an editable or virtual source at '.')") {
		t.Errorf("%+v", v)
	}
	write(t, dir, "uv.lock", "[[package\n")
	if v := locks(t, dir); !strings.Contains(joined(v.Problems), "could not be read") {
		t.Errorf("%+v", v)
	}
	write(t, dir, "uv.lock", projectLock("0.1.0", "", ""))
	if v := locks(t, dir); !v.OK() {
		t.Errorf("a project requiring nothing has no metadata table and passes: %+v", v)
	}
	write(t, dir, "uv.lock", "version = 1\n\n[[package]]\nname = \"consumer\"\nversion = \"0.1.0\"\nsource = { editable = \".\" }\nmetadata = 3\n")
	if v := locks(t, dir); !strings.Contains(joined(v.Problems), "metadata section that is not a table") {
		t.Errorf("%+v", v)
	}
}

func TestSourcedRequirementsAreComparedByPresenceAndTheRootsSourcesReachMembers(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	write(t, root, "pyproject.toml", "[tool.uv.workspace]\nmembers = [\"packages/*\"]\n\n[tool.uv.sources]\ncore = { workspace = true }\n")
	member := mkdir(t, root, "packages/widget")
	pyprojectWith(t, member, []string{"core>=1.0", "mirrored>=2", "direct @ file:///abs/direct"}, nil, nil)
	write(t, member, "pyproject.toml", string(mustRead(t, member, "pyproject.toml"))+"\n[tool.uv.sources]\nmirrored = { index = \"internal\" }\n")
	write(t, root, "uv.lock", "version = 1\n\n[[package]]\nname = \"consumer\"\nversion = \"0.1.0\"\nsource = { editable = \"packages/widget\" }\n\n[package.metadata]\n"+
		`requires-dist = [{ name = "core", editable = "packages/core" }, { name = "direct", path = "/abs/direct" }, { name = "mirrored", specifier = ">=2", index = "https://internal" }]`+"\n")
	if v := locks(t, member); !v.OK() {
		t.Fatalf("%+v", v)
	}
	// A member's own entry overrides the root's: an index source keeps the
	// specifier, so the lock's missing specifier is drift.
	write(t, member, "pyproject.toml", string(mustRead(t, member, "pyproject.toml"))+"core = { index = \"internal\" }\n")
	if v := locks(t, member); !strings.Contains(joined(v.Problems), "constrains core as >=1.0") {
		t.Errorf("%+v", v)
	}
}

func TestNpmLocks(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	packageJSON(t, dir, map[string]any{"name": "consumer", "version": "0.2.0", "dependencies": map[string]any{"kit": "^1.0.0", "added": "1"}, "devDependencies": map[string]any{"test": "^2"}})
	write(t, dir, "package-lock.json", `{"lockfileVersion": 3, "packages": {"": {"name": "consumer", "version": "0.1.0", "dependencies": {"kit": "^0.9.0", "removed": "1"}, "devDependencies": {"test": "^2"}}}}`)
	problems := joined(locks(t, dir).Problems)
	for _, want := range []string{
		`declares version "0.2.0" but package-lock.json records "0.1.0"`,
		"declares added in dependencies",
		"still records removed in dependencies",
		`constrains kit as "^1.0.0" in dependencies but package-lock.json records "^0.9.0"`,
		"Run `npm install --package-lock-only --ignore-scripts`",
	} {
		if !strings.Contains(problems, want) {
			t.Errorf("missing %q in\n%s", want, problems)
		}
	}
	write(t, dir, "package-lock.json", `{"lockfileVersion": 1, "dependencies": {"kit": {"version": "1.0.0"}}}`)
	v := locks(t, dir)
	if !strings.Contains(joined(v.Problems), "declares added in dependencies, which package-lock.json does not resolve") || !strings.Contains(joined(v.Notes), "lockfileVersion 1 records no root requirement map") {
		t.Errorf("%+v", v)
	}
	write(t, dir, "package-lock.json", "{")
	if v := locks(t, dir); !strings.Contains(joined(v.Problems), "could not be read") {
		t.Errorf("%+v", v)
	}
}

func TestGoSumCoverage(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	write(t, dir, ".git/HEAD", "ref: refs/heads/main\n")
	write(t, dir, "go.mod", "module example.com/consumer\n\ngo 1.26\n\nrequire (\n\texample.com/a v1.0.0\n\texample.com/local v0.1.0\n)\n\nrequire example.com/b v0.2.0\n\nreplace example.com/local => ../local\n")
	if v := locks(t, dir); !strings.Contains(joined(v.Problems), "there is no go.sum") {
		t.Fatalf("%+v", v)
	}
	write(t, dir, "go.sum", "example.com/a v1.0.0 h1:x=\nexample.com/a v1.0.0/go.mod h1:y=\n")
	v := locks(t, dir)
	if !strings.Contains(joined(v.Problems), "1 required module(s) have no go.sum entry (example.com/b v0.2.0)") {
		t.Fatalf("%+v", v)
	}
	write(t, dir, "go.work", "go 1.26\n\nuse .\n")
	write(t, dir, "go.work.sum", "example.com/b v0.2.0/go.mod h1:z=\n")
	if v := locks(t, dir); !v.OK() || !strings.Contains(joined(v.Notes), "go.sum covers all 2 required module(s)") {
		t.Errorf("%+v", v)
	}
}

func TestAGoWorkSumAboveTheRepositoryIsNotRead(t *testing.T) {
	hygiene.Isolate(t)
	outer := t.TempDir()
	write(t, outer, "go.work", "go 1.26\n")
	write(t, outer, "go.work.sum", "example.com/a v1.0.0 h1:x=\n")
	dir := mkdir(t, outer, "repo")
	write(t, dir, ".git/HEAD", "ref: refs/heads/main\n")
	write(t, dir, "go.mod", "module example.com/consumer\n\ngo 1.26\n\nrequire example.com/a v1.0.0\n")
	write(t, dir, "go.sum", "")
	if v := locks(t, dir); v.OK() {
		t.Errorf("the outer go.work.sum was read: %+v", v)
	}
}

func TestNothingToCompareIsASkip(t *testing.T) {
	hygiene.Isolate(t)
	if v := locks(t, t.TempDir()); v.SkipReason == "" {
		t.Errorf("%+v", v)
	}
	dir := t.TempDir()
	write(t, dir, "go.mod", "module example.com/consumer\n\ngo 1.26\n")
	if v := locks(t, dir); v.SkipReason != "" || !strings.Contains(joined(v.Notes), "no sums owed") {
		t.Errorf("%+v", v)
	}
}

func TestNormalizeSpecifier(t *testing.T) {
	hygiene.Isolate(t)
	cases := [][2]string{
		{">=1, <2", "<2,>=1"},
		{"<2,>=1", "<2,>=1"},
		{">=1.0.0-alpha1", ">=1.0.0a1"},
		{">=1.0.0RC1", ">=1.0.0rc1"},
		{">=01.02.03", ">=1.2.3"},
		{"==1.0.post2.dev3", "==1.0.post2.dev3"},
		{"==1.0-1", "==1.0.post1"},
		{"==1.0.*", "==1.0.*"},
		{"===01.0", "===01.0"},
		{"", ""},
	}
	for _, c := range cases {
		if got := dependencies.NormalizeSpecifier(c[0]); got != c[1] {
			t.Errorf("%q: %q, want %q", c[0], got, c[1])
		}
	}
	if dependencies.NormalizeSpecifier(">=1.0") == dependencies.NormalizeSpecifier(">=1.1") {
		t.Error("different bounds collapsed")
	}
}

// mustRead reads rel under dir.
func mustRead(t *testing.T, dir, rel string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(rel)))
	if err != nil {
		t.Fatal(err)
	}
	return data
}
