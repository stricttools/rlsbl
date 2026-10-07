package releaserecord_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

var (
	rewrittenA = strings.Repeat("a", 40)
	rewrittenB = strings.Repeat("b", 40)
	rewrittenC = strings.Repeat("c", 40)
	rewrittenD = strings.Repeat("d", 40)
)

// writeRewriteArchive writes a into root as the archive of a rewrite started
// at started.
func writeRewriteArchive(t *testing.T, root string, started time.Time, a releaserecord.RewriteArchive) string {
	t.Helper()
	var rel string
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(e *strictcli.Effects) error {
		var err error
		rel, err = releaserecord.WriteRewriteArchive(e, root, started, a)
		return err
	})
	return rel
}

func TestTheRewriteArchiveKeepsOnlyItsFieldsAndReadsBack(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	started := time.Date(2026, 3, 4, 5, 6, 7, 0, time.FixedZone("x", 3600))
	a := releaserecord.RewriteArchive{Operation: releaserecord.OperationScrub, Mode: "pattern", Reason: "a token", OldHead: rewrittenA, NewHead: rewrittenB, CommitsRewritten: 1, Rewrites: map[string]string{rewrittenA: rewrittenB}, Tags: []releaserecord.ArchivedTag{{Refname: "refs/tags/v1", OldSHA: rewrittenA, NewSHA: rewrittenB}}}
	rel := writeRewriteArchive(t, root, started, a)
	if rel != ".strictmetadata/history-rewrites/20260304T040607Z.toml" {
		t.Fatalf("archive path %s", rel)
	}
	archives, names, err := releaserecord.ReadRewriteArchives(root)
	if err != nil || len(names) != 1 {
		t.Fatalf("read %v, %v", names, err)
	}
	back := archives[rel]
	if back.Rewrites[rewrittenA] != rewrittenB || back.Tags[0].NewSHA != rewrittenB || back.Reason != "a token" || len(back.SquashCommits) != 0 {
		t.Fatalf("read back %+v", back)
	}
	path := filepath.Join(root, filepath.FromSlash(rel))
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(strings.Replace(string(data), "\n[rewrites]", "pattern = \"tok\"\n\n[rewrites]", 1)), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, err := releaserecord.ReadRewriteArchives(root); err == nil {
		t.Fatal("an archive carrying an unknown field was accepted")
	}
}

// Only a declassification records squash commits.
func TestASquashCommitIsRecordedByADeclassificationAlone(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	day := time.Date(2026, 3, 4, 0, 0, 0, 0, time.UTC)
	a := releaserecord.RewriteArchive{Operation: releaserecord.OperationScrub, Mode: "pattern", Reason: "x", OldHead: rewrittenA, NewHead: rewrittenB, Rewrites: map[string]string{rewrittenA: rewrittenB}, SquashCommits: []string{rewrittenB}}
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(ctx *strictcli.Context) error {
		if _, err := releaserecord.WriteRewriteArchive(ctx.Effects(), root, day, a); err == nil || !strings.Contains(err.Error(), "only a declassify records them") {
			t.Errorf("a scrub archive recording a squash commit was written: %v", err)
		}
		return nil
	})
}

// The squash commits are followed through every later rewrite, so each is
// the id the history holds now.
func TestTheSquashCommitsAreFollowedThroughLaterRewrites(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	day := time.Date(2026, 3, 4, 0, 0, 0, 0, time.UTC)
	writeRewriteArchive(t, root, day, releaserecord.RewriteArchive{Operation: releaserecord.OperationDeclassify, Mode: "squash", Reason: "public", OldHead: rewrittenA, NewHead: rewrittenB, Rewrites: map[string]string{rewrittenA: rewrittenB}, SquashCommits: []string{rewrittenB, rewrittenC}})
	writeRewriteArchive(t, root, day.Add(time.Hour), releaserecord.RewriteArchive{Operation: releaserecord.OperationScrub, Mode: "pattern", Reason: "a token", OldHead: rewrittenB, NewHead: rewrittenD, Rewrites: map[string]string{rewrittenB: rewrittenD}})
	got, err := releaserecord.DeclassifySquashCommits(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || !got[rewrittenD] || !got[rewrittenC] {
		t.Fatalf("squash commits %v, want %s and %s", got, rewrittenD, rewrittenC)
	}
}
