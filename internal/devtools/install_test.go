package devtools_test

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/devtools"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

const (
	packageJSON = "{\n  \"name\": \"portal\",\n  \"version\": \"0.1.0\"\n}\n"
	pyproject   = "[project]\nname = \"portal\"\nversion = \"0.1.0\"\n"
	goMod       = "module example.com/portal\n\ngo 1.26\n"
	goMain      = "package main\n\nfunc main() {}\n"
)

func install(t *testing.T, root string, dryRun bool, req devtools.InstallRequest) strictcli.Result {
	t.Helper()
	return run(t, root, dryRun, func(ctx *strictcli.Context, w *workspace.Workspace) error {
		return devtools.RunInstall(ctx, w, req)
	})
}

func requireArgvs(t *testing.T, f *tools, want ...string) {
	t.Helper()
	got := f.argvs()
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("the calls were:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}

func TestPypiAndNpmInstallGloballyFromTheProjectDirectory(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("package.json", packageJSON)
	repo.Write("pyproject.toml", pyproject)
	f := fakeTools(t, "npm", "uv")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	requireArgvs(t, f, "npm link", "uv tool install -e .")
	for _, c := range f.calls() {
		if c.dir != repo.Dir {
			t.Errorf("%s ran in %s", c.argv, c.dir)
		}
	}
}

func TestVenvRunsTheProjectEnvironmentCommands(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("package.json", packageJSON)
	repo.Write("pyproject.toml", pyproject)
	f := fakeTools(t, "npm", "uv")
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Venv}); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	requireArgvs(t, f, "npm install", "uv sync --all-packages")
}

func TestUninstallUsesThePackageName(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("package.json", packageJSON)
	repo.Write("pyproject.toml", pyproject)
	f := fakeTools(t, "npm", "uv")
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global, Uninstall: true}); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	requireArgvs(t, f, "npm unlink", "uv tool uninstall portal")
}

func TestAVenvUninstallIsSkippedWithTheReason(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("pyproject.toml", pyproject)
	f := fakeTools(t, "uv")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Venv, Uninstall: true})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Skipping pypi: this target has no uninstall for --target venv") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	requireArgvs(t, f)
}

// goProject is a Go module with one main package at cmd/portal, its go
// pipeline declaring installPaths (none when empty).
func goProject(t *testing.T, installPaths string) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	lines := "targets = [{ name = \"go\" }]\n"
	if installPaths != "" {
		lines += "\n[[members.pipelines]]\nname = \"go\"\ntype = \"go\"\ntarget = \"go\"\nlocal = false\nartifact = \"binary\"\ninstall_paths = " + installPaths + "\n"
	}
	standalone(repo, lines)
	repo.Write("go.mod", goMod)
	repo.Write("cmd/portal/main.go", goMain)
	return repo
}

func TestGoInstallsTheDeclaredMainPackages(t *testing.T) {
	hygiene.Isolate(t)
	repo := goProject(t, `["./cmd/portal"]`)
	f := fakeTools(t, "go")
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global}); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	requireArgvs(t, f, "go install ./cmd/portal")
}

func TestGoHasNoVenvModeAndNoUninstall(t *testing.T) {
	hygiene.Isolate(t)
	repo := goProject(t, `["./cmd/portal"]`)
	f := fakeTools(t, "go")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Venv})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Skipping go: --target venv is not supported for this target") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global, Uninstall: true})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Skipping go: this target has no uninstall for --target global") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	requireArgvs(t, f)
}

func TestGoWithoutInstallPathsIsRefusedAndDeclaringThemClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	repo := goProject(t, "")
	f := fakeTools(t, "go")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "declares no install_paths") || !strings.Contains(r.Stderr, `install_paths = ["./cmd/portal"]`) {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	requireArgvs(t, f)
	// The fix the refusal names: declare install_paths on the go pipeline.
	standalone(repo, "targets = [{ name = \"go\" }]\n\n[[members.pipelines]]\nname = \"go\"\ntype = \"go\"\ntarget = \"go\"\nlocal = false\nartifact = \"binary\"\ninstall_paths = [\"./cmd/portal\"]\n")
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global}); r.ExitCode != 0 {
		t.Fatalf("the fix did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	requireArgvs(t, f, "go install ./cmd/portal")
}

func TestAProjectWithoutTargetsIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	fakeTools(t, "npm", "uv")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "has no target to install") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestAMissingToolRefusesBeforeAnythingRuns(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("package.json", packageJSON)
	repo.Write("pyproject.toml", pyproject)
	f := fakeTools(t, "npm")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "uv is not on PATH") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	requireArgvs(t, f)
}

func TestAFailedInstallFailsTheRunAfterTheOthersRan(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("package.json", packageJSON)
	repo.Write("pyproject.toml", pyproject)
	f := fakeTools(t, "npm", "uv")
	f.fail("npm")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "installing failed for") || !strings.Contains(r.Stderr, "root (npm") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	requireArgvs(t, f, "npm link", "uv tool install -e .")
}

func TestPathScopedTargetsRunInTheirOwnDirectories(t *testing.T) {
	hygiene.Isolate(t)
	repo := goProject(t, "")
	standalone(repo, "targets = [{ name = \"go\" }, { name = \"npm\", path = \"npm\" }, { name = \"pypi\", path = \"pypi\" }]\n\n[[members.pipelines]]\nname = \"go\"\ntype = \"go\"\ntarget = \"go\"\nlocal = false\nartifact = \"binary\"\ninstall_paths = [\"./cmd/portal\"]\n")
	repo.Write("npm/package.json", packageJSON)
	repo.Write("pypi/pyproject.toml", "[project]\nname = \"portal-py\"\nversion = \"0.1.0\"\n")
	f := fakeTools(t, "go", "npm", "uv")
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global, Uninstall: true}); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	want := map[string]string{"npm unlink": repo.Path("npm"), "uv tool uninstall portal-py": repo.Path("pypi")}
	calls := f.calls()
	if len(calls) != 2 {
		t.Fatalf("calls: %+v", calls)
	}
	for _, c := range calls {
		if want[c.argv] != c.dir {
			t.Errorf("%s ran in %s", c.argv, c.dir)
		}
	}
}

// widgetAndGadget is a workspace whose widget and gadget members are npm
// packages and whose root member is a dev node holding a pyproject.toml.
func widgetAndGadget(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	workspaceOf(repo)
	repo.Write("pyproject.toml", "[project]\nname = \"root\"\nversion = \"0.1.0\"\n")
	repo.Write("packages/widget/package.json", "{\n  \"name\": \"widget\",\n  \"version\": \"0.1.0\"\n}\n")
	repo.Write("packages/gadget/package.json", "{\n  \"name\": \"gadget\",\n  \"version\": \"0.1.0\"\n}\n")
	return repo
}

func dirsOf(f *tools) []string {
	var out []string
	for _, c := range f.calls() {
		out = append(out, c.dir)
	}
	return out
}

func TestAWorkspaceNeedsTheMembersNamed(t *testing.T) {
	hygiene.Isolate(t)
	repo := widgetAndGadget(t)
	f := fakeTools(t, "npm", "uv")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "--all") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	requireArgvs(t, f)
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global, All: true}); r.ExitCode != 0 {
		t.Fatalf("the flag the refusal names did not clear it: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestAllInstallsEveryMemberButADevNode(t *testing.T) {
	hygiene.Isolate(t)
	repo := widgetAndGadget(t)
	f := fakeTools(t, "npm", "uv")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global, All: true})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "=== widget ===") || strings.Contains(r.Stdout, "=== root ===") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if got := dirsOf(f); strings.Join(got, ",") != repo.Path("packages/widget")+","+repo.Path("packages/gadget") {
		t.Fatalf("installed from %v", got)
	}
}

func TestIncludeAndExcludeFilter(t *testing.T) {
	hygiene.Isolate(t)
	repo := widgetAndGadget(t)
	f := fakeTools(t, "npm", "uv")
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global, Include: []string{"gadget"}}); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if got := dirsOf(f); strings.Join(got, ",") != repo.Path("packages/gadget") {
		t.Fatalf("--include installed from %v", got)
	}
	g := fakeTools(t, "npm", "uv")
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global, Exclude: []string{"gadget"}}); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if got := dirsOf(g); strings.Join(got, ",") != repo.Path("packages/widget") {
		t.Fatalf("--exclude installed from %v", got)
	}
}

func TestIncludeInstallsANamedDevNode(t *testing.T) {
	hygiene.Isolate(t)
	repo := widgetAndGadget(t)
	f := fakeTools(t, "npm", "uv")
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global, Include: []string{"root"}}); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	requireArgvs(t, f, "uv tool install -e .")
}

func TestAWorkspaceSelectionIsRefusedWhenItIsWrong(t *testing.T) {
	hygiene.Isolate(t)
	repo := widgetAndGadget(t)
	f := fakeTools(t, "npm", "uv")
	cases := []struct {
		name string
		req  devtools.InstallRequest
		want string
	}{
		{"an unknown member", devtools.InstallRequest{Include: []string{"portal"}}, `"portal" is not a member of this workspace; its members are root, widget, gadget`},
		{"--all with --include", devtools.InstallRequest{All: true, Include: []string{"widget"}}, "contradict"},
		{"nothing left", devtools.InstallRequest{Exclude: []string{"widget", "gadget"}}, "no member matched"},
	}
	for _, c := range cases {
		c.req.Mode = devtools.Global
		r := install(t, repo.Dir, false, c.req)
		if r.ExitCode != 1 || !strings.Contains(r.Stderr, c.want) {
			t.Errorf("%s: exit %d:\n%s", c.name, r.ExitCode, r.Stderr)
		}
	}
	requireArgvs(t, f)
}

func TestAStandaloneProjectTakesNoMemberSelection(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("package.json", packageJSON)
	f := fakeTools(t, "npm")
	r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global, All: true})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "run the command without them") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	requireArgvs(t, f)
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: devtools.Global}); r.ExitCode != 0 {
		t.Fatalf("the fix did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestADryRunInstallsNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("package.json", packageJSON)
	f := fakeTools(t, "npm")
	r := install(t, repo.Dir, true, devtools.InstallRequest{Mode: devtools.Global})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "npm link") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	requireArgvs(t, f)
}

func TestAnUnknownModeIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("package.json", packageJSON)
	fakeTools(t, "npm")
	if r := install(t, repo.Dir, false, devtools.InstallRequest{Mode: "everywhere"}); r.ExitCode != 1 || !strings.Contains(r.Stderr, "neither global nor venv") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}
