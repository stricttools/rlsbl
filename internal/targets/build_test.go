package targets

import (
	"os/exec"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestOnlyPythonIsBuiltByTheRelease(t *testing.T) {
	hygiene.Isolate(t)
	for _, name := range []string{Go, NPM} {
		target, _ := Get(name)
		if err := Build(nil, target, BuildInputs{Dir: t.TempDir()}); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// A rewritten build copies the project into a scratch directory, leaving out
// version control, rlsbl's records, dist/, and compiled files, writes the
// rewritten pyproject.toml there, and builds into the project's own dist/.
func TestARewrittenBuildRunsInACopyAndLeavesTheTreeAlone(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := exec.LookPath("uv"); err != nil {
		t.Skip("no uv on PATH")
	}
	dir := project(t, map[string]string{
		"pyproject.toml":              "[project]\nname = \"widget\"\nversion = \"1.0.0\"\ndependencies = [\"gadget @ file:///../gadget\"]\n",
		"widget/__init__.py":          "",
		"widget/__pycache__/x.pyc":    "",
		".strictmetadata/manifest.ok": "",
		"dist/old.whl":                "",
	})
	target, _ := Get(PyPI)
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: true, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		return Build(ctx.Effects(), target, BuildInputs{Dir: dir, RewrittenPyproject: []byte("[project]\nname = \"widget\"\nversion = \"1.0.0\"\ndependencies = [\"gadget>=0.2.0\"]\n")})
	})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d: %s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	log := r.Stdout + r.Stderr
	for _, want := range []string{"widget/__init__.py", "pyproject.toml", "uv build --out-dir " + dir + "/dist"} {
		if !strings.Contains(log, want) {
			t.Errorf("the preview does not name %q:\n%s", want, log)
		}
	}
	for _, unwanted := range []string{"x.pyc", "manifest.ok", "old.whl"} {
		if strings.Contains(log, unwanted) {
			t.Errorf("the copy carries %s:\n%s", unwanted, log)
		}
	}
	if got := read(t, dir+"/pyproject.toml"); !strings.Contains(got, "file:///../gadget") {
		t.Errorf("the working tree's pyproject.toml was changed:\n%s", got)
	}
}

func TestDevInstallCommandsPerTarget(t *testing.T) {
	hygiene.Isolate(t)
	npm, _ := Get(NPM)
	install, err := DevInstallOf(nil, npm, t.TempDir(), nil)
	if err != nil || install.Global.Tool != "npm" || install.Venv.Args[0] != "install" || install.Venv.UninstallArgs != nil {
		t.Fatalf("npm: %+v, %v", install, err)
	}
	pypi, _ := Get(PyPI)
	install, err = DevInstallOf(nil, pypi, t.TempDir(), nil)
	if err != nil || strings.Join(install.Global.UninstallArgs, " ") != "tool uninstall {name}" {
		t.Fatalf("pypi: %+v, %v", install, err)
	}
}
