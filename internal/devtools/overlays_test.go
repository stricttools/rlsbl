package devtools_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/devtools"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

var noSync = devtools.Environment{UVNoSync: "1"}

// overlayProject is a standalone Python project with two sibling checkouts
// outside its repository, strictcli and strictspec (the first with a static
// version, the second with a dynamic one), and an overlay file naming both.
type overlayProject struct {
	repo       *testsupport.Repo
	strictcli  string
	strictspec string
}

func newOverlayProject(t *testing.T) *overlayProject {
	t.Helper()
	repo := testsupport.NewRepo(t)
	standalone(repo, "")
	repo.Write("pyproject.toml", pyproject)
	siblings := t.TempDir()
	p := &overlayProject{repo: repo, strictcli: filepath.Join(siblings, "strictcli"), strictspec: filepath.Join(siblings, "strictspec")}
	testsupport.WriteFile(t, filepath.Join(p.strictcli, "pyproject.toml"), "[project]\nname = \"strictcli\"\nversion = \"0.37.0\"\n")
	testsupport.WriteFile(t, filepath.Join(p.strictspec, "pyproject.toml"), "[project]\nname = \"strictspec\"\ndynamic = [\"version\"]\n")
	p.overlays("[[overlay]]\npackage = \"strictcli\"\npath = \"" + p.strictcli + "\"\n\n[[overlay]]\npackage = \"strictspec\"\npath = \"" + p.strictspec + "\"\n")
	return p
}

func (p *overlayProject) overlays(text string) {
	p.repo.Write(dependencies.OverridesFile, text)
}

func syncIn(t *testing.T, root, dir string, env devtools.Environment) strictcli.Result {
	t.Helper()
	return run(t, root, false, func(ctx *strictcli.Context, w *workspace.Workspace) error {
		return devtools.RunSync(ctx, w, dir, env)
	})
}

func statusIn(t *testing.T, root, dir string) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly}, func(ctx *strictcli.Context) error {
		w, err := workspace.Load(root)
		if err != nil {
			return err
		}
		return devtools.RunStatus(ctx, w, dir, devtools.Environment{})
	})
}

func sentinelExists(t *testing.T, dir string) bool {
	t.Helper()
	_, err := os.Stat(filepath.Join(dir, dependencies.SentinelFile))
	return err == nil
}

func TestSyncRefusesWithoutUVNoSyncAndSettingItClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	f := fakeTools(t, "uv")
	for _, value := range []string{"", "true"} {
		r := syncIn(t, p.repo.Dir, p.repo.Dir, devtools.Environment{UVNoSync: value})
		if r.ExitCode != 1 || !strings.Contains(r.Stderr, "UV_NO_SYNC is not set to 1") || !strings.Contains(r.Stderr, "export UV_NO_SYNC=1") {
			t.Fatalf("UV_NO_SYNC=%q: exit %d:\n%s", value, r.ExitCode, r.Stderr)
		}
	}
	requireArgvs(t, f)
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("the fix did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestSyncRunsOneSyncThenAnEditableInstallPerOverlay(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	f := fakeTools(t, "uv")
	r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync)
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	requireArgvs(t, f,
		"uv sync --inexact --no-install-package strictcli --no-install-package strictspec",
		"uv pip install -e "+p.strictcli,
		"uv pip install -e "+p.strictspec,
	)
	for _, c := range f.calls() {
		if c.dir != p.repo.Dir || c.venv != p.repo.Path(".venv") {
			t.Errorf("%s ran in %s with VIRTUAL_ENV=%s", c.argv, c.dir, c.venv)
		}
	}
	for _, want := range []string{"Overlaying strictcli 0.37.0 from " + p.strictcli, "Overlaying strictspec (dynamic version) from " + p.strictspec} {
		if !strings.Contains(r.Stdout, want) {
			t.Errorf("stdout lacks %q:\n%s", want, r.Stdout)
		}
	}
	recorded, found, err := dependencies.LoadSentinel(p.repo.Dir)
	if err != nil || !found || len(recorded) != 2 {
		t.Fatalf("sentinel: %v %v %v", recorded, found, err)
	}
	if recorded[0].Package != "strictcli" || recorded[0].Version != "0.37.0" || recorded[1].Package != "strictspec" || recorded[1].Version != "" {
		t.Fatalf("sentinel: %+v", recorded)
	}
	// A second run makes the same calls.
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if got := f.argvs(); len(got) != 6 || got[3] != got[0] {
		t.Fatalf("the second run's calls: %v", got)
	}
}

func TestAFailedSyncInstallsNothingAndWritesNoSentinel(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	f := fakeTools(t, "uv")
	f.fail("uv")
	r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "no overlay was installed") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if got := f.argvs(); len(got) != 1 {
		t.Fatalf("calls after the failed sync: %v", got)
	}
	if sentinelExists(t, p.repo.Dir) {
		t.Fatal("a failed sync wrote the sentinel")
	}
}

func TestADryRunSyncRunsNothingAndWritesNoSentinel(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	f := fakeTools(t, "uv")
	r := run(t, p.repo.Dir, true, func(ctx *strictcli.Context, w *workspace.Workspace) error {
		return devtools.RunSync(ctx, w, p.repo.Dir, noSync)
	})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	requireArgvs(t, f)
	if sentinelExists(t, p.repo.Dir) {
		t.Fatal("a dry run wrote the sentinel")
	}
}

func TestAMissingOverlayFileIsRefusedWithTheFormat(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	if err := os.Remove(p.repo.Path(dependencies.OverridesFile)); err != nil {
		t.Fatal(err)
	}
	fakeTools(t, "uv")
	r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "no "+dependencies.OverridesFile) || !strings.Contains(r.Stderr, "[[overlay]]\n    package = ") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	p.overlays("[[overlay]]\npackage = \"strictcli\"\npath = \"" + p.strictcli + "\"\n")
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("the fix did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestAnInvalidOverlayFileIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	f := fakeTools(t, "uv")
	cases := []struct {
		name, text, want string
	}{
		{"invalid TOML", "[[overlay]\n", "is not a valid overlay file"},
		{"an unknown top-level key", "extra = 1\n\n[[overlay]]\npackage = \"strictcli\"\npath = \"" + p.strictcli + "\"\n", "is not a valid overlay file"},
		{"an unknown entry key", "[[overlay]]\npackage = \"strictcli\"\npath = \"" + p.strictcli + "\"\nextras = []\n", "is not a valid overlay file"},
		{"no package", "[[overlay]]\npath = \"" + p.strictcli + "\"\n", "is not a valid overlay file"},
		{"no path", "[[overlay]]\npackage = \"strictcli\"\n", "is not a valid overlay file"},
		{"an empty package", "[[overlay]]\npackage = \"\"\npath = \"" + p.strictcli + "\"\n", "has an empty 'package'"},
		{"no overlays", "", "declares no overlays"},
		{"a package twice", "[[overlay]]\npackage = \"strictcli\"\npath = \"" + p.strictcli + "\"\n\n[[overlay]]\npackage = \"StrictCLI\"\npath = \"" + p.strictcli + "\"\n", "names the package 'strictcli' twice"},
	}
	for _, c := range cases {
		p.overlays(c.text)
		r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync)
		if r.ExitCode != 1 || !strings.Contains(r.Stderr, c.want) {
			t.Errorf("%s: exit %d:\n%s", c.name, r.ExitCode, r.Stderr)
		}
	}
	requireArgvs(t, f)
}

func TestAStaleOverlayNamesTheFileItsPurposeAndBothFixes(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	fakeTools(t, "uv")
	gone := filepath.Join(filepath.Dir(p.strictcli), "moved-away")
	p.overlays("[[overlay]]\npackage = \"strictcli\"\npath = \"" + gone + "\"\n")
	r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync)
	for _, want := range []string{"the checkout path does not exist: " + gone, "the `rlsbl dev sync` overlay file", "point 'path' at the checkout's current location", "delete the [[overlay]] table for 'strictcli'"} {
		if r.ExitCode != 1 || !strings.Contains(r.Stderr, want) {
			t.Errorf("exit %d, stderr lacks %q:\n%s", r.ExitCode, want, r.Stderr)
		}
	}
	// The first fix: point the entry at the checkout.
	p.overlays("[[overlay]]\npackage = \"strictcli\"\npath = \"" + p.strictcli + "\"\n")
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("repointing did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestACheckoutThatIsNoMatchingProjectIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	fakeTools(t, "uv")
	empty := t.TempDir()
	unnamed := filepath.Join(t.TempDir(), "unnamed")
	testsupport.WriteFile(t, filepath.Join(unnamed, "pyproject.toml"), "[tool.uv]\npackage = true\n")
	cases := []struct {
		name, text, want string
	}{
		{"no pyproject.toml", "[[overlay]]\npackage = \"strictcli\"\npath = \"" + empty + "\"\n", "is not an installable project"},
		{"no [project].name", "[[overlay]]\npackage = \"strictcli\"\npath = \"" + unnamed + "\"\n", "declares no [project].name"},
		{"another name", "[[overlay]]\npackage = \"strictcli\"\npath = \"" + p.strictspec + "\"\n", "does not match the checkout's [project].name 'strictspec'"},
	}
	for _, c := range cases {
		p.overlays(c.text)
		r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync)
		if r.ExitCode != 1 || !strings.Contains(r.Stderr, c.want) {
			t.Errorf("%s: exit %d:\n%s", c.name, r.ExitCode, r.Stderr)
		}
	}
}

func TestTheNameMatchIsNormalized(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	fakeTools(t, "uv")
	p.overlays("[[overlay]]\npackage = \"StrictCLI\"\npath = \"" + p.strictcli + "\"\n")
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestAMissingUvIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	fakeTools(t)
	r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "uv is not on PATH") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestAnOverlayInsideTheRepositoryIsRefusedAndASiblingIsFine(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	f := fakeTools(t, "uv")
	p.repo.Write("vendor/strictcli/pyproject.toml", "[project]\nname = \"strictcli\"\nversion = \"0.37.0\"\n")
	p.overlays("[[overlay]]\npackage = \"strictcli\"\npath = \"vendor/strictcli\"\n")
	r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "which is inside this repository") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	requireArgvs(t, f)
	p.overlays("[[overlay]]\npackage = \"strictcli\"\npath = \"" + p.strictcli + "\"\n")
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("a sibling checkout was refused: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

// workspaceOverlay is the widget and gadget workspace with widget a Python
// member and a sibling checkout named sibling for each package name given.
func workspaceOverlay(t *testing.T, names ...string) (*testsupport.Repo, map[string]string) {
	t.Helper()
	repo := testsupport.NewRepo(t)
	workspaceOf(repo)
	repo.Write("packages/widget/pyproject.toml", "[project]\nname = \"widget-py\"\nversion = \"0.1.0\"\n")
	siblings := t.TempDir()
	paths := map[string]string{}
	for _, n := range names {
		paths[n] = filepath.Join(siblings, n)
		testsupport.WriteFile(t, filepath.Join(paths[n], "pyproject.toml"), "[project]\nname = \""+n+"\"\nversion = \"1.0.0\"\n")
	}
	return repo, paths
}

func TestAnOverlayNamingAMemberOrItsRegistryNameIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo, paths := workspaceOverlay(t, "gadget", "widget_py", "strictcli")
	f := fakeTools(t, "uv")
	member := repo.Path("packages/widget")
	for _, pkg := range []string{"gadget", "widget_py"} {
		repo.Write("packages/widget/"+dependencies.OverridesFile, "[[overlay]]\npackage = \""+pkg+"\"\npath = \""+paths[pkg]+"\"\n")
		r := syncIn(t, repo.Dir, member, noSync)
		if r.ExitCode != 1 || !strings.Contains(r.Stderr, "names a member of this workspace") {
			t.Errorf("%s: exit %d:\n%s", pkg, r.ExitCode, r.Stderr)
		}
	}
	requireArgvs(t, f)
	repo.Write("packages/widget/"+dependencies.OverridesFile, "[[overlay]]\npackage = \"strictcli\"\npath = \""+paths["strictcli"]+"\"\n")
	r := syncIn(t, repo.Dir, member, noSync)
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	for _, c := range f.calls() {
		if c.dir != member {
			t.Errorf("%s ran in %s, not the member's directory", c.argv, c.dir)
		}
	}
	if !sentinelExists(t, member) {
		t.Error("the sentinel is not in the member's directory")
	}
}

func TestSyncAtAWorkspacesRootMemberIsRefusedAndAMemberClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	repo, paths := workspaceOverlay(t, "strictcli")
	fakeTools(t, "uv")
	repo.Write("packages/widget/"+dependencies.OverridesFile, "[[overlay]]\npackage = \"strictcli\"\npath = \""+paths["strictcli"]+"\"\n")
	r := syncIn(t, repo.Dir, repo.Dir, noSync)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "the workspace's root member") || !strings.Contains(r.Stderr, "packages/widget") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if r := syncIn(t, repo.Dir, repo.Path("packages/widget"), noSync); r.ExitCode != 0 {
		t.Fatalf("running from the member did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func TestStatusWithoutASentinelPasses(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	r := statusIn(t, p.repo.Dir, p.repo.Dir)
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "No dev overlays are recorded") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestStatusAfterASyncReportsEveryOverlayIntact(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	fakeTools(t, "uv")
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	r := statusIn(t, p.repo.Dir, p.repo.Dir)
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "[ok] strictcli: editable at "+p.strictcli) || !strings.Contains(r.Stdout, "All 2 overlay(s) are intact.") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestAWipedOverlayFailsStatusAndASyncRestoresIt(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	fakeTools(t, "uv")
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	// What a bare `uv sync` does: the locked registry wheel replaces the
	// editable install, which leaves no direct_url.json.
	if err := os.Remove(p.repo.Path(".venv/lib/python3.12/site-packages/strictcli-1.0.0.dist-info/direct_url.json")); err != nil {
		t.Fatal(err)
	}
	r := statusIn(t, p.repo.Dir, p.repo.Dir)
	if r.ExitCode != 1 || !strings.Contains(r.Stdout, "[WIPED] strictcli") || !strings.Contains(r.Stderr, "1 of 2 overlay(s) drifted. Run `rlsbl dev sync`") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	// The fix the refusal names.
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if r := statusIn(t, p.repo.Dir, p.repo.Dir); r.ExitCode != 0 {
		t.Fatalf("the sync did not restore the overlay: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestAMissingOverlayFailsStatus(t *testing.T) {
	hygiene.Isolate(t)
	p := newOverlayProject(t)
	fakeTools(t, "uv")
	if r := syncIn(t, p.repo.Dir, p.repo.Dir, noSync); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if err := os.RemoveAll(p.repo.Path(".venv/lib/python3.12/site-packages/strictspec-1.0.0.dist-info")); err != nil {
		t.Fatal(err)
	}
	r := statusIn(t, p.repo.Dir, p.repo.Dir)
	if r.ExitCode != 1 || !strings.Contains(r.Stdout, "[MISSING] strictspec") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestStatusRefusesARecordedOverlayASyncWouldRefuse(t *testing.T) {
	hygiene.Isolate(t)
	repo, paths := workspaceOverlay(t, "gadget")
	member := repo.Path("packages/widget")
	repo.Write("packages/widget/"+dependencies.SentinelFile, "[[overlay]]\npackage = \"gadget\"\npath = \""+paths["gadget"]+"\"\nversion = \"1.0.0\"\n")
	r := statusIn(t, repo.Dir, member)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "names a member of this workspace") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	repo.Write("packages/widget/"+dependencies.SentinelFile, "[[overlay]]\npackage = \"strictcli\"\npath = \""+repo.Path("vendor")+"\"\nversion = \"\"\n")
	r = statusIn(t, repo.Dir, member)
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "which is inside this repository") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}
