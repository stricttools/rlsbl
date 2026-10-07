package changelog_test

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestRemapRewritesEveryChangelogKeepingModesAndOtherLines(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	oldA, newA := strings.Repeat("a", 40), strings.Repeat("1", 40)
	oldB, newB := strings.Repeat("b", 40), strings.Repeat("2", 40)
	oldC, newC := "c"+strings.Repeat("0", 39), strings.Repeat("3", 40)
	oldD := "c" + strings.Repeat("0", 38) + "1"
	rewrites := map[string]string{oldA: newA, oldB: newB, oldC: newC, oldD: strings.Repeat("4", 40)}
	untouched := line(internalEntry("9", strings.Repeat("e", 40)))
	unreleased := changelog.Dir("widget") + "/unreleased.jsonl"
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(unreleased)), untouched+"\n"+line(feature("1", "d", oldA, "fffffff"))+"\n")
	releasedRel := writeReleased(t, root, "widget", "1.0.0", feature("2", "d", oldB[:8]))
	retired := changelog.RetiredDir("gizmo") + "/unreleased.jsonl"
	writeLines(t, root, retired, internalEntry("3", "c000000"))

	var report changelog.RemapReport
	_, err := appending(t, root, false, func(e *strictcli.Effects) error {
		var err error
		report, err = changelog.Remap(e, root, rewrites)
		return err
	})
	mustNotFail(t, err)
	if got := readText(t, filepath.Join(root, filepath.FromSlash(unreleased))); got != untouched+"\n"+line(feature("1", "d", newA, "fffffff"))+"\n" {
		t.Errorf("unreleased:\n%s", got)
	}
	released := filepath.Join(root, filepath.FromSlash(releasedRel))
	if got := readText(t, released); got != line(feature("2", "d", newB))+"\n" || mode(t, released) != 0o444 {
		t.Errorf("released (mode %o):\n%s", mode(t, released), got)
	}
	if len(report.Files) != 2 {
		t.Errorf("files %+v", report.Files)
	}
	if strings.Join(report.Unmapped[unreleased], ",") != strings.Repeat("e", 40)+",fffffff" {
		t.Errorf("unmapped %v", report.Unmapped)
	}
	if strings.Join(report.Ambiguous[retired], ",") != "c000000" {
		t.Errorf("ambiguous %v", report.Ambiguous)
	}
	if !changelog.CanRemap(oldB[:7], rewrites) || changelog.CanRemap("c000000", rewrites) || changelog.CanRemap("aaa", rewrites) {
		t.Error("CanRemap disagrees with the remap")
	}
}

func TestDirsAndGlobsCoverReleasablesAndRetiredHistories(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeLines(t, root, changelog.Dir("widget")+"/unreleased.jsonl")
	writeLines(t, root, changelog.RetiredDir("gizmo")+"/0.1.0.jsonl")
	testsupport.WriteFile(t, filepath.Join(root, ".strictmetadata", "changelog", "manifest.toml"), "owner = \"rlsbl\"\n")
	dirs, err := changelog.Dirs(root)
	mustNotFail(t, err)
	if strings.Join(dirs, ",") != ".strictmetadata/changelog/widget,.strictmetadata/retired-release-histories/gizmo/changelog" {
		t.Fatalf("dirs %v", dirs)
	}
	for _, rel := range []string{".strictmetadata/changelog/widget/unreleased.jsonl", ".strictmetadata/retired-release-histories/gizmo/changelog/0.1.0.jsonl"} {
		matched := false
		for _, g := range changelog.RemapGlobs() {
			if ok, _ := filepath.Match(g, rel); ok {
				matched = true
			}
		}
		if !matched {
			t.Errorf("no glob matches %s", rel)
		}
	}
}

func TestUnresolvedCommitsNameEachFile(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	head := repo.CommitFile("a.txt", "a\n", "first")
	writeLines(t, repo.Dir, changelog.Dir("widget")+"/unreleased.jsonl", feature("1", "d", head, "deadbeefdead"))
	mustNotFail(t, reading(t, repo.Dir, func(gr git.Repo) error {
		got, err := changelog.UnresolvedCommits(gr, repo.Dir)
		if err != nil {
			return err
		}
		if len(got) != 1 || strings.Join(got[changelog.Dir("widget")+"/unreleased.jsonl"], ",") != "deadbeefdead" {
			t.Errorf("unresolved %v", got)
		}
		return nil
	}))
}
