package cli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// recordingProgram is a fake program recording each call's arguments in
// calls.txt beside it.
const recordingProgram = "#!/bin/sh\nprintf '%s\\n' \"${0##*/} $*\" >> \"${0%/*}/calls.txt\"\n"

// fakePrograms puts first on PATH, for the rest of the test, a directory
// holding a recording fake of each named program, and returns the
// directory.
func fakePrograms(t *testing.T, names ...string) string {
	t.Helper()
	dir := t.TempDir()
	for _, n := range names {
		if err := os.WriteFile(filepath.Join(dir, n), []byte(recordingProgram), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	// Put first, the fakes shadow the programs they name; git stays
	// reachable for the confidential-name index refresh every mutating
	// command makes.
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return dir
}

// recordedCalls are the calls the fakes in dir recorded.
func recordedCalls(t *testing.T, dir string) []string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, "calls.txt"))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		t.Fatal(err)
	}
	return strings.Split(strings.TrimRight(string(data), "\n"), "\n")
}

// standaloneNpmProject is a standalone npm project, the working directory
// for the rest of the test.
func standaloneNpmProject(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n")
	repo.Write("package.json", "{\n  \"name\": \"portal\",\n  \"version\": \"0.1.0\"\n}\n")
	hygiene.Chdir(t, repo.Dir)
	return repo
}

func TestTheDevCommandsAreClassified(t *testing.T) {
	hygiene.Isolate(t)
	group := appWith(t, testsupport.NewFakeHTTP(t)).Groups()["dev"]
	for name, want := range map[string]string{"install": strictcli.EffectMutating, "sync": strictcli.EffectMutating, "status": strictcli.EffectReadOnly} {
		cmd, ok := group.Commands[name]
		if !ok || cmd.Effect != want || cmd.Consequential {
			t.Errorf("dev %s: registered %v, %+v", name, ok, cmd)
		}
	}
}

func TestDevInstallThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	standaloneNpmProject(t)
	dir := fakePrograms(t, "npm")
	app := appWith(t, testsupport.NewFakeHTTP(t))
	if r := app.Test([]string{"dev", "install"}); r.ExitCode == 0 || !strings.Contains(r.Stderr, "target") {
		t.Fatalf("a missing --target: exit %d: %s", r.ExitCode, r.Stderr)
	}
	if r := app.Test([]string{"dev", "install", "--target", "everywhere"}); r.ExitCode == 0 {
		t.Fatalf("an unknown mode: exit %d: %s", r.ExitCode, r.Stderr)
	}
	r := app.Test([]string{"dev", "install", "--target", "global"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"dev", "install", "--target", "venv", "--uninstall"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Skipping npm") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if got := recordedCalls(t, dir); strings.Join(got, ";") != "npm link" {
		t.Fatalf("calls: %v", got)
	}
}

func TestDevInstallRefusesAnEmptyMemberName(t *testing.T) {
	hygiene.Isolate(t)
	standaloneNpmProject(t)
	fakePrograms(t, "npm")
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"dev", "install", "--target", "global", "--include", "widget,,gadget"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "holds an empty member name") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestDevSyncReadsUVNoSyncFromTheEnvironment(t *testing.T) {
	hygiene.Isolate(t)
	repo := standaloneNpmProject(t)
	sibling := filepath.Join(t.TempDir(), "strictcli")
	testsupport.WriteFile(t, filepath.Join(sibling, "pyproject.toml"), "[project]\nname = \"strictcli\"\nversion = \"0.37.0\"\n")
	repo.Write("dev-sources.toml.local-only", "[[overlay]]\npackage = \"strictcli\"\npath = \""+sibling+"\"\n")
	dir := fakePrograms(t, "uv")
	app := appWith(t, testsupport.NewFakeHTTP(t))
	t.Setenv("UV_NO_SYNC", "")
	if r := app.Test([]string{"dev", "sync"}); r.ExitCode != 1 || !strings.Contains(r.Stderr, "UV_NO_SYNC is not set to 1") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	t.Setenv("UV_NO_SYNC", "1")
	if r := app.Test([]string{"dev", "sync"}); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if got := recordedCalls(t, dir); len(got) != 2 || got[1] != "uv pip install -e "+sibling {
		t.Fatalf("calls: %v", got)
	}
	r := app.Test([]string{"dev", "status"})
	if r.ExitCode != 1 || !strings.Contains(r.Stdout, "[MISSING] strictcli") {
		t.Fatalf("the fake uv installs nothing, so the overlay is missing: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestDevStatusWithoutOverlaysPasses(t *testing.T) {
	hygiene.Isolate(t)
	standaloneNpmProject(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"dev", "status"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "No dev overlays are recorded") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}
