package historyrewrite_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/historyrewrite"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

var scrubTime = time.Date(2026, 3, 4, 5, 6, 7, 0, time.UTC)

func scrubNow() time.Time { return scrubTime }

// mangleSecret is the scrub the tests run: every match of SECRET mangled
// across the whole history.
var mangleSecret = historyrewrite.ScrubRequest{Mode: historyrewrite.ModePattern, Pattern: "SECRET", Mangle: true, EntireHistory: true, Reason: "a token was committed"}

func scrub(t *testing.T, dir string, req historyrewrite.ScrubRequest, dryRun bool) strictcli.Result {
	t.Helper()
	if req.IndexPath == "" {
		req.IndexPath = filepath.Join(t.TempDir(), "confidential-names.toml")
	}
	return run(t, dryRun, func(ctx *strictcli.Context) error {
		return historyrewrite.Scrub(ctx, dir, req, scrubNow)
	})
}

// releasePublished answers the Release rewrite of v0.1.0, which exists.
func releasePublished(t *testing.T) *testsupport.GH {
	t.Helper()
	return gh(t, testsupport.GHAnswer{Args: ghExists, Stdout: "v0.1.0"}, testsupport.GHAnswer{Args: ghRewrite})
}

// prune removes the history the rewrite replaced from the object store: the
// remote-tracking ref and ORIG_HEAD that still name it, then its reflog
// entries and the objects themselves, as the prune an operator runs does.
func (f *scrubFixture) prune(t *testing.T) {
	t.Helper()
	f.repo.Git("update-ref", "-d", "refs/remotes/origin/main")
	f.repo.GitResult("update-ref", "-d", "ORIG_HEAD")
	f.repo.Git("reflog", "expire", "--expire=now", "--all")
	f.repo.Git("gc", "-q", "--prune=now")
	if _, _, code := f.repo.GitResult("cat-file", "-e", f.old.S); code == 0 {
		t.Fatal("the prune left the replaced history in the object store")
	}
}

func TestAScrubRewritesRepairsCommitsAndPublishes(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	sg := newSafegit(t, "0.31.0")
	sg.Answer(f.rewriteScript(), safegitDocument(t, f.rewritePayload(true)), 0)
	fakeGH := releasePublished(t)

	r := scrub(t, f.repo.Dir, mangleSecret, false)
	requireExit(t, r, 0)

	scrubs := sg.Scrubs()
	if len(scrubs) != 1 {
		t.Fatalf("safegit scrubbed %d times, want once: %q", len(scrubs), scrubs)
	}
	for _, want := range []string{"scrub match --json", "--pattern SECRET", "--mangle", "--entire-history", "--remap-shas-in .strictmetadata/changelog/*/*.jsonl", "--remap-shas-in .strictmetadata/retired-release-histories/*/changelog/*.jsonl", "--reason a token was committed"} {
		if !strings.Contains(scrubs[0], want) {
			t.Fatalf("the safegit invocation %q lacks %q", scrubs[0], want)
		}
	}
	// The archive's release commit moved through the rewrite, recording the
	// rewritten tree.
	archive := readFile(t, f.repo, archivePath)
	if !strings.Contains(archive, `release_commit = "`+f.new.R+`"`) || !strings.Contains(archive, f.repo.Git("rev-parse", f.new.R+"^{tree}")) {
		t.Fatalf("the archive was not moved to the rewritten release commit and its tree:\n%s", archive)
	}
	if !strings.Contains(readFile(t, f.repo, transitionsPath), `"event":"release-commit-remap"`) {
		t.Fatal("the move was not recorded in the transition record")
	}
	// The rewrite's archive names commits and tags only.
	rewriteArchive := readFile(t, f.repo, historyRewritesDir+"/20260304T050607Z.toml")
	for _, want := range []string{`operation = "scrub"`, `mode = "pattern"`, `reason = "a token was committed"`, f.old.B, f.new.B, "refs/tags/v0.1.0"} {
		if !strings.Contains(rewriteArchive, want) {
			t.Fatalf("the rewrite archive lacks %q:\n%s", want, rewriteArchive)
		}
	}
	if strings.Contains(rewriteArchive, "SECRET") {
		t.Fatalf("the rewrite archive carries what was scrubbed:\n%s", rewriteArchive)
	}
	// One commit on the rewritten head carries the records, and nothing is
	// left uncommitted.
	if parent := f.repo.Git("rev-parse", "HEAD^"); parent != f.new.S {
		t.Fatalf("the scrub commit's parent is %s, want the rewritten head %s", parent, f.new.S)
	}
	if !strings.Contains(f.repo.Git("log", "-1", "--format=%B"), "Scrub-remap: "+f.old.S+".."+f.new.S) {
		t.Fatal("the scrub commit carries no Scrub-remap trailer")
	}
	if status := f.repo.Git("status", "--porcelain", "--", ".strictmetadata/releases", ".strictmetadata/transitions", historyRewritesDir); status != "" {
		t.Fatalf("records left uncommitted:\n%s", status)
	}
	// The branch and the tag were force-pushed, and the scrub result is gone.
	refs := testsupport.Refs(t, f.bare)
	if refs["refs/heads/main"] != f.repo.Head() || refs["refs/tags/v0.1.0"] != f.new.R {
		t.Fatalf("origin holds %v", refs)
	}
	if exists(f.repo, scrubResultPath) {
		t.Fatal("the scrub result was left behind")
	}
	// The Release was rewritten in place from the record, naming the
	// rewritten release commit.
	var edited bool
	for _, c := range fakeGH.Calls() {
		if strings.Join(c.Args, " ") == strings.Join(ghRewrite, " ") {
			edited = strings.Contains(c.Stdin, "<!-- rlsbl-ci-sha: "+f.new.R+" -->")
		}
		if len(c.Args) > 1 && c.Args[0] == "release" && c.Args[1] == "delete" {
			t.Fatal("a Release was deleted")
		}
	}
	if !edited {
		t.Fatalf("the Release was not rewritten with the rewritten release commit: %+v", fakeGH.Calls())
	}
}

func TestADryRunRecordsTheRewriteAndWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	sg := newSafegit(t, "0.31.0")
	head := f.repo.Head()

	r := scrub(t, f.repo.Dir, mangleSecret, true)
	requireExit(t, r, 0)
	if scrubs := sg.Scrubs(); len(scrubs) != 0 {
		t.Fatalf("a dry run ran safegit's scrub: %q", scrubs)
	}
	if !strings.Contains(r.Stdout+r.Stderr, "--dry-run") {
		t.Fatalf("the preview does not name the safegit invocation that shows its counts:\n%s%s", r.Stdout, r.Stderr)
	}
	if f.repo.Head() != head || exists(f.repo, scrubResultPath) {
		t.Fatal("a dry run changed the repository")
	}
}

func TestAnOlderSafegitIsRefusedUntilTheReleaseIsInstalled(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	sg := newSafegit(t, "0.30.0")
	r := scrub(t, f.repo.Dir, mangleSecret, true)
	requireExit(t, r, 1)
	requireStderr(t, r, "safegit 0.30.0 is installed", "0.31.0 or newer")

	sg.write("version.txt", "safegit 0.31.0+dirty\n")
	requireExit(t, scrub(t, f.repo.Dir, mangleSecret, true), 0)
}

func TestAnEnvelopeOfAnotherInterfaceVersionIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	sg := newSafegit(t, "0.31.0")
	sg.Answer("", `{"interface_version": 2, "payload": null}`, 0)
	r := scrub(t, f.repo.Dir, mangleSecret, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "interface_version 2", "interface_version 3")
}

func TestAStoppedReleaseRefusesTheScrubUntilItIsFinished(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	newSafegit(t, "0.31.0")
	state := ".strictmetadata/.release-state/portal/in-progress.toml"
	f.repo.Write(state, "format_version = 1\n")
	r := scrub(t, f.repo.Dir, mangleSecret, true)
	requireExit(t, r, 1)
	requireStderr(t, r, "a release is stopped mid-flight", state, "rlsbl release abandon")

	// Giving the release up removes its state, which clears the refusal.
	if err := os.Remove(f.repo.Path(state)); err != nil {
		t.Fatal(err)
	}
	requireExit(t, scrub(t, f.repo.Dir, mangleSecret, true), 0)
}

func TestAScrubOffTheReleaseBranchIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	newSafegit(t, "0.31.0")
	f.repo.Git("checkout", "-q", "-b", "topic")
	r := scrub(t, f.repo.Dir, mangleSecret, true)
	requireExit(t, r, 1)
	requireStderr(t, r, "topic is not a release branch", "check out a release branch")

	f.repo.Git("checkout", "-q", "main")
	requireExit(t, scrub(t, f.repo.Dir, mangleSecret, true), 0)
}

func TestARecipeThatIsNoFileIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	newSafegit(t, "0.31.0")
	recipe := f.repo.Path("experiments/recipe.toml")
	req := historyrewrite.ScrubRequest{Mode: historyrewrite.ModeRecipe, Recipe: recipe, EntireHistory: true, Reason: "a token"}
	r := scrub(t, f.repo.Dir, req, true)
	requireExit(t, r, 1)
	requireStderr(t, r, "is not a file")

	f.repo.Write("experiments/recipe.toml", "[[operations]]\n")
	requireExit(t, scrub(t, f.repo.Dir, req, true), 0)
}

func TestAPushOriginRefusesIsResumedByRunningAgain(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	sg := newSafegit(t, "0.31.0")
	sg.Answer(f.rewriteScript(), safegitDocument(t, f.rewritePayload(true)), 0)
	releasePublished(t)
	// Origin refuses every push until the hook is removed.
	hook := f.bare + "/hooks/pre-receive"
	testsupport.WriteFile(t, hook, "#!/bin/sh\necho refused >&2\nexit 1\n")
	if err := os.Chmod(hook, 0o755); err != nil {
		t.Fatal(err)
	}

	r := scrub(t, f.repo.Dir, mangleSecret, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "origin refused the push of refs/heads/main", "run the same command again")
	if !exists(f.repo, scrubResultPath) {
		t.Fatal("the scrub result was not kept for the resume")
	}

	if err := os.Remove(hook); err != nil {
		t.Fatal(err)
	}
	r = scrub(t, f.repo.Dir, mangleSecret, false)
	requireExit(t, r, 0)
	if len(sg.Scrubs()) != 1 {
		t.Fatalf("the resume ran safegit's rewrite again: %q", sg.Scrubs())
	}
	refs := testsupport.Refs(t, f.bare)
	if refs["refs/heads/main"] != f.repo.Head() || refs["refs/tags/v0.1.0"] != f.new.R {
		t.Fatalf("origin holds %v after the resume", refs)
	}
}

func TestARerunWithOtherArgumentsDoesNotFinishASavedScrub(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	sg := newSafegit(t, "0.31.0")
	sg.Answer(f.rewriteScript(), safegitDocument(t, f.rewritePayload(true)), 0)
	hook := f.bare + "/hooks/pre-receive"
	testsupport.WriteFile(t, hook, "#!/bin/sh\nexit 1\n")
	if err := os.Chmod(hook, 0o755); err != nil {
		t.Fatal(err)
	}
	requireExit(t, scrub(t, f.repo.Dir, mangleSecret, false), 1)

	other := mangleSecret
	other.Pattern = "OTHER"
	r := scrub(t, f.repo.Dir, other, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "holds a rewrite made by `safegit", "with the arguments the saved scrub was started with")

	// Running it with the saved arguments finishes it.
	if err := os.Remove(hook); err != nil {
		t.Fatal(err)
	}
	releasePublished(t)
	requireExit(t, scrub(t, f.repo.Dir, mangleSecret, false), 0)
}

func TestAStaleScrubResultIsRefusedUntilRemoved(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	newSafegit(t, "0.31.0")
	f.repo.Write(scrubResultPath, `{"mode":"pattern","reason":"x","started_at":"2026-01-01T00:00:00Z","safegit_args":[],"rewrites":{},"tags":[],"commits_rewritten":0,"old_head":"","new_head":"`+strings.Repeat("a", 40)+`","cleanup_ok":true,"cleanup_errors":[],"remote_refs":{},"unverified":false,"completed_steps":[],"remapped_files":[],"release_commit_files":[],"deleted_caches":[],"archive_path":"","releases_written":0}`)
	r := scrub(t, f.repo.Dir, mangleSecret, true)
	requireExit(t, r, 1)
	requireStderr(t, r, "holds a scrub whose rewrite left HEAD at", "remove .strictmetadata/.release-state/scrub-result.json")

	if err := os.Remove(f.repo.Path(scrubResultPath)); err != nil {
		t.Fatal(err)
	}
	requireExit(t, scrub(t, f.repo.Dir, mangleSecret, true), 0)
}

func TestARewriteSafegitFailedAfterIsConfirmedThenFinished(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	sg := newSafegit(t, "0.31.0")
	// safegit rewrites, then fails its own verification.
	sg.Answer(f.rewriteScript(), safegitDocument(t, f.rewritePayload(false)), 1)

	r := scrub(t, f.repo.Dir, mangleSecret, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "safegit rewrote the local history, then failed", "git reflog expire --expire=now --all && git gc --prune=now", "run this same command again")
	if !strings.Contains(readFile(t, f.repo, scrubResultPath), `"unverified": true`) {
		t.Fatal("the rewrite was not saved for the confirmation")
	}
	if refs := testsupport.Refs(t, f.bare); refs["refs/heads/main"] != f.old.S {
		t.Fatal("something was pushed before the rewrite was confirmed")
	}

	// The prune is done: safegit, asked again, finds nothing more to rewrite
	// and confirms; the scrub finishes.
	f.prune(t)
	sg.Answer("", safegitDocument(t, map[string]any{"version": 1, "dry_run": false, "pattern": "SECRET", "commits_rewritten": 0, "old_head": f.new.S, "cleanup_ok": true}), 0)
	releasePublished(t)
	r = scrub(t, f.repo.Dir, mangleSecret, false)
	requireExit(t, r, 0)
	if len(sg.Scrubs()) != 2 {
		t.Fatalf("safegit was asked %d times, want twice", len(sg.Scrubs()))
	}
	if refs := testsupport.Refs(t, f.bare); refs["refs/tags/v0.1.0"] != f.new.R {
		t.Fatalf("origin holds %v", refs)
	}
}

func TestAFailedCleanupStopsTheScrubUntilThePruneIsDone(t *testing.T) {
	hygiene.Isolate(t)
	f := newScrubFixture(t)
	sg := newSafegit(t, "0.31.0")
	sg.Answer(f.rewriteScript(), safegitDocument(t, f.rewritePayload(false)), 0)

	r := scrub(t, f.repo.Dir, mangleSecret, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "did not succeed", "still exist", "complete the prune")
	if strings.Contains(f.repo.Git("log", "-1", "--format=%s"), "scrub:") {
		t.Fatal("the scrub committed before the prune")
	}

	// The prune clears the refusal: no replaced object exists any more.
	f.prune(t)
	releasePublished(t)
	requireExit(t, scrub(t, f.repo.Dir, mangleSecret, false), 0)
}

func TestNothingToRewriteRepairsTheChangelogFromTheJournal(t *testing.T) {
	hygiene.Isolate(t)
	// A rewrite made outside rlsbl left the changelog naming commits that are
	// gone; safegit's journal names where they went.
	repo := testsupport.NewRepo(t)
	h := buildHistory(t, repo, "notes")
	gone := strings.Repeat("d", 40)
	repo.Write(unreleasedLogPath, entryLine(2, gone))
	repo.Git("add", "-A")
	repo.Git("commit", "-q", "-m", "an entry naming a rewritten commit")
	repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	sg := newSafegit(t, "0.31.0")
	sg.Answer("", safegitDocument(t, nil), 0)

	r := scrub(t, repo.Dir, mangleSecret, false)
	requireExit(t, r, 1)
	requireStderr(t, r, "the scrub found nothing to rewrite", gone, "rlsbl changelog")

	// With the journal naming the move, the same scrub repairs and commits.
	writeJournal(t, repo, "rw1", map[string]string{gone: h.B}, true)
	r = scrub(t, repo.Dir, mangleSecret, false)
	requireExit(t, r, 0)
	if !strings.Contains(readFile(t, repo, unreleasedLogPath), h.B) {
		t.Fatal("the entry was not remapped from the journal")
	}
	if !strings.Contains(repo.Git("log", "-1", "--format=%s"), "repair changelog commit ids") {
		t.Fatal("the repair was not committed")
	}
}
