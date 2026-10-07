package declarations

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// commented is the standalone sample with comments an edit must keep.
const commented = `# Release declarations for gadget.
format_version = 1
repository_layout = "workspace"
release_branches = ["main"] # the only branch

[[releasables]]
# The command-line tool.
name = "gadget"
tag_format = "v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
releasable = "gadget" # versioned with the tool
`

func edit(t *testing.T, text string, f func(ed *Editor) error) (string, *Releasables) {
	t.Helper()
	ed, err := NewEditor([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	if err := f(ed); err != nil {
		t.Fatal(err)
	}
	data, d, err := ed.Result()
	if err != nil {
		t.Fatal(err)
	}
	return string(data), d
}

func TestRenderedDeclarationsParseBackToThemselves(t *testing.T) {
	hygiene.Isolate(t)
	for _, text := range []string{fullSample, standaloneSample, workspaceOfRootOnly} {
		d := mustParse(t, text)
		again := mustParse(t, string(Render(d)))
		if !reflect.DeepEqual(d, again) {
			t.Fatalf("rendering changed the declarations:\n%s", Render(d))
		}
	}
}

func TestAddingAMemberWithPipelinesKeepsEverythingElse(t *testing.T) {
	hygiene.Isolate(t)
	m := Member{
		Path:       "cli",
		Name:       "cli",
		Releasable: "gadget",
		Targets:    []Target{{Name: "go"}},
		Pipelines:  []Pipeline{{Name: "go", Type: "go", Target: "go", Artifact: "binary"}},
	}
	text, d := edit(t, commented, func(ed *Editor) error { return ed.AddMember(m) })
	if !strings.HasPrefix(text, commented) {
		t.Fatalf("the edit changed the existing text:\n%s", text)
	}
	got, ok := d.MemberAt("cli")
	if !ok || !reflect.DeepEqual(got.Pipelines, m.Pipelines) {
		t.Fatalf("the added member reads back as %#v", got)
	}
}

func TestAddingTheFirstReleasableReplacesTheEmptyList(t *testing.T) {
	hygiene.Isolate(t)
	_, d := edit(t, workspaceOfRootOnly, func(ed *Editor) error {
		if err := ed.AddReleasable(Releasable{Name: "tools", TagFormat: "{name}@v{version}", PublishMode: PublishNone}); err != nil {
			return err
		}
		return ed.SetMemberReleasable(".", "tools")
	})
	if d.RootMember().Releasable != "tools" || len(d.Releasables) != 1 {
		t.Fatalf("the edit read back as %#v", d)
	}
}

func TestRemovingTheLastReleasableLeavesAnEmptyList(t *testing.T) {
	hygiene.Isolate(t)
	_, d := edit(t, commented, func(ed *Editor) error {
		if err := ed.SetMemberReleasable(".", ""); err != nil {
			return err
		}
		return ed.RemoveReleasable("gadget")
	})
	if len(d.Releasables) != 0 || d.RootMember().Versioned() {
		t.Fatalf("the edit read back as %#v", d)
	}
}

func TestRenamingAReleasableRenamesEveryReferenceAndKeepsComments(t *testing.T) {
	hygiene.Isolate(t)
	text, d := edit(t, commented, func(ed *Editor) error { return ed.RenameReleasable("gadget", "gizmo") })
	if _, ok := d.Releasable("gizmo"); !ok || d.RootMember().Releasable != "gizmo" {
		t.Fatalf("the rename read back as %#v", d)
	}
	for _, comment := range []string{"# Release declarations for gadget.", "# the only branch", "# The command-line tool.", "# versioned with the tool"} {
		if !strings.Contains(text, comment) {
			t.Fatalf("the rename lost %q:\n%s", comment, text)
		}
	}
}

// threeMembers has a member with pipelines between two without.
const threeMembers = workspaceOfRootOnly + `
[[members]]
path = "cli"
name = "cli"
releasable = false
internal_dep_floors = ["lib"]
targets = [{ name = "go" }, { name = "npm" }]

[[members.pipelines]]
name = "go"
type = "go"
target = "go"
local = false
artifact = "binary"

[[members.pipelines]]
name = "npm"
type = "npm"
target = "npm"
local = false
artifact = "package"

[[members]]
path = "lib"
name = "lib"
releasable = false
`

func TestRemovingAMemberRemovesItsPipelines(t *testing.T) {
	hygiene.Isolate(t)
	_, d := edit(t, threeMembers, func(ed *Editor) error { return ed.RemoveMember("cli") })
	if len(d.Members) != 2 || d.Members[1].Name != "lib" || len(d.Members[1].Pipelines) != 0 {
		t.Fatalf("the removal read back as %#v", d)
	}
}

func TestSettingInternalDepFloors(t *testing.T) {
	hygiene.Isolate(t)
	_, d := edit(t, threeMembers, func(ed *Editor) error { return ed.SetInternalDepFloors("lib", []string{"cli"}) })
	if lib, _ := d.Member("lib"); !reflect.DeepEqual(lib.InternalDepFloors, []string{"cli"}) {
		t.Fatalf("lib reads back as %#v", lib)
	}
	_, d = edit(t, threeMembers, func(ed *Editor) error { return ed.SetInternalDepFloors("cli", nil) })
	if cli, _ := d.Member("cli"); cli.InternalDepFloors != nil {
		t.Fatalf("cli reads back as %#v", cli)
	}
}

func TestAnEditThatBreaksARuleIsRefusedByResult(t *testing.T) {
	hygiene.Isolate(t)
	ed, err := NewEditor([]byte(commented))
	if err != nil {
		t.Fatal(err)
	}
	if err := ed.SetMemberReleasable(".", "gizmo"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := ed.Result(); err == nil || !strings.Contains(err.Error(), "gizmo") {
		t.Fatalf("Result = %v, want a refusal naming gizmo", err)
	}
}

func TestEditsNameWhatTheyCannotFind(t *testing.T) {
	hygiene.Isolate(t)
	ed, err := NewEditor([]byte(commented))
	if err != nil {
		t.Fatal(err)
	}
	if err := ed.RemoveMember("nowhere"); err == nil || !strings.Contains(err.Error(), "\".\"") {
		t.Fatalf("RemoveMember = %v, want a refusal naming the declared paths", err)
	}
	if err := ed.RenameReleasable("gizmo", "x"); err == nil || !strings.Contains(err.Error(), "gadget") {
		t.Fatalf("RenameReleasable = %v, want a refusal naming the declared releasables", err)
	}
	if err := ed.RenameReleasable("gadget", "a/b"); err == nil {
		t.Fatal("a rename to a name with a separator was accepted")
	}
}

// writeFixture writes a file of the repository, the world a test starts
// from, outside any effects handle.
func writeFixture(t *testing.T, root, rel, content string) {
	t.Helper()
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), content)
}

func readFixture(t *testing.T, root, rel string) (string, bool) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
	if os.IsNotExist(err) {
		return "", false
	}
	if err != nil {
		t.Fatal(err)
	}
	return string(data), true
}

func writeDeclarations(t *testing.T, dryRun bool, root, text string) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun}, func(ctx *strictcli.Context) error {
		_, err := Write(ctx.Effects(), root, []byte(text))
		return err
	})
}

func TestWriteCreatesTheFileAndItsManifest(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	if r := writeDeclarations(t, false, root, standaloneSample); r.ExitCode != 0 {
		t.Fatalf("Write failed: %s", r.Stderr)
	}
	if got, _ := readFixture(t, root, ReleasablesFile); got != standaloneSample {
		t.Fatalf("the file holds %q", got)
	}
	if got, _ := readFixture(t, root, ReleasablesDir+"/manifest.toml"); got != "owner = \"rlsbl\"\n" {
		t.Fatalf("the manifest holds %q", got)
	}
	d, err := Load(root)
	if err != nil || d.Releasables[0].Name != "gadget" {
		t.Fatalf("Load = %#v, %v", d, err)
	}
}

func TestWriteRefusesWhatParseRefusesAndWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	if r := writeDeclarations(t, false, root, strings.Replace(standaloneSample, "name = \"root\"", "name = \"base\"", 1)); r.ExitCode == 0 {
		t.Fatal("Write accepted a declaration the rules refuse")
	}
	if _, found := readFixture(t, root, ReleasablesFile); found {
		t.Fatal("a refused declaration was written")
	}
}

func TestWriteUnderDryRunWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	if r := writeDeclarations(t, true, root, standaloneSample); r.ExitCode != 0 {
		t.Fatalf("Write failed: %s", r.Stderr)
	}
	if _, found := readFixture(t, root, ReleasablesDir+"/manifest.toml"); found {
		t.Fatal("a dry run wrote the manifest")
	}
}

func TestWriteRefusesADirectoryAnotherToolOwns(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeFixture(t, root, ReleasablesDir+"/manifest.toml", "owner = \"selfdoc\"\n")
	r := writeDeclarations(t, false, root, standaloneSample)
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, "selfdoc") {
		t.Fatalf("Write exited %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestLoadNamesTheFileItReadsWhenItIsMissing(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	if _, err := Load(root); err == nil || !strings.Contains(err.Error(), ReleasablesFile) {
		t.Fatalf("Load = %v, want a refusal naming %s", err, ReleasablesFile)
	}
}

func TestFindRepositoryRootStopsAtTheNearestRepository(t *testing.T) {
	hygiene.Isolate(t)
	outer := testsupport.NewRepo(t)
	inner := filepath.Join(outer.Dir, "nested")
	testsupport.RunGit(t, outer.Dir, "init", "-q", "-b", "main", inner)
	writeFixture(t, inner, "pkg/file.txt", "x\n")
	got, err := FindRepositoryRoot(filepath.Join(inner, "pkg"))
	if err != nil {
		t.Fatal(err)
	}
	want, _ := filepath.EvalSymlinks(inner)
	if got != want {
		t.Fatalf("FindRepositoryRoot = %s, want %s", got, want)
	}
	if _, err := FindRepositoryRoot(t.TempDir()); err == nil {
		t.Fatal("a directory outside every repository found a root")
	}
}
