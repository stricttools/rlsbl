package historyrewrite_test

import (
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/historyrewrite"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

const planPath = ".strictmetadata/.release-state/portal/reconcile-plan.toml"

// released is portal's history with 0.1.0 released, its tag pushed to a
// bare origin with the branch unless tagOnOrigin is false.
type released struct {
	repo *testsupport.Repo
	bare string
	h    history
}

func newReleased(t *testing.T, tagOnOrigin bool) *released {
	t.Helper()
	repo := testsupport.NewRepo(t)
	h := buildHistory(t, repo, "notes")
	repo.Git("tag", "v0.1.0", h.R)
	bare := repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	if tagOnOrigin {
		repo.Git("push", "-q", "origin", "v0.1.0")
	}
	return &released{repo: repo, bare: bare, h: h}
}

// reconcile runs one half of the reconcile of the releasable the directory
// dir selects, or of named where it selects none.
func reconcile(t *testing.T, dir, named string, mode historyrewrite.ReconcileMode, dryRun bool) strictcli.Result {
	t.Helper()
	return run(t, dryRun, func(ctx *strictcli.Context) error {
		ws, err := workspace.Discover(dir)
		if err != nil {
			return err
		}
		rel, err := historyrewrite.SelectReleasable(ws, dir, named)
		if err != nil {
			return err
		}
		return historyrewrite.Reconcile(ctx, ws.Root, historyrewrite.ReconcileRequest{Mode: mode, Releasable: rel, PushTimeout: time.Minute, Version: "0.0.0-test"}, scrubNow)
	})
}

// listed answers the Release listing with the tags given.
func listed(tags ...string) testsupport.GHAnswer {
	out := ""
	for _, tag := range tags {
		out += tag + "\n"
	}
	return testsupport.GHAnswer{Args: ghList, Stdout: out}
}

func TestAMatchingWorldPlansNothingAndAppliesAsANoOp(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, true)
	gh(t, listed("v0.1.0"))
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false)
	requireExit(t, r, 0)
	plan := readFile(t, f.repo, planPath)
	if !strings.Contains(plan, `state = "already-correct"`) || strings.Contains(plan, `"materialize"`) {
		t.Fatalf("the plan judges the matching world wrongly:\n%s", plan)
	}
	r = reconcile(t, f.repo.Dir, "", historyrewrite.ReconcileApply, false)
	requireExit(t, r, 0)
	if !strings.Contains(r.Stdout, "Applied 0 change(s)") || exists(f.repo, planPath) {
		t.Fatalf("the apply was not a clean no-op:\n%s", r.Stdout)
	}
}

func TestATagOriginLacksIsPushed(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, false)
	gh(t, listed("v0.1.0"))
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false), 0)
	if !strings.Contains(readFile(t, f.repo, planPath), `state = "materialize"`) {
		t.Fatal("the missing tag was not planned")
	}
	if _, onOrigin := testsupport.Refs(t, f.bare)["refs/tags/v0.1.0"]; onOrigin {
		t.Fatal("the plan pushed")
	}
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcileApply, false), 0)
	if got := testsupport.Refs(t, f.bare)["refs/tags/v0.1.0"]; got != f.h.R {
		t.Fatalf("origin's v0.1.0 is %q, want %s", got, f.h.R)
	}
}

func TestAMissingReleaseIsCreatedFromTheRecord(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, true)
	fake := gh(t, listed(),
		testsupport.GHAnswer{Args: ghLatest, Stderr: "release not found", Exit: 1},
		testsupport.GHAnswer{Args: ghCreate})
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false), 0)
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcileApply, false), 0)
	var created bool
	for _, c := range fake.Calls() {
		if strings.Join(c.Args, " ") == strings.Join(ghCreate, " ") {
			created = strings.Contains(c.Stdin, "<!-- rlsbl-ci-sha: "+f.h.R+" -->")
		}
	}
	if !created {
		t.Fatalf("the Release was not created with the release commit's marker: %+v", fake.Calls())
	}
}

func TestATagARecordedRewriteMovedIsRePointedWithItsMarker(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, false)
	// Origin holds the tag at the commit the rewrite replaced.
	f.repo.Git("push", "-q", "origin", f.h.B+":refs/tags/v0.1.0")
	writeJournal(t, f.repo, "rw1", map[string]string{f.h.B: f.h.R}, true)
	fake := gh(t, listed("v0.1.0"),
		testsupport.GHAnswer{Args: ghBody, Stdout: "notes\n\n<!-- rlsbl-ci-sha: " + f.h.B + " -->"},
		testsupport.GHAnswer{Args: ghEdit})
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false)
	requireExit(t, r, 0)
	plan := readFile(t, f.repo, planPath)
	if !strings.Contains(plan, `state = "re-point-with-lease"`) || !strings.Contains(plan, `observed = "`+f.h.B+`"`) {
		t.Fatalf("the moved tag was not planned with its lease:\n%s", plan)
	}
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcileApply, false), 0)
	if got := testsupport.Refs(t, f.bare)["refs/tags/v0.1.0"]; got != f.h.R {
		t.Fatalf("origin's v0.1.0 is %q, want %s", got, f.h.R)
	}
	var marked bool
	for _, c := range fake.Calls() {
		if strings.Join(c.Args, " ") == strings.Join(ghEdit, " ") {
			marked = strings.Contains(c.Stdin, "<!-- rlsbl-ci-sha: "+f.h.R+" -->") && !strings.Contains(c.Stdin, f.h.B)
		}
	}
	if !marked {
		t.Fatalf("the Release's marker was not moved: %+v", fake.Calls())
	}
}

func TestADivergenceNothingExplainsTripsTheWholeReconcile(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, false)
	f.repo.Git("push", "-q", "origin", f.h.B+":refs/tags/v0.1.0")
	gh(t, listed())
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "refusing to reconcile", "refs/tags/v0.1.0", "refuse-foreign", "Nothing has been changed")
	if exists(f.repo, planPath) {
		t.Fatal("a refused plan was written")
	}
}

func TestALocalRefDisagreeingWithTheRecordIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, false)
	f.repo.Git("tag", "-f", "v0.1.0", f.h.B)
	gh(t, listed("v0.1.0"))
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "the local ref does not match the release record")

	// Pointing the tag at the release commit clears it.
	f.repo.Git("tag", "-f", "v0.1.0", f.h.R)
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false), 0)
}

func TestAnApplyWithoutAPlanIsRefusedUntilThePlanIsWritten(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, true)
	gh(t, listed("v0.1.0"))
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcileApply, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "there is no reconcile plan", "--mode plan")

	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false), 0)
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcileApply, false), 0)
}

func TestAPlanWhoseWorldMovedIsRefusedUntilPlannedAgain(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, true)
	gh(t, listed("v0.1.0"))
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false), 0)
	f.repo.Git("push", "-q", "origin", "main:refs/heads/other")
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcileApply, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "the world changed since", "--mode plan")

	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false), 0)
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcileApply, false), 0)
}

func TestATagMovedHereAfterThePlanRefusesTheApply(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, false)
	f.repo.Git("tag", "-d", "v0.1.0")
	gh(t, listed("v0.1.0"))
	// Planned while the tag is absent here: materialized at the release
	// commit, created locally.
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false), 0)
	// The tag brought here at another commit changes the verdict without
	// changing origin.
	f.repo.Git("tag", "v0.1.0", f.h.B)
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcileApply, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "refs/tags/v0.1.0")
	if _, onOrigin := testsupport.Refs(t, f.bare)["refs/tags/v0.1.0"]; onOrigin {
		t.Fatal("the refused apply pushed")
	}
}

// danglingArchive points the committed archive of 0.1.0 at a commit the
// repository does not have, recording the release commit's tree: what an
// out-of-band rewrite leaves.
func danglingArchive(t *testing.T, f *released) string {
	t.Helper()
	missing := strings.Repeat("e", 40)
	tree := f.repo.Git("rev-parse", f.h.R+"^{tree}")
	f.repo.Write(archivePath, archiveText(missing, tree))
	f.repo.Git("add", "-A")
	f.repo.Git("commit", "-q", "-m", "an archive naming a rewritten commit")
	return missing
}

func TestADanglingReleaseCommitARecordExplainsIsHealedAndCommitted(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, true)
	missing := danglingArchive(t, f)
	newSafegit(t, "0.31.0")
	writeJournal(t, f.repo, "rw1", map[string]string{missing: f.h.R}, true)
	gh(t, listed("v0.1.0"))

	// A dry run heals nothing and judges the world a run without --dry-run would.
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, true)
	requireExit(t, r, 0)
	if !strings.Contains(readFile(t, f.repo, archivePath), missing) || !strings.Contains(r.Stdout, "already-correct") {
		t.Fatalf("the dry run wrote or judged against the dangling commit:\n%s", r.Stdout)
	}

	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false), 0)
	if !strings.Contains(readFile(t, f.repo, archivePath), `release_commit = "`+f.h.R+`"`) {
		t.Fatal("the archive was not moved through the journal")
	}
	if subject := f.repo.Git("log", "-1", "--format=%s"); !strings.Contains(subject, "move the release record's release commits") {
		t.Fatalf("the healed archive was not committed (last commit %q)", subject)
	}
	if !strings.Contains(readFile(t, f.repo, transitionsPath), `"event":"release-commit-remap"`) {
		t.Fatal("the move was not recorded in the transition record")
	}
}

func TestADanglingReleaseCommitNothingExplainsIsRefusedUntilARecordDoes(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, true)
	missing := danglingArchive(t, f)
	newSafegit(t, "0.31.0")
	gh(t, listed("v0.1.0"))
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "no record explains where they went", missing, "safegit's rewrite journal")

	writeJournal(t, f.repo, "rw1", map[string]string{missing: f.h.R}, true)
	requireExit(t, reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false), 0)
}

func TestAnUnversionedTagIsNotJudged(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, true)
	f.repo.Git("push", "-q", "origin", f.h.B+":refs/tags/nightly")
	f.repo.Git("tag", "nightly", f.h.A)
	gh(t, listed("v0.1.0"))
	r := reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "refs/tags/nightly")

	f.repo.Write(".strictmetadata/lifecycle-and-license/lifecycle-and-license.toml", "format_version = 1\n\n[[unversioned_tags]]\ntag = \"nightly\"\nreason = \"a moving marker CI publishes\"\nrecorded = 2026-01-01\n")
	r = reconcile(t, f.repo.Dir, "", historyrewrite.ReconcilePlan, false)
	requireExit(t, r, 0)
	if !strings.Contains(r.Stdout, "outside the version model") {
		t.Fatalf("the unversioned tag was not reported as skipped:\n%s", r.Stdout)
	}
}

const widgetDeclarations = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "widget"
tag_format = "{name}@v{version}"
publish_mode = "ci"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "widget"
name = "widget"
releasable = "widget"
targets = [{ name = "go" }]
`

func TestAMovedCompanionTagLeavesTheReleasesAlone(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	files := map[string]string{declarationsPath: widgetDeclarations, "widget/README": "widget\n"}
	for _, m := range manifests {
		files[m] = "owner = \"rlsbl\"\n"
	}
	a := commitAll(t, repo, files, "the workspace")
	release := commitAll(t, repo, map[string]string{".strictmetadata/changelog/widget/0.1.0.jsonl": entryLine(1, a)}, "widget: release v0.1.0")
	tree := repo.Git("rev-parse", release+":widget")
	commitAll(t, repo, map[string]string{".strictmetadata/releases/widget/v0.1.0.toml": "format_version = 2\nbump = \"minor\"\ninclude = [\"go\"]\nexclude = []\ndescription = \"The first release\"\nrelease_commit = \"" + release + "\"\n\n[released_trees]\n\"widget\" = \"" + tree + "\"\n"}, "archive")
	repo.Git("tag", "widget@v0.1.0", release)
	repo.Git("tag", "widget/v0.1.0", release)
	bare := repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	// Both tags sit on origin at the commit a recorded rewrite replaced.
	repo.Git("push", "-q", "origin", a+":refs/tags/widget@v0.1.0", a+":refs/tags/widget/v0.1.0")
	writeJournal(t, repo, "rw1", map[string]string{a: release}, true)
	body := []string{"release", "view", "widget@v0.1.0", "--repo", "acme/portal", "--json", "body", "--jq", ".body"}
	edit := []string{"release", "edit", "widget@v0.1.0", "--repo", "acme/portal", "--notes-file", "-"}
	fake := testsupport.FakeGH(t, append(append([]testsupport.GHAnswer(nil), ghLoggedIn...),
		testsupport.GHAnswer{Args: ghList, Stdout: "widget@v0.1.0\n"},
		testsupport.GHAnswer{Args: body, Stdout: "notes\n\n<!-- rlsbl-ci-sha: " + a + " -->"},
		testsupport.GHAnswer{Args: edit})...)

	requireExit(t, reconcile(t, repo.Dir, "widget", historyrewrite.ReconcilePlan, false), 0)
	requireExit(t, reconcile(t, repo.Dir, "widget", historyrewrite.ReconcileApply, false), 0)
	refs := testsupport.Refs(t, bare)
	if refs["refs/tags/widget@v0.1.0"] != release || refs["refs/tags/widget/v0.1.0"] != release {
		t.Fatalf("origin holds %v", refs)
	}
	for _, c := range fake.Calls() {
		for _, arg := range c.Args {
			if arg == "widget/v0.1.0" {
				t.Fatalf("the companion tag's move touched a Release: %q", c.Args)
			}
		}
	}
}

func TestTheReleasableIsSelectedByTheDirectoryOrNamedWhereItSelectsNone(t *testing.T) {
	hygiene.Isolate(t)
	standalone := testsupport.NewRepo(t)
	standalone.CommitFile(declarationsPath, portalDeclarations, "the project")
	ws, err := workspace.Load(standalone.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := historyrewrite.SelectReleasable(ws, standalone.Dir, "portal"); err == nil || !strings.Contains(err.Error(), "--releasable is refused here") {
		t.Fatalf("naming the releasable the directory selects was not refused: %v", err)
	}
	if rel, err := historyrewrite.SelectReleasable(ws, standalone.Dir, ""); err != nil || rel.Name != "portal" {
		t.Fatalf("without the selector: %v, %v", rel, err)
	}

	multi := testsupport.NewRepo(t)
	multi.Write("widget/README", "widget\n")
	multi.Commit("the workspace", "widget/README")
	multi.CommitFile(declarationsPath, widgetDeclarations, "declarations")
	ws, err = workspace.Load(multi.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := historyrewrite.SelectReleasable(ws, multi.Dir, ""); err == nil || !strings.Contains(err.Error(), "--releasable <name>: widget") {
		t.Fatalf("the workspace root selected a releasable: %v", err)
	}
	if _, err := historyrewrite.SelectReleasable(ws, multi.Dir, "gadget"); err == nil || !strings.Contains(err.Error(), "no releasable is named \"gadget\"") {
		t.Fatalf("an unknown releasable was not refused: %v", err)
	}
	if rel, err := historyrewrite.SelectReleasable(ws, multi.Dir, "widget"); err != nil || rel.Name != "widget" {
		t.Fatalf("naming the releasable: %v, %v", rel, err)
	}
	if rel, err := historyrewrite.SelectReleasable(ws, multi.Path("widget"), ""); err != nil || rel.Name != "widget" {
		t.Fatalf("from the member's directory: %v, %v", rel, err)
	}
}
