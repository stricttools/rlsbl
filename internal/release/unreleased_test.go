package release_test

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/release"
)

func readUnreleased(t *testing.T, dir string) (release.Unreleased, error) {
	t.Helper()
	var u release.Unreleased
	err := run(t, nil, func(e *strictcli.Effects) error {
		var err error
		u, err = release.ReadUnreleased(e, dir, release.Fork{})
		return err
	})
	return u, err
}

func TestUnreleasedListsTheCommitsSinceTheNearestRelease(t *testing.T) {
	hygiene.Isolate(t)
	repo, change := released(t, "none")
	missing := repo.CommitFile("d.txt", "d\n", "an undescribed change")
	u, err := readUnreleased(t, repo.Dir)
	mustNotFail(t, err)
	if u.Releasable != "portal" || u.NearestRelease == nil || *u.NearestRelease != "0.4.0" || u.LatestRelease == nil || *u.LatestRelease != "0.4.0" {
		t.Fatalf("report %+v", u)
	}
	var lines []string
	for _, c := range u.Commits {
		state := "missing"
		switch {
		case c.Exempt:
			state = "exempt"
		case c.Covered:
			state = "covered"
		}
		lines = append(lines, c.Subject+":"+state)
		if c.Author == "" || c.Date == "" || len(c.Hash) != 40 {
			t.Errorf("commit %+v lacks its summary", c)
		}
	}
	want := "an undescribed change:missing|changelog: entry:exempt|regenerate:exempt|a change:covered|release 0.4.0:exempt"
	if got := strings.Join(lines, "|"); got != want {
		t.Fatalf("commits\n%s\nwant\n%s", got, want)
	}
	if u.Commits[0].Hash != missing || u.Commits[3].Hash != change {
		t.Fatalf("hashes %+v", u.Commits)
	}
	if u.Coverage != (release.Coverage{Covered: 1, Total: 2, Exempted: 3}) {
		t.Fatalf("coverage %+v", u.Coverage)
	}
}

func TestUnreleasedWithNoReleaseListsTheWholeHistory(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "none")
	repo.CommitFile("b.txt", "b\n", "a change")
	u, err := readUnreleased(t, repo.Dir)
	mustNotFail(t, err)
	if u.NearestRelease != nil || u.LatestRelease != nil || len(u.Commits) != 2 || u.Coverage.Total != 2 {
		t.Fatalf("report %+v", u)
	}
}

// The range is bounded by the release this checkout contains, while the
// latest release named is the releasable's latest, which it may not contain.
func TestUnreleasedNamesALatestReleaseOutsideTheCheckout(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "none")
	recordRelease(t, repo, "portal", "0.4.0", "v0.4.0", "recorded")
	base := repo.Head()
	repo.Git("checkout", "-q", "-b", "side")
	repo.CommitFile("side.txt", "s\n", "a side change")
	recordRelease(t, repo, "portal", "0.5.0", "v0.5.0", "recorded")
	archive := ".strictmetadata/releases/portal/v0.5.0.toml"
	releasedFile := ".strictmetadata/changelog/portal/0.5.0.jsonl"
	repo.Git("checkout", "-q", "main")
	repo.Git("checkout", "side", "--", archive, releasedFile)
	repo.Commit("carry the 0.5.0 record", archive, releasedFile)
	u, err := readUnreleased(t, repo.Dir)
	mustNotFail(t, err)
	if u.NearestRelease == nil || *u.NearestRelease != "0.4.0" {
		t.Fatalf("nearest %v (the checkout is based on %s)", u.NearestRelease, base)
	}
	if u.LatestRelease == nil || *u.LatestRelease != "0.5.0" || u.LatestReleaseInCheckout == nil || *u.LatestReleaseInCheckout {
		t.Fatalf("latest %+v", u.LatestFields)
	}
	requireContains(t, u.LatestReleaseLabel, "not in this checkout's history")
}

func TestUnreleasedInAWorkspaceListsOnlyTheReleasablesCommits(t *testing.T) {
	hygiene.Isolate(t)
	repo := workspaceRepo(t)
	repo.CommitFile("widget/a.txt", "a\n", "a widget change")
	repo.CommitFile("gadget/a.txt", "a\n", "a gadget change")
	u, err := readUnreleased(t, repo.Path("gadget"))
	mustNotFail(t, err)
	var subjects []string
	for _, c := range u.Commits {
		subjects = append(subjects, c.Subject)
	}
	if u.Releasable != "gadget" || strings.Join(subjects, "|") != "a gadget change|the workspace" {
		t.Fatalf("releasable %s, commits %v", u.Releasable, subjects)
	}
}

// A member versioned under no releasable has no changelog; the refusal
// names running from a member that has one, which answers.
func TestUnreleasedOfAMemberVersionedUnderNoReleasable(t *testing.T) {
	hygiene.Isolate(t)
	repo := workspaceRepo(t)
	_, err := readUnreleased(t, repo.Dir)
	if err == nil {
		t.Fatal("a dev node's unreleased commits were listed")
	}
	requireContains(t, err.Error(), `the member "root"`, "versioned under no releasable", "run it from the directory of a member")
	u, err := readUnreleased(t, repo.Path("widget"))
	mustNotFail(t, err)
	if u.Releasable != "widget" {
		t.Fatalf("report %+v", u)
	}
}
