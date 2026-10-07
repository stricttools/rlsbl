package targets

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// project is a directory holding files (relative path to content).
func project(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for rel, content := range files {
		testsupport.WriteFile(t, filepath.Join(dir, filepath.FromSlash(rel)), content)
	}
	return dir
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// mutate runs fn with the effects handle of a mutating throwaway command.
func mutate(t *testing.T, fn func(e *strictcli.Effects) error) strictcli.Result {
	t.Helper()
	return testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, Allowlist: previewapply.Prefixes()}, fn)
}

func TestTheSupportedTargetsAreGoNpmAndPypi(t *testing.T) {
	hygiene.Isolate(t)
	if got := strings.Join(Names(), ","); got != "go,npm,pypi" {
		t.Fatalf("Names = %s", got)
	}
	for _, name := range Names() {
		target, err := Get(name)
		if err != nil || target.Name() != name || target.Facts().Name != name {
			t.Errorf("Get(%q) = %v, %v", name, target, err)
		}
	}
	_, err := Get("docker")
	if err == nil || !strings.Contains(err.Error(), "go, npm, pypi") {
		t.Fatalf("an unsupported target: %v", err)
	}
}

func TestDetectionReadsTheManifests(t *testing.T) {
	hygiene.Isolate(t)
	dir := project(t, map[string]string{
		"go.mod":         "module example.com/portal\n",
		"package.json":   "{}\n",
		"pyproject.toml": "[project]\nname = \"portal\"\n",
	})
	found, err := Detect(dir)
	if err != nil || len(found) != 3 || found[0].Name != Go || found[2].Name != PyPI {
		t.Fatalf("Detect = %+v, %v", found, err)
	}
	// A pyproject.toml without a [project] table declares no package.
	for _, content := range []string{"[tool.uv.workspace]\nmembers = [\"a\"]\n", "[tool.black]\nline-length = 100\n"} {
		found, err := Detect(project(t, map[string]string{"pyproject.toml": content}))
		if err != nil || len(found) != 0 {
			t.Errorf("%q: %+v, %v", content, found, err)
		}
	}
	if _, err := Detect(project(t, map[string]string{"pyproject.toml": "[project\n"})); err == nil {
		t.Error("a pyproject.toml that does not parse was read as no project")
	}
}

func TestMemberTargetsAreDeclaredOrDetected(t *testing.T) {
	hygiene.Isolate(t)
	root := project(t, map[string]string{"widget/package.json": "{}\n", "widget/py/pyproject.toml": "[project]\nname = \"w\"\n"})
	detected, err := MemberTargets(root, declarations.Member{Path: "widget", Name: "widget"})
	if err != nil || len(detected) != 1 || detected[0].Name != NPM {
		t.Fatalf("detected = %+v, %v", detected, err)
	}
	declared := declarations.Member{Path: "widget", Name: "widget", Targets: []declarations.Target{{Name: PyPI, Path: "py"}}}
	if got, err := MemberTargets(root, declared); err != nil || len(got) != 1 || got[0].Path != "py" {
		t.Fatalf("declared = %+v, %v", got, err)
	}
	missing := declarations.Member{Path: "widget", Name: "widget", Targets: []declarations.Target{{Name: Go, Path: "gone"}}}
	if _, err := MemberTargets(root, missing); err == nil || !strings.Contains(err.Error(), "widget/gone, which does not exist") {
		t.Fatalf("a declared target directory that does not exist: %v", err)
	}
}
