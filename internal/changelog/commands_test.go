package changelog_test

import (
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// portalRepo is a standalone repository releasing portal, its declarations
// committed, and one commit changing a file, returned as change.
func portalRepo(t *testing.T) (repo *testsupport.Repo, change string) {
	t.Helper()
	repo = testsupport.NewRepo(t)
	repo.Write(declarations.ReleasablesFile, standalone)
	// A scaffolded repository commits the run-state directory's .gitignore,
	// so the lock a command takes leaves nothing untracked.
	repo.Write(declarations.ReleaseStateDir+"/.gitignore", "*\n!.gitignore\n")
	repo.Commit("the project", declarations.ReleasablesFile, declarations.ReleaseStateDir+"/.gitignore")
	change = repo.CommitFile("a.txt", "a\n", "a change")
	return repo, change
}

// invoke runs fn with the changelog of the releasable dir selects, in a
// mutating command, and returns the dispatch's result.
func invoke(t *testing.T, dir string, dryRun bool, fn func(inv changelog.Invocation, s changelog.Subject) error) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		s, err := changelog.OpenAt(ctx.Effects(), dir)
		if err != nil {
			return err
		}
		return fn(changelog.Invocation{E: ctx.Effects(), DryRun: ctx.DryRun(), Say: ctx.Out}, s)
	})
}

func requireExit(t *testing.T, r strictcli.Result, code int) {
	t.Helper()
	if r.ExitCode != code {
		t.Fatalf("exit %d, want %d\nstdout:\n%s\nstderr:\n%s", r.ExitCode, code, r.Stdout, r.Stderr)
	}
}

func add(t *testing.T, dir string, req changelog.EntryRequest) strictcli.Result {
	t.Helper()
	return invoke(t, dir, false, func(inv changelog.Invocation, s changelog.Subject) error {
		return inv.Add(s, changelog.AddRequest{Entry: req, AutoCommit: true})
	})
}

func status(t *testing.T, repo *testsupport.Repo) string {
	t.Helper()
	return repo.Git("status", "--porcelain")
}

func TestAddAppendsTheEntryAndCommitsIt(t *testing.T) {
	hygiene.Isolate(t)
	repo, change := portalRepo(t)
	testsupport.FakeSafegit(t)
	r := add(t, repo.Dir, changelog.EntryRequest{Commits: []string{change[:9]}, UserFacing: true, Description: "Did a thing", Type: changelog.TypeFeature})
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "Added entry ")
	text := readText(t, repo.Path(changelog.Dir("portal")+"/unreleased.jsonl"))
	requireContains(t, text, `"commits":["`+change+`"]`, `"description":"Did a thing"`)
	if got := repo.Git("log", "-1", "--format=%s"); got != "changelog: Did a thing" {
		t.Errorf("commit subject %q", got)
	}
	if s := status(t, repo); s != "" {
		t.Errorf("the add left uncommitted changes:\n%s", s)
	}
}

func TestAddUnderDryRunWritesAndCommitsNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo, change := portalRepo(t)
	testsupport.FakeSafegit(t)
	head := repo.Head()
	r := invoke(t, repo.Dir, true, func(inv changelog.Invocation, s changelog.Subject) error {
		return inv.Add(s, changelog.AddRequest{Entry: changelog.EntryRequest{Commits: []string{change}}, AutoCommit: true})
	})
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "Would add to", "Would commit")
	if _, err := os.Stat(repo.Path(changelog.Dir("portal"))); !os.IsNotExist(err) {
		t.Errorf("a dry run created the changelog directory (%v)", err)
	}
	if repo.Head() != head {
		t.Error("a dry run committed")
	}
}

func TestAnUncommittedAddNamesTheCommitThatRecordsIt(t *testing.T) {
	hygiene.Isolate(t)
	repo, change := portalRepo(t)
	testsupport.PathOnly(t, "git", "sh")
	r := add(t, repo.Dir, changelog.EntryRequest{Commits: []string{change}, UserFacing: true, Description: "It's done", Type: changelog.TypeFix})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "is written but not committed", "safegit commit -m ")
	_, command, ok := strings.Cut(r.Stderr, "commit what was written: ")
	if !ok {
		t.Fatalf("no command named:\n%s", r.Stderr)
	}
	command = strings.TrimSpace(command)

	testsupport.FakeSafegit(t)
	cmd := exec.Command("sh", "-c", command)
	cmd.Dir = repo.Dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("the named command failed: %v\n%s", err, out)
	}
	if s := status(t, repo); s != "" {
		t.Errorf("the named command left changes uncommitted:\n%s", s)
	}
	if got := repo.Git("log", "-1", "--format=%s"); got != "changelog: It's done" {
		t.Errorf("commit subject %q", got)
	}
}

func TestGenerateNeedsAChangelogDirectoryAndCommitsWithTheTrailer(t *testing.T) {
	hygiene.Isolate(t)
	repo, change := portalRepo(t)
	testsupport.FakeSafegit(t)
	generate := func() strictcli.Result {
		return invoke(t, repo.Dir, false, func(inv changelog.Invocation, s changelog.Subject) error { return inv.Generate(s, true) })
	}
	r := generate()
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "has no changelog directory", "rlsbl changelog add")

	requireExit(t, add(t, repo.Dir, changelog.EntryRequest{Commits: []string{change}, UserFacing: true, Description: "Did a thing", Type: changelog.TypeFeature}), 0)
	r = generate()
	requireExit(t, r, 0)
	requireContains(t, readText(t, repo.Path("CHANGELOG.md")), "## Unreleased", "- Did a thing")
	if got := repo.Git("log", "-1", "--format=%(trailers:key=Autogenerated,valueonly)"); strings.TrimSpace(got) != "true" {
		t.Errorf("the generation commit carries no Autogenerated trailer: %q", got)
	}
	r = generate()
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "nothing was written")
}

// releasedPortal is portalRepo with release 0.1.0 of the change recorded:
// its read-only changelog file and its archive, with the given fate.
func releasedPortal(t *testing.T, fate string) (*testsupport.Repo, string) {
	t.Helper()
	repo, change := portalRepo(t)
	rel := writeReleased(t, repo.Dir, "portal", "0.1.0", feature("1", "First", change))
	paths := []string{rel}
	if fate != "" {
		writeArchive(t, repo.Dir, "portal", "0.1.0", fate, change, "The first release")
		paths = append(paths, ".strictmetadata/releases/portal/v0.1.0.toml")
	}
	repo.Commit("release 0.1.0", paths...)
	return repo, change
}

func amend(t *testing.T, repo *testsupport.Repo, commits []string) (strictcli.Result, changelog.Outcome) {
	t.Helper()
	var outcome changelog.Outcome
	r := invoke(t, repo.Dir, false, func(inv changelog.Invocation, s changelog.Subject) error {
		var err error
		outcome, err = inv.Amend(s, changelog.AmendRequest{Version: version(t, "0.1.0"), Entry: changelog.EntryRequest{Commits: commits, UserFacing: true, Description: "Also this", Type: changelog.TypeFix}})
		return err
	})
	return r, outcome
}

func TestAmendAppendsToTheReleasedFileAndNamesTheReleaseToRewrite(t *testing.T) {
	hygiene.Isolate(t)
	repo, change := releasedPortal(t, "recorded")
	testsupport.FakeSafegit(t)
	more := repo.CommitFile("b.txt", "b\n", "more")
	r, outcome := amend(t, repo, []string{more})
	requireExit(t, r, 0)
	released := repo.Path(changelog.Dir("portal") + "/0.1.0.jsonl")
	text := readText(t, released)
	requireContains(t, text, change, more, "Also this")
	if mode(t, released) != 0o444 {
		t.Errorf("the released file is mode %o", mode(t, released))
	}
	requireContains(t, readText(t, repo.Path("CHANGELOG.md")), "- Also this")
	if outcome.Resync == nil || outcome.Resync.String() != "0.1.0" {
		t.Errorf("outcome %+v", outcome)
	}
	if got := repo.Git("log", "-1", "--format=%s"); got != "changelog: amend 0.1.0: Also this" {
		t.Errorf("commit subject %q", got)
	}
	if s := status(t, repo); s != "" {
		t.Errorf("uncommitted:\n%s", s)
	}
}

func TestAmendRefusesAVersionWithoutAnArchiveUntilOneIsWritten(t *testing.T) {
	hygiene.Isolate(t)
	repo, change := releasedPortal(t, "")
	testsupport.FakeSafegit(t)
	before := readText(t, repo.Path(changelog.Dir("portal")+"/0.1.0.jsonl"))
	r, _ := amend(t, repo, []string{change})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "no release archive", "rlsbl release backfill")
	if readText(t, repo.Path(changelog.Dir("portal")+"/0.1.0.jsonl")) != before {
		t.Fatal("a refused amend wrote the released file")
	}
	// The backfill writes the archive the refusal asks for.
	writeArchive(t, repo.Dir, "portal", "0.1.0", "recorded", change, "The first release")
	repo.Commit("backfill 0.1.0", ".strictmetadata/releases/portal/v0.1.0.toml")
	more := repo.CommitFile("b.txt", "b\n", "more")
	r, _ = amend(t, repo, []string{more})
	requireExit(t, r, 0)
}

func TestAmendOfANeverReleasedVersionNamesNoRelease(t *testing.T) {
	hygiene.Isolate(t)
	repo, _ := releasedPortal(t, "never-released")
	testsupport.FakeSafegit(t)
	more := repo.CommitFile("b.txt", "b\n", "more")
	r, outcome := amend(t, repo, []string{more})
	requireExit(t, r, 0)
	if outcome.Resync != nil {
		t.Errorf("a never-released version has no Release to rewrite: %+v", outcome)
	}
}

// twoEntriesOfOneCommit is portalRepo with a feature and a fix entry naming
// the same commit in the unreleased file, committed.
func twoEntriesOfOneCommit(t *testing.T) (*testsupport.Repo, string) {
	t.Helper()
	repo, change := portalRepo(t)
	fix := changelog.Entry{ID: id("2"), Commits: []string{change}, UserFacing: true, Description: "A fix", Type: changelog.TypeFix}
	rel := changelog.Dir("portal") + "/unreleased.jsonl"
	writeLines(t, repo.Dir, rel, feature("1", "A feature", change), fix)
	repo.Commit("entries", rel)
	return repo, change
}

func editEntry(t *testing.T, repo *testsupport.Repo, cmd changelog.EditCommand) strictcli.Result {
	t.Helper()
	return invoke(t, repo.Dir, false, func(inv changelog.Invocation, s changelog.Subject) error {
		_, err := inv.Edit(s, cmd)
		return err
	})
}

func TestEditNarrowsByTypeAndRefusesAnAmbiguousSelectorUntilTheIDIsNamed(t *testing.T) {
	hygiene.Isolate(t)
	repo, change := twoEntriesOfOneCommit(t)
	testsupport.FakeSafegit(t)
	text := "Now described"
	r := editEntry(t, repo, changelog.EditCommand{Selector: changelog.Selector{Commits: []string{change}}, Edit: changelog.EditRequest{Description: &text}, AutoCommit: true})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "matches 2 entries", id("1"), id("2"), "rlsbl changelog edit --id <id>")

	requireExit(t, editEntry(t, repo, changelog.EditCommand{Selector: changelog.Selector{ID: id("2")}, Edit: changelog.EditRequest{Description: &text}, AutoCommit: true}), 0)
	requireContains(t, readText(t, repo.Path(changelog.Dir("portal")+"/unreleased.jsonl")), `"description":"Now described","type":"fix"`)

	fix := changelog.TypeFix
	other := "By type"
	requireExit(t, editEntry(t, repo, changelog.EditCommand{Selector: changelog.Selector{Commits: []string{change}}, Edit: changelog.EditRequest{Description: &other, Type: &fix}, AutoCommit: true}), 0)
	requireContains(t, readText(t, repo.Path(changelog.Dir("portal")+"/unreleased.jsonl")), `"description":"By type","type":"fix"`, `"description":"A feature"`)
	if got := repo.Git("log", "-1", "--format=%s"); got != "changelog: edit unreleased: By type" {
		t.Errorf("commit subject %q", got)
	}
}

func remove(t *testing.T, repo *testsupport.Repo, sel changelog.Selector) strictcli.Result {
	t.Helper()
	return invoke(t, repo.Dir, false, func(inv changelog.Invocation, s changelog.Subject) error {
		_, err := inv.Remove(s, changelog.RemoveCommand{Selector: sel, AutoCommit: true})
		return err
	})
}

func TestRemoveRefusesSeveralMatchesUntilTheIDIsNamed(t *testing.T) {
	hygiene.Isolate(t)
	repo, change := twoEntriesOfOneCommit(t)
	testsupport.FakeSafegit(t)
	r := remove(t, repo, changelog.Selector{Commits: []string{change}})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "matches 2 entries", "rlsbl changelog remove --id <id>")
	requireExit(t, remove(t, repo, changelog.Selector{ID: id("1")}), 0)
	text := readText(t, repo.Path(changelog.Dir("portal")+"/unreleased.jsonl"))
	if strings.Contains(text, id("1")) || !strings.Contains(text, id("2")) {
		t.Errorf("after the removal:\n%s", text)
	}
	if got := repo.Git("log", "-1", "--format=%s"); got != "changelog: remove from unreleased: A feature" {
		t.Errorf("commit subject %q", got)
	}
}

func TestRemoveMatchesACommitThatNoLongerResolves(t *testing.T) {
	hygiene.Isolate(t)
	repo, _ := portalRepo(t)
	testsupport.FakeSafegit(t)
	gone := strings.Repeat("d", 40)
	rel := changelog.Dir("portal") + "/unreleased.jsonl"
	writeLines(t, repo.Dir, rel, internalEntry("1", gone))
	repo.Commit("entries", rel)
	requireExit(t, remove(t, repo, changelog.Selector{Commits: []string{gone}}), 0)
	if text := readText(t, repo.Path(rel)); text != "" {
		t.Errorf("after the removal: %q", text)
	}
}

func TestARemovalFromAReleasedFileNamesTheReleaseToRewrite(t *testing.T) {
	hygiene.Isolate(t)
	repo, _ := releasedPortal(t, "recorded")
	testsupport.FakeSafegit(t)
	var outcome changelog.Outcome
	r := invoke(t, repo.Dir, false, func(inv changelog.Invocation, s changelog.Subject) error {
		var err error
		outcome, err = inv.Remove(s, changelog.RemoveCommand{Selector: changelog.Selector{ID: id("1")}, AutoCommit: true})
		return err
	})
	requireExit(t, r, 0)
	released := repo.Path(changelog.Dir("portal") + "/0.1.0.jsonl")
	if readText(t, released) != "" || mode(t, released) != 0o444 {
		t.Errorf("released file (mode %o): %q", mode(t, released), readText(t, released))
	}
	if outcome.Resync == nil || outcome.Resync.String() != "0.1.0" {
		t.Errorf("outcome %+v", outcome)
	}
	requireContains(t, readText(t, repo.Path("CHANGELOG.md")), "## 0.1.0", "No user-facing changes")
}

func remapAll(t *testing.T, repo *testsupport.Repo, sources ...changelog.MapSource) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		g, err := git.Open(ctx.Effects(), repo.Dir)
		if err != nil {
			return err
		}
		return changelog.Invocation{E: ctx.Effects(), Say: ctx.Out}.RemapAll(g, sources)
	})
}

func TestRemapCollapsesTheCommitsAFoldMapsToOne(t *testing.T) {
	hygiene.Isolate(t)
	repo, _ := portalRepo(t)
	testsupport.FakeSafegit(t)
	a, b, squash := strings.Repeat("a", 40), strings.Repeat("b", 40), strings.Repeat("5", 40)
	rel := changelog.Dir("portal") + "/unreleased.jsonl"
	writeLines(t, repo.Dir, rel, feature("1", "Both", a, b))
	repo.Commit("entries", rel)
	r := remapAll(t, repo, changelog.MapSource{Label: "the squash", Map: map[string]string{a: squash, b: squash}})
	requireExit(t, r, 0)
	if got := readText(t, repo.Path(rel)); got != line(feature("1", "Both", squash))+"\n" {
		t.Errorf("after the remap:\n%s", got)
	}
	if got := repo.Git("log", "-1", "--format=%s%n%(trailers:key=Autogenerated,valueonly)"); !strings.HasPrefix(got, "changelog: remap stale commit hashes\ntrue") {
		t.Errorf("commit %q", got)
	}
}

func TestRemapRefusesSourcesThatDisagreeOrHoldNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo, _ := portalRepo(t)
	old := strings.Repeat("a", 40)
	r := remapAll(t, repo,
		changelog.MapSource{Label: "the map file", Map: map[string]string{old: strings.Repeat("1", 40)}},
		changelog.MapSource{Label: "standard input", Map: map[string]string{old: strings.Repeat("2", 40)}})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "the map file maps", "standard input maps it to", "Remap from one source")
	r = remapAll(t, repo, changelog.MapSource{Label: "standard input", Map: map[string]string{}})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "standard input holds no mapping")
}

func TestRemapRefusesAnAbbreviationTheMapCannotDecideUntilTheFullIDIsWritten(t *testing.T) {
	hygiene.Isolate(t)
	repo, _ := portalRepo(t)
	testsupport.FakeSafegit(t)
	one, two := "c"+strings.Repeat("0", 39), "c"+strings.Repeat("0", 38)+"1"
	rewrites := map[string]string{one: strings.Repeat("3", 40), two: strings.Repeat("4", 40)}
	rel := changelog.Dir("portal") + "/unreleased.jsonl"
	writeLines(t, repo.Dir, rel, internalEntry("1", "c000000"))
	repo.Commit("entries", rel)
	r := remapAll(t, repo, changelog.MapSource{Label: "the map file", Map: rewrites})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, rel+": c000000", "full commit id")
	if got := readText(t, repo.Path(rel)); got != line(internalEntry("1", "c000000"))+"\n" {
		t.Fatalf("a refused remap wrote:\n%s", got)
	}
	// The fix: the full id written into the line by hand.
	writeLines(t, repo.Dir, rel, internalEntry("1", two))
	repo.Commit("full id", rel)
	requireExit(t, remapAll(t, repo, changelog.MapSource{Label: "the map file", Map: rewrites}), 0)
	if got := readText(t, repo.Path(rel)); got != line(internalEntry("1", strings.Repeat("4", 40)))+"\n" {
		t.Errorf("after the remap:\n%s", got)
	}
}

func TestParseVersionArgumentRefusesALeadingV(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := changelog.ParseVersionArgument("v0.1.0", "--version"); err == nil || !strings.Contains(err.Error(), "name the version bare: 0.1.0") {
		t.Fatalf("err %v", err)
	}
	if v, err := changelog.ParseVersionArgument("0.1.0", "--version"); err != nil || v.String() != "0.1.0" {
		t.Fatalf("%v %v", v, err)
	}
	if _, err := changelog.SplitCommits(" , ", "--commits"); err == nil {
		t.Fatal("an empty commit list was accepted")
	}
}
