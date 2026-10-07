package targets

import (
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestAManifestRenameRewritesOnlyTheNameField(t *testing.T) {
	hygiene.Isolate(t)
	npm, _ := Get(NPM)
	dir := project(t, map[string]string{"package.json": "{\n  \"name\": \"portal\",\n  \"bin\": {\"portal\": \"cli.js\"}\n}\n"})
	plan, err := RenamePackage(npm, dir, "portal", "gateway")
	if err != nil || plan.Occurrences != 1 || string(plan.Text) != "{\n  \"name\": \"gateway\",\n  \"bin\": {\"portal\": \"cli.js\"}\n}\n" {
		t.Fatalf("npm: %+v, %v", plan, err)
	}
	if plan, err := RenamePackage(npm, dir, "other", "gateway"); err != nil || plan.Occurrences != 0 || plan.CurrentName != "portal" {
		t.Fatalf("a name the manifest does not hold: %+v, %v", plan, err)
	}
	if names, err := CommandNames(npm, dir, "portal"); err != nil || len(names) != 1 || !strings.HasSuffix(names[0], "bin \"portal\"") {
		t.Fatalf("command names: %q, %v", names, err)
	}

	pypi, _ := Get(PyPI)
	dir = project(t, map[string]string{
		"pyproject.toml":    "[project]\nname = \"portal\" # the name\n\n[project.scripts]\nportal = \"portal:main\"\n",
		"src/portal/x.py":   "",
		"portal-docs/x.txt": "",
	})
	plan, err = RenamePackage(pypi, dir, "portal", "gateway")
	if err != nil || plan.Occurrences != 1 || !strings.Contains(string(plan.Text), "name = \"gateway\"") || !strings.Contains(string(plan.Text), "portal = \"portal:main\"") {
		t.Fatalf("pypi: %+v, %v", plan, err)
	}
	if names, err := CommandNames(pypi, dir, "portal"); err != nil || len(names) != 1 {
		t.Fatalf("command names: %q, %v", names, err)
	}
	if got := SourceDirs(pypi, dir, "portal"); !slices.Equal(got, []string{filepath.Join(dir, "src", "portal")}) {
		t.Fatalf("source dirs: %q", got)
	}

	golang, _ := Get(Go)
	if _, err := RenamePackage(golang, dir, "portal", "gateway"); err == nil || !strings.Contains(err.Error(), "module-path rewrite") {
		t.Fatalf("go: %v", err)
	}
}
