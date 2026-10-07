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
