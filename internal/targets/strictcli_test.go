package targets

import (
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// detect runs DetectStrictcli in a read-only throwaway command.
func detect(t *testing.T, dir string) (StrictcliProgram, bool, error) {
	t.Helper()
	var program StrictcliProgram
	var found bool
	var detectErr error
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		program, found, detectErr = DetectStrictcli(e, dir)
		return nil
	})
	return program, found, detectErr
}

func TestAPythonStrictcliProgramIsItsFirstScript(t *testing.T) {
	hygiene.Isolate(t)
	dir := project(t, map[string]string{"pyproject.toml": "[project]\nname = \"portal\"\ndependencies = [\"strictcli>=0.30\", \"rich\"]\n\n[project.scripts]\nportal = \"portal.cli:main\"\nportal-admin = \"portal.admin:main\"\n"})
	if program, found, err := detect(t, dir); err != nil || !found || program != (StrictcliProgram{EntryPoint: "portal", Language: StrictcliPython}) {
		t.Fatalf("%+v, %v, %v", program, found, err)
	}
	for _, content := range []string{
		"[project]\nname = \"portal\"\ndependencies = [\"rich\"]\n\n[project.scripts]\nportal = \"portal.cli:main\"\n",
		"[project]\nname = \"portal\"\ndependencies = [\"strictcli\"]\n",
		"[tool.black]\nline-length = 100\n",
	} {
		if _, found, err := detect(t, project(t, map[string]string{"pyproject.toml": content})); err != nil || found {
			t.Errorf("%q: found %v, %v", content, found, err)
		}
	}
}

// The Python read any dependency starting with "strictcli" as strictcli, so
// a project depending on strictcli-extras was taken for a strictcli program.
func TestADistributionMerelyStartingWithStrictcliIsNotStrictcli(t *testing.T) {
	hygiene.Isolate(t)
	dir := project(t, map[string]string{"pyproject.toml": "[project]\nname = \"portal\"\ndependencies = [\"strictcli-extras\"]\n\n[project.scripts]\nportal = \"portal.cli:main\"\n"})
	if _, found, err := detect(t, dir); err != nil || found {
		t.Fatalf("found %v, %v", found, err)
	}
	for _, req := range []string{"strictcli", "strictcli>=1.0", "strictcli [yaml] ==1", "strictcli; python_version>'3'", "strictcli@ file:///x"} {
		if !requirementNames(req, "strictcli") {
			t.Errorf("%q does not name strictcli", req)
		}
	}
}

func TestATypeScriptStrictcliProgramIsItsOneBin(t *testing.T) {
	hygiene.Isolate(t)
	if program, found, err := detect(t, project(t, map[string]string{"package.json": "{\"name\": \"portal\", \"bin\": \"dist/cli.js\", \"dependencies\": {\"strictcli\": \"^1.0.0\"}}"})); err != nil || !found || program.EntryPoint != "dist/cli.js" {
		t.Fatalf("a string bin: %+v, %v, %v", program, found, err)
	}
	if program, found, err := detect(t, project(t, map[string]string{"package.json": "{\"bin\": {\"portal\": \"bin/portal.js\"}, \"devDependencies\": {\"strictcli\": \"1\"}}"})); err != nil || !found || program.EntryPoint != "bin/portal.js" {
		t.Fatalf("one named bin: %+v, %v, %v", program, found, err)
	}
	_, _, err := detect(t, project(t, map[string]string{"package.json": "{\"bin\": {\"a\": \"a.js\", \"b\": \"b.js\"}, \"dependencies\": {\"strictcli\": \"1\"}}"}))
	if err == nil || !strings.Contains(err.Error(), "bin entries declared: \"a\", \"b\"") {
		t.Fatalf("two bins: %v", err)
	}
}

func TestOnlyRequireDirectivesMakeAGoStrictcliProgram(t *testing.T) {
	hygiene.Isolate(t)
	library := project(t, map[string]string{"go.mod": "module github.com/stricttools/strictcli/go\n\ngo 1.21\n"})
	if requires, err := GoStrictcliRequirements(library); err != nil || len(requires) != 0 {
		t.Fatalf("the library's own module directive counted: %q, %v", requires, err)
	}
	both := project(t, map[string]string{"go.mod": "module example.com/portal\n\ngo 1.21\n\nrequire (\n\tgithub.com/smm-h/strictcli/go v0.35.0\n\tgithub.com/stricttools/strictcli-extras v0.1.0\n\tgithub.com/stricttools/strictcli/go v0.38.0\n)\n"})
	requires, err := GoStrictcliRequirements(both)
	if err != nil || !slices.Equal(requires, []string{"github.com/smm-h/strictcli/go", "github.com/stricttools/strictcli/go"}) {
		t.Fatalf("requires = %q, %v", requires, err)
	}
}

func TestTheGoEntryPointIsTheMainPackageImportingStrictcli(t *testing.T) {
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoCache))
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no Go toolchain on PATH")
	}
	// The strictcli module is required but absent: go list -e lists the
	// packages anyway, and nothing is fetched.
	t.Setenv("GOPROXY", "off")
	dir := project(t, map[string]string{
		"go.mod":             "module example.com/portal\n\ngo 1.21\n\nrequire github.com/stricttools/strictcli/go v0.38.0\n",
		"cmd/portal/main.go": "package main\n\nimport _ \"github.com/stricttools/strictcli/go/strictcli\"\n\nfunc main() {}\n",
		"cmd/tool/main.go":   "package main\n\nfunc main() {}\n",
	})
	if program, found, err := detect(t, dir); err != nil || !found || program != (StrictcliProgram{EntryPoint: "./cmd/portal/", Language: StrictcliGo}) {
		t.Fatalf("%+v, %v, %v", program, found, err)
	}
}

// rlsbl's own release dumps its schema, so its own layout must let the
// detection tell its entry point among its main packages.
func TestRlsblsOwnEntryPointIsDetected(t *testing.T) {
	// The module cache is read where it is, so go list resolves rlsbl's
	// requirements without downloading them into the isolated home.
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoModCache))
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	program, found, err := detect(t, root)
	if err != nil || !found || program != (StrictcliProgram{EntryPoint: "./cmd/rlsbl/", Language: StrictcliGo}) {
		t.Fatalf("%+v, %v, %v", program, found, err)
	}
}
