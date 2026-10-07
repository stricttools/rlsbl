package monorepo

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// needFilterRepo skips a test that rewrites history when git-filter-repo is
// not installed: the conversions refuse without it, which
// TestAConversionRefusesAMissingFilterRepo covers.
func needFilterRepo(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath(filterRepoProgram); err != nil {
		t.Skip("git-filter-repo is not installed")
	}
}

// converting runs fn as the handler of a mutating command, under --dry-run
// when dryRun, and returns the result with everything it said and printed.
func converting(t *testing.T, dryRun bool, fn func(ctx *strictcli.Context, say func(string)) error) (strictcli.Result, string) {
	t.Helper()
	var said []string
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		return fn(ctx, func(s string) { said = append(said, s) })
	})
	return r, strings.Join(said, "\n") + "\n" + r.Stdout + r.Stderr
}

// mustConvert is converting for a run that must succeed.
func mustConvert(t *testing.T, dryRun bool, fn func(ctx *strictcli.Context, say func(string)) error) string {
	t.Helper()
	r, text := converting(t, dryRun, fn)
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s", r.ExitCode, text)
	}
	return text
}

// mustRefuse is converting for a run that must fail with every one of
// wants in what it printed.
func mustRefuse(t *testing.T, dryRun bool, fn func(ctx *strictcli.Context, say func(string)) error, wants ...string) string {
	t.Helper()
	r, text := converting(t, dryRun, fn)
	if r.ExitCode == 0 {
		t.Fatalf("the run succeeded:\n%s", text)
	}
	for _, w := range wants {
		if !strings.Contains(text, w) {
			t.Errorf("the output lacks %q:\n%s", w, text)
		}
	}
	return text
}

// entryLine is one changelog line describing commits.
func entryLine(description string, commits ...string) string {
	return changelog.Serialize(changelog.Entry{ID: changelog.NewID(), Commits: commits, UserFacing: true, Type: changelog.TypeFeature, Description: description}) + "\n"
}

// archiveText is an archive recording a release from commit with the tree
// of each released path.
func archiveText(commit string, trees map[string]string) string {
	text := "format_version = 2\nbump = \"minor\"\ninclude = []\nexclude = []\ndescription = \"a release\"\nrelease_commit = \"" + commit + "\"\n\n[released_trees]\n"
	for _, p := range sortedKeys(trees) {
		text += "\"" + p + "\" = \"" + trees[p] + "\"\n"
	}
	return text
}

func TestParseCommitMapSeparatesThePrunedCommits(t *testing.T) {
	hygiene.Isolate(t)
	a, b, c := strings.Repeat("a", 40), strings.Repeat("b", 40), strings.Repeat("c", 40)
	m, pruned, err := parseCommitMap("old new\n" + a + " " + b + "\n" + c + " " + strings.Repeat("0", 40) + "\n\n")
	if err != nil {
		t.Fatal(err)
	}
	if len(m) != 1 || m[a] != b || len(pruned) != 1 || pruned[0] != c {
		t.Fatalf("map %v, pruned %v", m, pruned)
	}
	if _, _, err := parseCommitMap("old new\n" + a + " HEAD\n"); err == nil || !strings.Contains(err.Error(), "line 2") {
		t.Fatalf("an unreadable line: %v", err)
	}
}

func TestRelativeToMovesAPathBetweenMembers(t *testing.T) {
	hygiene.Isolate(t)
	for _, c := range []struct{ p, from, to, want string }{
		{"packages/widget", "packages/widget", ".", "."},
		{"packages/widget/cmd", "packages/widget", ".", "cmd"},
		{".", ".", "packages/gizmo", "packages/gizmo"},
		{"cmd", ".", "packages/gizmo", "packages/gizmo/cmd"},
		{"apps/gadget", "packages/widget", ".", "apps/gadget"},
	} {
		if got := relativeTo(c.p, c.from, c.to); got != c.want {
			t.Errorf("relativeTo(%q, %q, %q) = %q, want %q", c.p, c.from, c.to, got, c.want)
		}
	}
}

func TestCarryEntriesNarrowsAndDrops(t *testing.T) {
	hygiene.Isolate(t)
	kept := changelog.Entry{ID: changelog.NewID(), Commits: []string{"a1", "b2"}, UserFacing: false}
	gone := changelog.Entry{ID: changelog.NewID(), Commits: []string{"b2"}, UserFacing: false}
	out, dropped, narrowed, err := carryEntries([]changelog.Entry{kept, gone}, func(h string) (string, error) {
		if h == "a1" {
			return "c3", nil
		}
		return "", nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if dropped != 1 || narrowed != 1 || len(out) != 1 || strings.Join(out[0].Commits, ",") != "c3" || out[0].ID != kept.ID {
		t.Fatalf("out %+v, dropped %d, narrowed %d", out, dropped, narrowed)
	}
}

func TestARenderedRecordReadsBackAndMovesItsTagNamespace(t *testing.T) {
	hygiene.Isolate(t)
	rec, err := lifecycle.Parse([]byte(widgetRecord))
	if err != nil {
		t.Fatal(err)
	}
	moved := entriesOf(rec, map[string]string{"widget": "widget"})
	back, err := lifecycle.Parse(renderRecord(moved, nil, nil, nil))
	if err != nil {
		t.Fatal(err)
	}
	if len(back.Lifecycle()) != 1 || len(back.Licenses()) != 1 || len(back.Identities()) != 1 {
		t.Fatalf("read back: %+v %+v %+v", back.Lifecycle(), back.Licenses(), back.Identities())
	}
	if err := moveTagNamespace(back, "widget", "widget@v*", "v*", today, "extracted"); err != nil {
		t.Fatal(err)
	}
	var open, closed int
	for _, id := range back.Identities() {
		switch {
		case id.Open() && strings.Join(id.TagPatterns, ",") == "v*":
			open++
		case !id.Open() && id.Until.Equal(today):
			closed++
		}
	}
	if open != 1 || closed != 1 {
		t.Fatalf("identities: %+v", back.Identities())
	}
	if err := back.Validate(today, []string{"widget"}); err != nil {
		t.Fatal(err)
	}
}

func TestAddedEntriesAreAddedOnce(t *testing.T) {
	hygiene.Isolate(t)
	arriving, err := lifecycle.Parse([]byte(widgetRecord))
	if err != nil {
		t.Fatal(err)
	}
	here, err := lifecycle.Parse([]byte("format_version = 1\n"))
	if err != nil {
		t.Fatal(err)
	}
	moved := entriesOf(arriving, map[string]string{"widget": "gizmo"})
	for i := 0; i < 2; i++ {
		if err := addEntries(here, moved); err != nil {
			t.Fatalf("run %d: %v", i+1, err)
		}
	}
	if len(here.Lifecycle()) != 1 || len(here.Licenses()) != 1 || len(here.Identities()) != 1 || here.Licenses()[0].Subject != "gizmo" {
		t.Fatalf("after two runs: %+v %+v %+v", here.Lifecycle(), here.Licenses(), here.Identities())
	}
	started := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < 2; i++ {
		if err := closeRepositoryURL(here, "gizmo", "https://github.com/acme/gizmo", started, today, "absorbed"); err != nil {
			t.Fatalf("run %d: %v", i+1, err)
		}
	}
	urls := 0
	for _, id := range here.Identities() {
		if id.Facet == lifecycle.FacetRepositoryURL {
			urls++
			if id.Open() || !id.Until.Equal(today) || !id.From.Equal(started) {
				t.Errorf("repository-url identity: %+v", id)
			}
		}
	}
	if urls != 1 {
		t.Fatalf("%d repository-url identities", urls)
	}
	if err := here.Validate(today, []string{"gizmo"}); err != nil {
		t.Fatal(err)
	}
}

func TestAConversionRefusesAMissingFilterRepo(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.PathOnly(t, "git")
	err := requireFilterRepo()
	if err == nil || !strings.Contains(err.Error(), "Install it") {
		t.Fatalf("err = %v", err)
	}
	// Installing it clears the refusal.
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, filterRepoProgram), []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	if err := requireFilterRepo(); err != nil {
		t.Fatalf("after installing: %v", err)
	}
}

func TestAConversionRefusesAMissingSafermUnlessAPlainRemovalIsAskedFor(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.PathOnly(t, "git")
	err := requireSaferm(false, "the departing members")
	if err == nil || !strings.Contains(err.Error(), "--delete-with-rm") || !strings.Contains(err.Error(), "Install saferm") {
		t.Fatalf("err = %v", err)
	}
	if err := requireSaferm(true, "the departing members"); err != nil {
		t.Fatalf("with --delete-with-rm: %v", err)
	}
	testsupport.FakeSaferm(t)
	if err := requireSaferm(false, "the departing members"); err != nil {
		t.Fatalf("with saferm installed: %v", err)
	}
}

func TestARepositoryStartedOnALaterLocalDateThanTheConversionStartsItsURLThere(t *testing.T) {
	hygiene.Isolate(t)
	rec, err := lifecycle.Parse([]byte("format_version = 1\n"))
	if err != nil {
		t.Fatal(err)
	}
	// The root commit's own date is the 8th (00:30 at +02:00), the 7th in
	// UTC; the conversion runs on the 7th. The record keeps each time's own
	// date, so the period would end before it began.
	started := time.Date(2026, 10, 8, 0, 30, 0, 0, time.FixedZone("", 2*60*60))
	on := time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)
	if err := closeRepositoryURL(rec, "gizmo", "https://github.com/acme/gizmo", started, on, "absorbed"); err != nil {
		t.Fatal(err)
	}
	if err := rec.Validate(on, []string{"gizmo"}); err != nil {
		t.Fatalf("the closed repository-url identity is invalid: %v", err)
	}
}
