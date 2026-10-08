package release_test

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

func version(t *testing.T, s string) semver.Version {
	t.Helper()
	v, err := semver.Parse(s)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func entryTypes(plan release.BumpPlan) string {
	var out []string
	for _, e := range plan.Entries {
		out = append(out, string(e.Type))
	}
	return strings.Join(out, ",")
}

func bumpPlan(t *testing.T, dir string, in release.BumpPlanInputs) release.BumpPlan {
	t.Helper()
	ws, err := workspace.Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	in.Workspace = ws
	plan, err := release.BuildBumpPlan(in)
	if err != nil {
		t.Fatal(err)
	}
	return plan
}

func standaloneInputs(t *testing.T) release.BumpPlanInputs {
	return release.BumpPlanInputs{Releasable: "portal", Representative: "root", Current: version(t, "0.4.0"), Next: version(t, "0.5.0"), Primary: "npm", EcosystemTagging: true, RlsblVersion: "0.200.0", Generated: []string{"docs/cli.md"}}
}

func TestAStandaloneBumpPlansItsVersionWritesTheKeywordAndTheChecks(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.CommitFile("selfdoc.json", "{\"version\": \"0.4.0\"}\n", "docs")
	plan := bumpPlan(t, repo.Dir, standaloneInputs(t))
	if got := entryTypes(plan); got != "write-target-versions,bump-selfdoc,ensure-keyword,clean-artifacts,secret-scan,packed-artifact-contents,guard-unexpected-files" {
		t.Errorf("entries: %s", got)
	}
	files := strings.Join(plan.FilesToCommit, ",")
	for _, want := range []string{"package.json", "selfdoc.json", "CHANGELOG.md", "docs/cli.md"} {
		if !strings.Contains(","+files+",", ","+want+",") {
			t.Errorf("the commit lacks %s: %s", want, files)
		}
	}
	if plan.AlreadyBumped {
		t.Error("an unbumped version reads as bumped")
	}
}

// A pypi target whose only pipeline ships a go binary pipeline's binaries
// as wheels is not built: CI assembles the wheels from the release
// archives, and building the pyproject.toml would make a package that never
// ships. A pypi target publishing its own package is built.
func TestAPypiTargetShippingGoBinariesIsNotBuilt(t *testing.T) {
	hygiene.Isolate(t)
	pyproject := "[project]\nname = \"portal\"\nversion = \"0.4.0\"\n"
	for artifact, built := range map[string]bool{"go-binary\nbinary_pipeline = \"go\"": false, "package": true} {
		repo := testsupport.NewRepo(t)
		repo.Write(declarationsPath, fmt.Sprintf(standaloneDeclarations, "ci")+`description = "A portal"
targets = [{ name = "go" }, { name = "pypi", path = "py" }]

[[members.pipelines]]
name = "go"
type = "go"
target = "go"
local = false
artifact = "binary"

[[members.pipelines]]
name = "pypi"
type = "pypi"
target = "pypi"
local = false
artifact = `+fmt.Sprintf("%q", strings.SplitN(artifact, "\n", 2)[0])+"\n"+strings.Join(strings.SplitN(artifact, "\n", 2)[1:], "")+"\n")
		repo.Write("go.mod", "module example.com/portal\n\ngo 1.25\n")
		repo.Write("main.go", "package main\n\nfunc main() {}\n")
		repo.Write("VERSION", "0.4.0\n")
		repo.Write("py/pyproject.toml", pyproject)
		repo.Commit("the project", declarationsPath, "go.mod", "main.go", "VERSION", "py/pyproject.toml")
		in := standaloneInputs(t)
		in.Primary = "go"
		plan := bumpPlan(t, repo.Dir, in)
		if got := strings.Contains(entryTypes(plan), string(release.EntryBuild)); got != built {
			t.Errorf("artifact %s: built %v, want %v: %s", strings.SplitN(artifact, "\n", 2)[0], got, built, entryTypes(plan))
		}
	}
}

func TestAVersionAlreadyWrittenIsNotWrittenAgain(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.CommitFile("package.json", packageJSON("portal", "0.5.0"), "bumped by the attempt that stopped")
	plan := bumpPlan(t, repo.Dir, standaloneInputs(t))
	if !plan.AlreadyBumped || strings.Contains(entryTypes(plan), "write-target-versions") {
		t.Errorf("the version was written again: %s", entryTypes(plan))
	}
}

func TestAFirstReleaseWritesNoVersion(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	in := standaloneInputs(t)
	in.Next = in.Current
	plan := bumpPlan(t, repo.Dir, in)
	if plan.AlreadyBumped || strings.Contains(entryTypes(plan), "write-") {
		t.Errorf("a first release writes its version: %s", entryTypes(plan))
	}
}

func TestAWorkspaceBumpWritesTheVersionFileAndEveryPublishingMembersVersion(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(declarationsPath, strings.Replace(workspaceDeclarations, "[[releasables]]\nname = \"gadget\"", "[[members]]\npath = \"widget/cli\"\nname = \"cli\"\nreleasable = \"widget\"\n\n[[releasables]]\nname = \"gadget\"", 1))
	repo.Write("widget/package.json", packageJSON("widget", "0.1.0"))
	repo.Write("widget/cli/package.json", packageJSON("widget-cli", "0.1.0"))
	repo.Write(".strictmetadata/releases/widget/version", "0.1.0\n")
	repo.Commit("the workspace", declarationsPath, "widget/package.json", "widget/cli/package.json", ".strictmetadata/releases/widget/version")
	in := release.BumpPlanInputs{Releasable: "widget", Representative: "widget", Current: version(t, "0.1.0"), Next: version(t, "0.2.0"), Primary: "npm"}
	plan := bumpPlan(t, repo.Dir, in)
	if got := entryTypes(plan); got != "write-releasable-version,guard-unexpected-files" {
		t.Errorf("a releasable publishing nothing writes its members' manifests: %s", got)
	}
	repo.CommitFile(declarationsPath, strings.Replace(read(t, repo.Path(declarationsPath)), "name = \"widget\"\ntag_format = \"{name}@v{version}\"\npublish_mode = \"none\"", "name = \"widget\"\ntag_format = \"{name}@v{version}\"\npublish_mode = \"ci\"", 1), "publish widget")
	plan = bumpPlan(t, repo.Dir, in)
	if got := entryTypes(plan); got != "write-releasable-version,write-target-versions,write-member-versions,clean-artifacts,secret-scan,packed-artifact-contents,guard-unexpected-files" {
		t.Errorf("entries: %s", got)
	}
	files := strings.Join(plan.FilesToCommit, ",")
	for _, want := range []string{".strictmetadata/releases/widget/version", "widget/package.json", "widget/cli/package.json", ".strictmetadata/changelog/widget/CHANGELOG.md", "CHANGELOG.md"} {
		if !strings.Contains(","+files+",", ","+want+",") {
			t.Errorf("the commit lacks %s: %s", want, files)
		}
	}
}

func TestAnOlderScaffoldStateRecordsTheReleasingRlsbl(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.CommitFile(declarations.ScaffoldStateFile, "format_version = 1\nrlsbl_version = \"0.100.0\"\n\n[files]\n", "scaffold")
	plan := bumpPlan(t, repo.Dir, standaloneInputs(t))
	var state *release.PlanEntry
	for i := range plan.Entries {
		if plan.Entries[i].Type == release.EntryWriteScaffoldState {
			state = &plan.Entries[i]
		}
	}
	if state == nil || state.State.RlsblVersion != "0.200.0" {
		t.Fatalf("the scaffold state is not written: %s", entryTypes(plan))
	}
	in := standaloneInputs(t)
	in.RlsblVersion = "0.100.0"
	if strings.Contains(entryTypes(bumpPlan(t, repo.Dir, in)), "write-scaffold-state") {
		t.Error("a scaffold state naming the releasing rlsbl is written again")
	}
}

// executeBump builds the standalone plan and issues it in repo.
func executeBump(t *testing.T, repo *testsupport.Repo, dryRun bool) (strictcli.Result, release.BumpResult, error) {
	t.Helper()
	plan := bumpPlan(t, repo.Dir, standaloneInputs(t))
	var result release.BumpResult
	res, err := mutating(t, dryRun, func(e *strictcli.Effects) error {
		ws, err := workspace.Load(repo.Dir)
		if err != nil {
			return err
		}
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		record, err := lifecycle.Load(repo.Dir)
		if err != nil {
			return err
		}
		idx, err := index.Load(filepath.Join(t.TempDir(), "confidential-names.toml"))
		if err != nil {
			return err
		}
		result, err = release.ExecuteBumpPlan(e, plan, release.BumpExecution{Workspace: ws, Releasable: "portal", Repo: r, LiveRoot: repo.Dir, Rerun: "run the release again", Preview: dryRun, Record: record, Index: idx, Now: time.Date(2026, 10, 7, 0, 0, 0, 0, time.UTC), Log: func(string) {}})
		return err
	})
	return res, result, err
}

func TestExecutingTheBumpWritesTheVersionsAndPassesItsChecks(t *testing.T) {
	hygiene.Isolate(t)
	fakeGitleaks(t)
	repo := standalone(t, "ci")
	repo.CommitFile("selfdoc.json", "{\"version\": \"0.4.0\"}\n", "docs")
	if err := os.Chmod(repo.Path("selfdoc.json"), 0o640); err != nil {
		t.Fatal(err)
	}
	_, _, err := executeBump(t, repo, false)
	mustNotFail(t, err)
	if got := read(t, repo.Path("package.json")); !strings.Contains(got, `"version": "0.5.0"`) || !strings.Contains(got, `"rlsbl"`) {
		t.Errorf("package.json: %s", got)
	}
	if got := read(t, repo.Path("selfdoc.json")); got != "{\"version\": \"0.5.0\"}\n" {
		t.Errorf("selfdoc.json: %q", got)
	}
	if info, err := os.Stat(repo.Path("selfdoc.json")); err != nil || info.Mode().Perm() != 0o640 {
		t.Errorf("selfdoc.json's mode was not kept: %v %v", info.Mode(), err)
	}
}

func TestAChangeTheReleaseDidNotMakeIsRefusedUntilRemoved(t *testing.T) {
	hygiene.Isolate(t)
	fakeGitleaks(t)
	repo := standalone(t, "ci")
	repo.Write("stray.txt", "written by something else\n")
	_, _, err := executeBump(t, repo, false)
	if err == nil || !strings.Contains(err.Error(), "stray.txt") {
		t.Fatalf("the stray change was not refused: %v", err)
	}
	// The fix the refusal names: the stray file goes where git ignores it.
	repo.Git("checkout", "--", "package.json")
	repo.CommitFile(".gitignore", "stray.txt\n", "ignore it")
	_, _, err = executeBump(t, repo, false)
	mustNotFail(t, err)
}

func TestAPreviewOfTheBumpWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	before := read(t, repo.Path("package.json"))
	res, _, err := executeBump(t, repo, true)
	mustNotFail(t, err)
	if got := read(t, repo.Path("package.json")); got != before {
		t.Errorf("the preview wrote package.json: %s", got)
	}
	if out := res.Stdout + res.Stderr; !strings.Contains(out, "package.json") {
		t.Errorf("the preview does not name the write:\n%s", out)
	}
	if got := status(t, repo.Dir); got != "" {
		t.Errorf("the preview changed the tree:\n%s", got)
	}
}

func TestThePlanTableNamesEveryEntry(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	plan := bumpPlan(t, repo.Dir, standaloneInputs(t))
	table := release.RenderPlan(plan.Entries)
	for i, e := range plan.Entries {
		if !strings.Contains(table, fmt.Sprintf("%2d. %s", i+1, e.Type)) {
			t.Errorf("the table lacks %s:\n%s", e.Type, table)
		}
	}
}

func TestExecutingTheBumpRefusesAPackedFileAnotherMemberOwns(t *testing.T) {
	hygiene.Isolate(t)
	fakeGitleaks(t)
	repo := testsupport.NewRepo(t)
	decl := strings.Replace(workspaceDeclarations, "publish_mode = \"none\"", "publish_mode = \"ci\"", 1)
	decl = strings.Replace(decl, "[[members]]\npath = \"widget\"\nname = \"widget\"\nreleasable = \"widget\"\n", "[[members]]\npath = \"widget\"\nname = \"widget\"\nreleasable = \"widget\"\ntargets = [{ name = \"go\" }]\n\n[[members.pipelines]]\nname = \"go\"\ntype = \"go\"\ntarget = \"go\"\nlocal = false\nartifact = \"library\"\n", 1)
	decl += "\n[[members]]\npath = \"widget/inner\"\nname = \"inner\"\nreleasable = \"gadget\"\n"
	repo.Write(declarationsPath, decl)
	repo.Write("widget/go.mod", "module example.com/widget\n\ngo 1.22\n")
	repo.Write("widget/widget.go", "package widget\n")
	repo.Write("widget/inner/notes.txt", "the gadget's own notes\n")
	repo.Write("gadget/package.json", packageJSON("gadget", "0.2.0"))
	repo.Write(".strictmetadata/releases/widget/version", "0.1.0\n")
	repo.Write(".strictmetadata/releases/gadget/version", "0.2.0\n")
	repo.Commit("the workspace", declarationsPath, "widget/go.mod", "widget/widget.go", "widget/inner/notes.txt", "gadget/package.json", ".strictmetadata/releases/widget/version", ".strictmetadata/releases/gadget/version")
	in := release.BumpPlanInputs{Releasable: "widget", Representative: "widget", Current: version(t, "0.1.0"), Next: version(t, "0.2.0"), Primary: "go"}
	execute := func() error {
		plan := bumpPlan(t, repo.Dir, in)
		if !strings.Contains(entryTypes(plan), "packed-artifact-contents") {
			t.Fatalf("the plan checks no packed contents: %s", entryTypes(plan))
		}
		_, err := mutating(t, false, func(e *strictcli.Effects) error {
			ws, err := workspace.Load(repo.Dir)
			if err != nil {
				return err
			}
			r, err := git.Open(e, repo.Dir)
			if err != nil {
				return err
			}
			idx, err := index.Load(filepath.Join(t.TempDir(), "confidential-names.toml"))
			if err != nil {
				return err
			}
			_, err = release.ExecuteBumpPlan(e, plan, release.BumpExecution{Workspace: ws, Releasable: "widget", Repo: r, LiveRoot: repo.Dir, Rerun: "run the release again", Record: &lifecycle.Record{}, Index: idx, Now: time.Date(2026, 10, 7, 0, 0, 0, 0, time.UTC), Log: func(string) {}})
			return err
		})
		return err
	}
	err := execute()
	if err == nil {
		t.Fatal("a module zip carrying another member's file was not refused")
	}
	requireContains(t, err.Error(), "would publish files from outside its members' paths", "widget/inner/notes.txt", `the member "inner" owns`)
	// The fix the refusal names: the file is no longer packed, the inner
	// member a module of its own, which the module zip leaves out.
	repo.Git("checkout", "--", ".")
	repo.CommitFile("widget/inner/go.mod", "module example.com/widget/inner\n\ngo 1.22\n", "inner is a module of its own")
	mustNotFail(t, execute())
}
