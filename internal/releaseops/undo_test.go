package releaseops_test

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaseops"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

var undoClock = func() time.Time { return time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC) }

func undo(t *testing.T, p *project, fake *testsupport.FakeHTTP, dryRun bool, req releaseops.UndoRequest) strictcli.Result {
	t.Helper()
	req.Dir, req.Now = p.Dir, undoClock
	return run(t, fake, dryRun, func(ctx *strictcli.Context) error { return releaseops.Undo(ctx, req) })
}

// undoGH is the fake gh of an undo of tag whose Release exists.
func undoGH(t *testing.T, tag string) *testsupport.GH {
	return testsupport.FakeGH(t, authStatus, releaseExists(tag), testsupport.GHAnswer{Args: deleteArgs(tag)})
}

func TestUndoRevertsTheLatestRelease(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	testsupport.FakeSafegit(t)
	gh := undoGH(t, "v0.4.0")
	fake := testsupport.NewFakeHTTP(t, npmDocument("0.3.0"))
	r := undo(t, p, fake, false, releaseops.UndoRequest{})
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "Undid v0.4.0")
	requireContains(t, p.read("package.json"), `"version": "0.3.0"`)
	if p.exists(".strictmetadata/releases/portal/v0.4.0.toml") || p.exists(".strictmetadata/changelog/portal/0.4.0.jsonl") {
		t.Fatal("the archive or the released changelog file of 0.4.0 is still there")
	}
	requireContains(t, p.read(".strictmetadata/releases/portal/unreleased.toml"), `description = "release 0.4.0"`)
	requireContains(t, p.read(".strictmetadata/changelog/portal/unreleased.jsonl"), "shipped in 0.4.0")
	requireContains(t, p.read("CHANGELOG.md"), "Unreleased")
	if _, ok := testsupport.Refs(t, p.Dir)["refs/tags/v0.4.0"]; ok {
		t.Fatal("the tag is still here")
	}
	origin := p.originRefs()
	if _, ok := origin["refs/tags/v0.4.0"]; ok {
		t.Fatal("the tag is still on origin")
	}
	if _, ok := origin["refs/tags/v0.3.0"]; !ok {
		t.Fatal("the previous release's tag was deleted")
	}
	if origin["refs/heads/main"] != p.Head() {
		t.Fatal("the undo was not pushed")
	}
	if _, ok := called(gh.Calls(), deleteArgs("v0.4.0")); !ok {
		t.Fatalf("the Release was not deleted: %+v", gh.Calls())
	}
	if got := p.subjects(2); got[0] != "Undo the release v0.4.0" || got[1] != "Record the undo of v0.4.0" {
		t.Fatalf("the last commits are %q", got)
	}
	var audit map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(p.read(releaseops.AuditPath("portal")))), &audit); err != nil {
		t.Fatal(err)
	}
	if audit["version"] != "0.4.0" || audit["verdict"] != "cleared" || audit["latest"] != true || audit["release_deleted"] != true {
		t.Fatalf("the audit line is %v", audit)
	}
	// The audit line was committed before anything was deleted: it is in
	// the commit below the undo.
	requireContains(t, p.Git("show", "--name-only", "--format=", "HEAD~1"), releaseops.AuditPath("portal"))
}

func TestUndoEvidenceNamesNoVersionInAnyRegistryRequest(t *testing.T) {
	hygiene.Isolate(t)
	p := newPortal(t, "package.json", "pyproject.toml", "go.mod")
	p.release("portal", "0.3.0")
	p.release("portal", "0.4.0")
	testsupport.FakeSafegit(t)
	undoGH(t, "v0.4.0")
	fake := testsupport.NewFakeHTTP(t,
		npmDocument("0.3.0"),
		get("https://pypi.org/pypi/portal/json", 200, `{"releases":{"0.3.0":[{"yanked":false}]}}`),
		get("https://proxy.golang.org/github.com/acme/portal/@v/list", 200, "v0.3.0\n"))
	r := undo(t, p, fake, true, releaseops.UndoRequest{})
	// Origin carries the Go module's tag v0.4.0, which the proxy serves to
	// whoever asks, so the release counts as published.
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "go tag github.com/acme/portal: published")
	if len(fake.Requests()) != 3 {
		t.Fatalf("requests %q", fake.URLs())
	}
	requirePackageLevel(t, fake, "0.4.0", "0.3.0")
}

func TestUndoRefusesAPublishedReleaseNamingYankAndDeprecate(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	gh := undoGH(t, "v0.4.0")
	head := p.Head()
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0", "0.4.0")), false, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "published: npm portal", "rlsbl release yank 0.4.0", "rlsbl release deprecate 0.4.0", "Nothing was changed")
	if p.Head() != head || !p.exists(".strictmetadata/releases/portal/v0.4.0.toml") {
		t.Fatal("a refused undo changed the repository")
	}
	if _, ok := p.originRefs()["refs/tags/v0.4.0"]; !ok {
		t.Fatal("a refused undo deleted the tag")
	}
	if _, ok := called(gh.Calls(), deleteArgs("v0.4.0")); ok {
		t.Fatal("a refused undo deleted the Release")
	}
}

func TestUndoWithoutAnySourceObservingTheAbsenceIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	undoGH(t, "v0.4.0")
	r := undo(t, p, testsupport.NewFakeHTTP(t, get("https://registry.npmjs.org/portal", 503, "down")), false, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "no source observed the version's absence", "HTTP 503")
}

func TestARunningPublishRunBlocksTheUndo(t *testing.T) {
	hygiene.Isolate(t)
	p := newPortal(t, "package.json")
	p.Write(".github/workflows/publish.yml", publishWorkflow)
	p.Commit("the publish workflow", ".github/workflows/publish.yml")
	p.release("portal", "0.3.0")
	commit := p.release("portal", "0.4.0")
	runs := []string{"api", "--method", "GET", "repos/acme/portal/actions/workflows/publish.yml/runs?head_sha=" + commit + "&per_page=100"}
	testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"),
		testsupport.GHAnswer{Args: runs, Stdout: `{"total_count":1,"workflow_runs":[{"id":3,"head_branch":"v0.4.0","head_sha":"` + commit + `","event":"release","status":"in_progress","conclusion":""}]}`})
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "a publish run is still running", "publish.yml")
}

func TestUndoKeepsSomebodysCommitBetweenTheBumpAndTheReleaseCommit(t *testing.T) {
	hygiene.Isolate(t)
	p := newPortal(t, "package.json")
	p.release("portal", "0.3.0")
	p.bump("portal", "0.4.0")
	// A fix that made CI pass, on which the resumed release was tagged.
	fix := p.CommitFile("src/fix.txt", "the fix\n", "fix: make CI pass")
	p.finalize("portal", "0.4.0", fix)
	testsupport.FakeSafegit(t)
	undoGH(t, "v0.4.0")
	requireExit(t, undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), false, releaseops.UndoRequest{}), 0)
	requireContains(t, p.read("package.json"), `"version": "0.3.0"`)
	if p.read("src/fix.txt") != "the fix\n" {
		t.Fatal("the fix was reverted")
	}
}

func TestARevertConflictRefusesBeforeAnythingIsDestroyedUntilTheLaterWorkIsMovedAside(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	later := p.CommitFile("package.json", "{\n  \"name\": \"portal\",\n  \"version\": \"0.4.0-patched\"\n}\n", "edit the version line")
	p.Git("push", "-q", "origin", "main")
	testsupport.FakeSafegit(t)
	gh := undoGH(t, "v0.4.0")
	head := p.Head()
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), false, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "conflicts in package.json", "nothing was destroyed")
	if p.Head() != head {
		t.Fatal("a refused undo committed")
	}
	if _, ok := called(gh.Calls(), deleteArgs("v0.4.0")); ok {
		t.Fatal("a refused undo deleted the Release")
	}
	// What the refusal names: revert the later work's change, commit.
	p.Git("revert", "--no-edit", later)
	p.Git("push", "-q", "origin", "main")
	requireExit(t, undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), false, releaseops.UndoRequest{}), 0)
	requireContains(t, p.read("package.json"), `"version": "0.3.0"`)
}

func TestUndoPassesOverANeverReleasedVersionAboveTheLatestRelease(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	archive := ".strictmetadata/releases/portal/v0.5.0.toml"
	p.Write(archive, "format_version = 2\nbump = \"minor\"\ninclude = []\nexclude = []\ndescription = \"abandoned\"\nnever_released = true\n")
	p.Commit("abandon 0.5.0", archive)
	p.Git("push", "-q", "origin", "main")
	undoGH(t, "v0.4.0")
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "Undo of v0.4.0 (the latest release)")
}

func TestADryRunUndoChangesNothing(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	safegit := testsupport.FakeSafegit(t)
	gh := undoGH(t, "v0.4.0")
	head, origin := p.Head(), p.originRefs()
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "delete the tag v0.4.0 (origin and here)", "revert", "v0.4.0", "nothing was changed")
	if p.Head() != head || len(safegit.Calls()) != 0 || p.exists(releaseops.AuditPath("portal")) {
		t.Fatal("a dry run wrote")
	}
	if got := p.originRefs(); got["refs/tags/v0.4.0"] != origin["refs/tags/v0.4.0"] {
		t.Fatal("a dry run deleted the tag on origin")
	}
	if _, ok := called(gh.Calls(), deleteArgs("v0.4.0")); ok {
		t.Fatal("a dry run deleted the Release")
	}
}

func TestUndoOfAnEarlierVersionRevertsNoCommit(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	testsupport.FakeSafegit(t)
	undoGH(t, "v0.3.0")
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.4.0")), false, releaseops.UndoRequest{Version: "0.3.0"})
	requireExit(t, r, 0)
	requireContains(t, p.read("package.json"), `"version": "0.4.0"`)
	if _, ok := p.originRefs()["refs/tags/v0.3.0"]; ok {
		t.Fatal("the tag of 0.3.0 is still on origin")
	}
	if _, ok := p.originRefs()["refs/tags/v0.4.0"]; !ok {
		t.Fatal("the latest release's tag was deleted")
	}
	if p.exists(".strictmetadata/releases/portal/v0.3.0.toml") {
		t.Fatal("the archive of 0.3.0 is still there")
	}
}

func TestVersionNamingTheLatestReleaseIsRefusedUntilItIsOmitted(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	undoGH(t, "v0.4.0")
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{Version: "0.4.0"})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "Run the undo without --version")
	requireExit(t, undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{}), 0)
}

func TestUndoRefusesOffAReleaseBranchUntilItIsCheckedOut(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	undoGH(t, "v0.4.0")
	p.Git("checkout", "-q", "--detach")
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "not on a release branch", "Check out the release branch")
	p.Git("checkout", "-q", "main")
	requireExit(t, undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{}), 0)
}

func TestAnUnreadableAuditLineRefusesUntilItIsRepaired(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	audit := releaseops.AuditPath("portal")
	p.Write(audit, "{\"version\":\"0.2.0\"}\nnot json\n")
	p.Commit("a broken audit", audit)
	p.Git("push", "-q", "origin", "main")
	undoGH(t, "v0.4.0")
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "line 2 of "+audit, "Repair that line")
	p.Write(audit, "{\"version\":\"0.2.0\"}\n")
	p.Commit("repair the audit", audit)
	p.Git("push", "-q", "origin", "main")
	requireExit(t, undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{}), 0)
}

func TestAReleaseFileHoldingTheNextReleaseRefusesUntilItIsEmptied(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	release := ".strictmetadata/releases/portal/unreleased.toml"
	p.Write(release, "format_version = 2\nbump = \"patch\"\ninclude = []\nexclude = []\ndescription = \"the next one\"\n")
	p.Commit("prepare the next release", release)
	p.Git("push", "-q", "origin", "main")
	undoGH(t, "v0.4.0")
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "holds the next release's fields", "Empty the file's bump and description")
	p.Write(release, "format_version = 2\nbump = \"\"\ninclude = []\nexclude = []\ndescription = \"\"\n")
	p.Commit("set the next release aside", release)
	p.Git("push", "-q", "origin", "main")
	requireExit(t, undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{}), 0)
}

func TestOriginAheadOfTheCheckoutRefusesUntilItIsPulled(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	other := p.CommitFile("notes.txt", "elsewhere\n", "work pushed from elsewhere")
	p.Git("push", "-q", "origin", "main")
	p.Git("reset", "-q", "--hard", "HEAD~1")
	undoGH(t, "v0.4.0")
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "holds commits this checkout does not", "git pull --ff-only")
	p.Git("merge", "-q", "--ff-only", other)
	requireExit(t, undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{}), 0)
}

func TestUndoRefusesWhileAnUnrecordedReleaseIsInProgressUntilItIsAbandoned(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	p.bump("portal", "0.5.0")
	p.Git("push", "-q", "origin", "main")
	state := p.inProgress("portal", "0.5.0")
	testsupport.FakeSafegit(t)
	testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"), releaseMissing("v0.5.0"))
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "a release of 0.5.0 is in progress", "would have reverted 0.4.0", "rlsbl release resume", releaseops.AbandonInvocation, state)
	// The fix it names: abandon the attempt.
	requireExit(t, abandon(t, p, false), 0)
	p.Git("push", "-q", "origin", "main")
	r = undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	if strings.Contains(r.Stderr, "is in progress") {
		t.Fatalf("the refusal did not clear:\n%s", r.Stderr)
	}
}

func TestALeftoverStateFileOfAnAbandonedVersionRefusesUntilTheAbandonFinishes(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	archive := ".strictmetadata/releases/portal/v0.5.0.toml"
	p.Write(archive, "format_version = 2\nbump = \"minor\"\ninclude = []\nexclude = []\ndescription = \"abandoned\"\nnever_released = true\n")
	p.Commit("abandon 0.5.0", archive)
	p.Git("push", "-q", "origin", "main")
	p.inProgress("portal", "0.5.0")
	testsupport.FakeSafegit(t)
	undoGH(t, "v0.4.0")
	r := undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "holds 0.5.0 as never released", "left over", releaseops.AbandonInvocation)
	requireExit(t, abandon(t, p, false), 0)
	if p.exists(runstate.InProgressPath("portal")) {
		t.Fatal("the abandon left the state file")
	}
	requireExit(t, undo(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0")), true, releaseops.UndoRequest{}), 0)
}

func TestAWorkspaceReleasableIsUndoneByItsOwnTagAndSubject(t *testing.T) {
	hygiene.Isolate(t)
	p := newWorkspace(t)
	p.release("widget", "0.2.0")
	p.release("gadget", "0.2.0")
	p.release("widget", "0.3.0")
	testsupport.FakeSafegit(t)
	undoGH(t, "widget@v0.3.0")
	fake := testsupport.NewFakeHTTP(t, get("https://registry.npmjs.org/widget", 404, `{"error":"Not found"}`))
	r := run(t, fake, false, func(ctx *strictcli.Context) error {
		return releaseops.Undo(ctx, releaseops.UndoRequest{Dir: p.Path("widget"), Now: undoClock})
	})
	requireExit(t, r, 0)
	requireContains(t, p.read("widget/package.json"), `"version": "0.2.0"`)
	requireContains(t, p.read(".strictmetadata/releases/widget/version"), "0.2.0")
	if _, ok := p.originRefs()["refs/tags/gadget@v0.2.0"]; !ok {
		t.Fatal("another releasable's tag was deleted")
	}
	if p.exists(runstate.InProgressPath("widget")) {
		t.Fatal("the in-progress state is still there after the undo")
	}
}
