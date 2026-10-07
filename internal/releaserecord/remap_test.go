package releaserecord_test

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// rewritten is a repository with a released commit and two rewrites of it:
// one carrying the same tree, one a different tree.
type rewritten struct {
	repo                      *testsupport.Repo
	released, same, different string
	tree                      string
}

func newRewritten(t *testing.T) rewritten {
	t.Helper()
	repo := testsupport.NewRepo(t)
	released := repo.CommitFile("file.txt", "released\n", "release")
	tree := repo.Git("rev-parse", released+"^{tree}")
	same := repo.Git("commit-tree", tree, "-m", "the same content, rewritten")
	other := repo.CommitFile("file.txt", "redacted\n", "redact")
	different := repo.Git("commit-tree", repo.Git("rev-parse", other+"^{tree}"), "-m", "redacted")
	rel := releaserecord.ArchivePath(releaserecord.ArchiveDir(releasable), version(t, "0.1.0"))
	testsupport.WriteFile(t, filepath.Join(repo.Dir, rel), releaseFile+"shipped_as = \"old@v0.1.0\"\nrelease_commit = \""+released+"\"\n\n[released_trees]\n\".\" = \""+tree+"\"\n")
	writeArchive(t, repo.Dir, "0.2.0", "unrecoverable", "")
	return rewritten{repo: repo, released: released, same: same, different: different, tree: tree}
}

func plan(t *testing.T, dir string, commitMap map[string]string, onChange releaserecord.ContentChange) ([]releaserecord.CommitRemap, error) {
	t.Helper()
	var remaps []releaserecord.CommitRemap
	_, err := writing(t, dir, false, func(_ *strictcli.Effects, repo git.Repo) error {
		var err error
		remaps, err = releaserecord.PlanRemap(repo, releaserecord.ArchiveDir(releasable), commitMap, onChange)
		return err
	})
	return remaps, err
}

func TestARewriteKeepingTheContentMovesTheReleaseCommit(t *testing.T) {
	hygiene.Isolate(t)
	w := newRewritten(t)
	var remaps []releaserecord.CommitRemap
	var touched []string
	_, err := writing(t, w.repo.Dir, false, func(e *strictcli.Effects, repo git.Repo) error {
		var err error
		remaps, touched, err = releaserecord.RepairReleaseCommits(e, repo, map[string]string{w.released: w.same}, "safegit scrub", releaserecord.ContentChangeRefuse, recordedAt)
		return err
	})
	mustNotFail(t, err)
	if len(remaps) != 1 || remaps[0].NewCommit != w.same || len(remaps[0].Changed) != 0 {
		t.Fatalf("remaps %+v", remaps)
	}
	if strings.Join(touched, ",") != ".strictmetadata/releases/gadget/v0.1.0.toml,"+transitionsFile {
		t.Fatalf("touched %v", touched)
	}
	a, err := releaserecord.ReadArchive(w.repo.Dir, releaserecord.ArchiveDir(releasable), version(t, "0.1.0"))
	mustNotFail(t, err)
	if a.ReleaseCommit.Commit != w.same || a.ReleaseCommit.Trees["."] != w.tree || a.ShippedAs != "old@v0.1.0" {
		t.Fatalf("archive %+v", a)
	}
	events, err := releaserecord.ReadEvents(w.repo.Dir)
	mustNotFail(t, err)
	ev, ok := events[0].(*releaserecord.ReleaseCommitRemapEvent)
	if len(events) != 1 || !ok || ev.Releasable != releasable || ev.Rewrite != "safegit scrub" || ev.Mappings[0].OldSHA != w.released || ev.Mappings[0].NewSHA != w.same {
		t.Fatalf("events %+v", events)
	}
}

func TestARewriteChangingAReleasedTreeIsRefusedByDefault(t *testing.T) {
	hygiene.Isolate(t)
	w := newRewritten(t)
	before := readText(t, filepath.Join(w.repo.Dir, ".strictmetadata/releases/gadget/v0.1.0.toml"))
	_, err := writing(t, w.repo.Dir, false, func(e *strictcli.Effects, repo git.Repo) error {
		_, _, err := releaserecord.RepairReleaseCommits(e, repo, map[string]string{w.released: w.different}, "safegit scrub", releaserecord.ContentChangeRefuse, recordedAt)
		return err
	})
	if err == nil {
		t.Fatal("a changed tree was recorded")
	}
	requireContains(t, err.Error(), "cannot be moved through the rewrite", "Nothing was written")
	if readText(t, filepath.Join(w.repo.Dir, ".strictmetadata/releases/gadget/v0.1.0.toml")) != before {
		t.Fatal("the archive changed")
	}
}

func TestARewriteDeclaredToRecordChangesRecordsTheNewTree(t *testing.T) {
	hygiene.Isolate(t)
	w := newRewritten(t)
	remaps, err := plan(t, w.repo.Dir, map[string]string{w.released: w.different}, releaserecord.ContentChangeRecord)
	mustNotFail(t, err)
	if len(remaps) != 1 || len(remaps[0].Changed) != 1 || remaps[0].Changed[0].Recorded != w.tree || remaps[0].Trees["."] == w.tree {
		t.Fatalf("remaps %+v", remaps)
	}
}

func TestAnAbbreviatedReleaseCommitMapsByPrefix(t *testing.T) {
	hygiene.Isolate(t)
	w := newRewritten(t)
	remaps, err := plan(t, w.repo.Dir, map[string]string{w.released: w.same}, releaserecord.ContentChangeRefuse)
	mustNotFail(t, err)
	if len(remaps) != 1 {
		t.Fatalf("remaps %+v", remaps)
	}
	// An abbreviated archive matching two keys is refused.
	rel := filepath.Join(w.repo.Dir, ".strictmetadata/releases/gadget/v0.1.0.toml")
	testsupport.WriteFile(t, rel, strings.Replace(readText(t, rel), w.released, w.released[:7], 1))
	ambiguous := w.released[:7] + strings.Repeat("0", 33)
	_, err = plan(t, w.repo.Dir, map[string]string{w.released: w.same, ambiguous: w.same}, releaserecord.ContentChangeRefuse)
	if err == nil || !strings.Contains(err.Error(), "more than one commit") {
		t.Fatalf("got %v", err)
	}
}

func TestAMapNamingNoReleaseCommitMovesNothing(t *testing.T) {
	hygiene.Isolate(t)
	w := newRewritten(t)
	remaps, err := plan(t, w.repo.Dir, map[string]string{strings.Repeat("f", 40): w.same}, releaserecord.ContentChangeRefuse)
	mustNotFail(t, err)
	if len(remaps) != 0 {
		t.Fatalf("remaps %+v", remaps)
	}
	if _, err := plan(t, w.repo.Dir, map[string]string{w.released: w.same}, "guess"); err == nil {
		t.Fatal("an undeclared content-change answer was accepted")
	}
}
