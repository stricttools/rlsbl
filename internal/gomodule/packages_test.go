package gomodule

import (
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// goProject writes a module with files (relative path to content).
func goProject(t *testing.T, files ...file) string {
	t.Helper()
	dir := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(dir, "go.mod"), "module example.com/portal\n\ngo 1.21\n")
	for _, f := range files {
		testsupport.WriteFile(t, filepath.Join(dir, filepath.FromSlash(f.rel)), f.content)
	}
	return dir
}

// withGo runs fn with the effects handle of a read-only command, under
// --dry-run too when dryRun is set: `go list -e -f` is on the observe
// allowlist, so it runs, not recorded, either way.
func withGo(t *testing.T, dryRun bool, fn func(r Runner) error) {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no Go toolchain on PATH")
	}
	t.Setenv("GOPROXY", "off")
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		return fn(e)
	})
}

const mainSource = "package main\n\nfunc main() {}\n"

func TestMainPackagesAreWhatGoListSays(t *testing.T) {
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoCache))
	dir := goProject(t,
		file{"cmd/portal/cli.go", mainSource},
		file{"cmd/portal/cli_test.go", "package main_test\n"},
		file{"cmd/widget/main.go", "package main\n\nimport \"example.com/missing/dependency\"\n\nvar _ = dependency.X\n\nfunc main() {}\n"},
		file{"lib/lib.go", "package lib\n"},
	)
	withGo(t, true, func(r Runner) error {
		mains, err := MainPackages(r, dir)
		if err != nil {
			return err
		}
		var got []string
		for _, p := range mains {
			got = append(got, p.Dir)
		}
		if strings.Join(got, ",") != "./cmd/portal,./cmd/widget" {
			t.Errorf("mains = %q", got)
		}
		return nil
	})
}

func TestTheOneMainPackageIsResolvedAndAmbiguityIsRefused(t *testing.T) {
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoCache))
	single := goProject(t, file{"cmd/portal/main.go", mainSource}, file{"lib/lib.go", "package lib\n"})
	several := goProject(t, file{"cmd/portal/main.go", mainSource}, file{"cmd/widget/main.go", mainSource})
	library := goProject(t, file{"lib/lib.go", "package lib\n"})
	withGo(t, false, func(r Runner) error {
		if dir, err := ResolveMainPackageDir(r, single, nil); err != nil || dir != "./cmd/portal" {
			t.Errorf("single: %q, %v", dir, err)
		}
		if _, err := ResolveMainPackageDir(r, several, nil); err == nil || !strings.Contains(err.Error(), "\"./cmd/portal\", \"./cmd/widget\"") {
			t.Errorf("several without a declaration: %v", err)
		}
		if dir, err := ResolveMainPackageDir(r, several, []string{"cmd/widget"}); err != nil || dir != "./cmd/widget" {
			t.Errorf("a declaration resolves it: %q, %v", dir, err)
		}
		if _, err := ResolveMainPackageDir(r, several, []string{"./cmd/portal", "./cmd/widget"}); err == nil {
			t.Error("two declared paths resolved to one binary")
		}
		if _, err := ResolveMainPackageDir(r, several, []string{"./lib"}); err == nil || !strings.Contains(err.Error(), "is not a main package") {
			t.Errorf("a declared path that is no main package: %v", err)
		}
		if _, err := ResolveMainPackageDir(r, library, nil); err == nil || !strings.Contains(err.Error(), "no main packages") {
			t.Errorf("a library: %v", err)
		}
		if _, err := ListPackages(r, t.TempDir()); err == nil || !strings.Contains(err.Error(), "holds no go.mod") {
			t.Errorf("a directory without go.mod: %v", err)
		}
		return nil
	})
}

func TestAnInstallPathOutsideTheModuleIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	for in, want := range map[string]string{".": ".", "./cmd/x": "./cmd/x", "cmd/x/": "./cmd/x"} {
		if got, err := NormalizeInstallPath(in); err != nil || got != want {
			t.Errorf("NormalizeInstallPath(%q) = %q, %v", in, got, err)
		}
	}
	for _, in := range []string{"../x", "/abs"} {
		if _, err := NormalizeInstallPath(in); err == nil {
			t.Errorf("%q was accepted", in)
		}
	}
}
