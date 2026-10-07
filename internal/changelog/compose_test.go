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

// twoMembers is a workspace repository with one commit in each member and
// one touching both.
type twoMembers struct {
	repo   *testsupport.Repo
	widget string
	gadget string
	both   string
}

func newTwoMembers(t *testing.T) twoMembers {
	t.Helper()
	repo := testsupport.NewRepo(t)
	r := twoMembers{repo: repo}
	r.widget = repo.CommitFile("widget/w.txt", "w\n", "widget work")
	r.gadget = repo.CommitFile("gadget/g.txt", "g\n", "gadget work")
	repo.Write("widget/w.txt", "w2\n")
	repo.Write("gadget/g.txt", "g2\n")
	r.both = repo.Commit("both", "widget/w.txt", "gadget/g.txt")
	return r
}

func TestPrepareResolvesTheCommitsAndDerivesThePackages(t *testing.T) {
	hygiene.Isolate(t)
	r := newTwoMembers(t)
	w := newWorkspace(t, r.repo.Dir, twoReleasables)
	mustNotFail(t, reading(t, r.repo.Dir, func(repo git.Repo) error {
		s := subject(t, repo, w, "widget")
		p, err := s.Prepare(changelog.EntryRequest{Commits: []string{r.widget[:9], " " + r.both + " "}, UserFacing: true, Description: "Widget work", Type: changelog.TypeFeature}, nil)
		if err != nil {
			return err
		}
		if strings.Join(p.Entry.Commits, ",") != r.widget+","+r.both || strings.Join(p.Entry.Packages, ",") != "widget" || len(p.Entry.ID) != 48 {
			t.Errorf("entry %+v", p.Entry)
		}
		return nil
	}))
}

func TestPrepareRefusesACommitOfAnotherReleasableNamingItsOwner(t *testing.T) {
	hygiene.Isolate(t)
	r := newTwoMembers(t)
	w := newWorkspace(t, r.repo.Dir, twoReleasables)
	mustNotFail(t, reading(t, r.repo.Dir, func(repo git.Repo) error {
		_, err := subject(t, repo, w, "widget").Prepare(changelog.EntryRequest{Commits: []string{r.gadget}, Type: changelog.TypeFix}, nil)
		if err == nil {
			t.Fatal("a commit of gadget was accepted into widget's changelog")
		}
		requireContains(t, err.Error(), `touches nothing the changelog of "widget" covers`, `member "gadget" (releasable "gadget")`, "add the entry from a directory of the owning member")
		_, err = subject(t, repo, w, "gadget").Prepare(changelog.EntryRequest{Commits: []string{r.gadget}, Type: changelog.TypeFix}, nil)
		return err
	}))
}

func TestPrepareRefusalsNameWhatIsMissing(t *testing.T) {
	hygiene.Isolate(t)
	r := newTwoMembers(t)
	w := newWorkspace(t, r.repo.Dir, twoReleasables)
	mustNotFail(t, reading(t, r.repo.Dir, func(repo git.Repo) error {
		s := subject(t, repo, w, "widget")
		for name, c := range map[string]struct {
			req  changelog.EntryRequest
			want string
		}{
			"no commit":             {changelog.EntryRequest{Commits: []string{" "}}, "names no commit"},
			"an unknown commit":     {changelog.EntryRequest{Commits: []string{"deadbeefdead"}}, "deadbeefdead does not resolve"},
			"no description":        {changelog.EntryRequest{Commits: []string{r.widget}, UserFacing: true, Type: changelog.TypeFix}, "needs a description and a type"},
			"a needless reason":     {changelog.EntryRequest{Commits: []string{r.widget}, BatchReason: "why"}, "needs no batch reason"},
			"an unknown entry type": {changelog.EntryRequest{Commits: []string{r.widget}, UserFacing: true, Description: "d", Type: "chore"}, "STRICTSPEC_"},
		} {
			_, err := s.Prepare(c.req, nil)
			if err == nil {
				t.Errorf("%s: accepted", name)
				continue
			}
			requireContains(t, err.Error(), c.want)
		}
		return nil
	}))
}

func TestAnEntryOverTheLimitNeedsABatchReason(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	var commits []string
	for i := 0; i < 6; i++ {
		commits = append(commits, repo.CommitFile("a.txt", strings.Repeat("a", i+1)+"\n", "work"))
	}
	w := newWorkspace(t, repo.Dir, standalone)
	mustNotFail(t, reading(t, repo.Dir, func(gr git.Repo) error {
		s := subject(t, gr, w, "portal")
		_, err := s.Prepare(changelog.EntryRequest{Commits: commits}, nil)
		if err == nil {
			t.Fatal("six commits without a batch reason were accepted")
		}
		requireContains(t, err.Error(), "6 commits", "--batch-reason")
		p, err := s.Prepare(changelog.EntryRequest{Commits: commits, BatchReason: "one change"}, nil)
		if err != nil {
			return err
		}
		if p.Entry.BatchReason != "one change" || p.Entry.Packages != nil {
			t.Errorf("entry %+v", p.Entry)
		}
		return nil
	}))
}

func TestACommitAnEntryOfTheSameTypeCoversIsRefusedAndAnotherTypeIsNoted(t *testing.T) {
	hygiene.Isolate(t)
	r := newTwoMembers(t)
	w := newWorkspace(t, r.repo.Dir, twoReleasables)
	existing := []changelog.Entry{feature("1", "Widget work", r.widget)}
	mustNotFail(t, reading(t, r.repo.Dir, func(repo git.Repo) error {
		s := subject(t, repo, w, "widget")
		_, err := s.Prepare(changelog.EntryRequest{Commits: []string{r.widget}, UserFacing: true, Description: "Again", Type: changelog.TypeFeature}, existing)
		if err == nil {
			t.Fatal("a second feature entry for one commit was accepted")
		}
		requireContains(t, err.Error(), "already covered by entry "+id("1"), "rlsbl changelog edit --id "+id("1"))
		p, err := s.Prepare(changelog.EntryRequest{Commits: []string{r.widget}, UserFacing: true, Description: "A fix too", Type: changelog.TypeFix}, existing)
		if err != nil {
			return err
		}
		if len(p.Notes) != 1 || !strings.Contains(p.Notes[0], "also appears in entry "+id("1")) {
			t.Errorf("notes %v", p.Notes)
		}
		return nil
	}))
}

func TestFindAddressesEntriesByIDOrCommitAcrossFiles(t *testing.T) {
	hygiene.Isolate(t)
	r := newTwoMembers(t)
	w := newWorkspace(t, r.repo.Dir, twoReleasables)
	dir := changelog.Dir("widget")
	writeLines(t, r.repo.Dir, dir+"/unreleased.jsonl", feature("1", "first", r.both))
	writeReleased(t, r.repo.Dir, "widget", "0.1.0", feature("2", "shipped", r.widget, r.both), internalEntry("3", "deadbeefdead"))
	mustNotFail(t, reading(t, r.repo.Dir, func(repo git.Repo) error {
		s := subject(t, repo, w, "widget")
		matches, err := s.Find(changelog.Selector{Commits: []string{r.both[:8]}}, true)
		if err != nil {
			return err
		}
		if len(matches) != 2 || matches[0].File.Released || !matches[1].File.Released || matches[1].Entry().ID != id("2") {
			t.Errorf("matches %+v", matches)
		}
		requireContains(t, matches[1].Describe(), "[v0.1.0] entry "+id("2")+", type feature: shipped")
		byID, err := s.Find(changelog.Selector{ID: id("2")}, true)
		if err != nil {
			return err
		}
		if len(byID) != 1 || byID[0].Index != 0 {
			t.Errorf("by id %+v", byID)
		}
		if _, err := s.Find(changelog.Selector{Commits: []string{"deadbeefdead"}}, true); err == nil {
			t.Error("an unresolvable commit was accepted where resolution is required")
		}
		gone, err := s.Find(changelog.Selector{Commits: []string{"deadbeefdead"}}, false)
		if err != nil {
			return err
		}
		if len(gone) != 1 || gone[0].Entry().ID != id("3") {
			t.Errorf("an unresolvable commit matched %+v", gone)
		}
		_, err = s.Find(changelog.Selector{ID: id("7")}, true)
		if err == nil || !strings.Contains(err.Error(), "has the id "+id("7")) {
			t.Errorf("a missing id: %v", err)
		}
		if _, err := s.Find(changelog.Selector{ID: id("2"), Commits: []string{r.widget}}, true); err == nil {
			t.Error("a selector naming both an id and commits was accepted")
		}
		return nil
	}))
}

func ptr[T any](v T) *T { return &v }

func TestApplyEditIsASparseUpdate(t *testing.T) {
	hygiene.Isolate(t)
	base := feature("1", "old", sha1)
	got, err := changelog.ApplyEdit(base, changelog.EditRequest{Description: ptr("new")})
	mustNotFail(t, err)
	if got.Description != "new" || got.Type != changelog.TypeFeature || !got.UserFacing {
		t.Errorf("description edit: %+v", got)
	}
	got, err = changelog.ApplyEdit(base, changelog.EditRequest{UserFacing: ptr(false), Description: ptr("ignored")})
	mustNotFail(t, err)
	if got.UserFacing || got.Description != "old" || got.Type != changelog.TypeFeature {
		t.Errorf("turning it off kept the line's prose and stored no new value: %+v", got)
	}
	got, err = changelog.ApplyEdit(base, changelog.EditRequest{UserFacing: ptr(false), UnsetDescription: true, UnsetType: true})
	mustNotFail(t, err)
	if got.Description != "" || got.Type != "" {
		t.Errorf("clears: %+v", got)
	}
	if _, err := changelog.ApplyEdit(internalEntry("2", sha1), changelog.EditRequest{UserFacing: ptr(true), Description: ptr("d")}); err == nil {
		t.Error("an entry made user-facing without a type was accepted")
	}
	got, err = changelog.ApplyEdit(internalEntry("2", sha1), changelog.EditRequest{UserFacing: ptr(true), Description: ptr("d"), Type: ptr(changelog.TypeFix)})
	mustNotFail(t, err)
	if !got.UserFacing || got.Type != changelog.TypeFix {
		t.Errorf("made user-facing: %+v", got)
	}
	for name, req := range map[string]changelog.EditRequest{
		"nothing":                {},
		"write and clear":        {Description: ptr("d"), UnsetDescription: true},
		"a type outside the set": {Type: ptr("chore")},
		"clearing what it needs": {UnsetType: true},
	} {
		if _, err := changelog.ApplyEdit(base, req); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestReplaceAndRemoveRewriteOneLineKeepingTheRest(t *testing.T) {
	hygiene.Isolate(t)
	r := newTwoMembers(t)
	w := newWorkspace(t, r.repo.Dir, twoReleasables)
	releasedRel := writeReleased(t, r.repo.Dir, "widget", "0.1.0", feature("1", "first", r.widget), feature("2", "second", r.both))
	path := filepath.Join(r.repo.Dir, filepath.FromSlash(releasedRel))
	var match changelog.Match
	mustNotFail(t, reading(t, r.repo.Dir, func(repo git.Repo) error {
		matches, err := subject(t, repo, w, "widget").Find(changelog.Selector{ID: id("2")}, true)
		if err != nil {
			return err
		}
		match = matches[0]
		return nil
	}))
	edited := feature("2", "second, reworded", r.both)
	_, err := writing(t, r.repo.Dir, false, func(e *strictcli.Effects, _ git.Repo) error {
		return changelog.Replace(e, r.repo.Dir, match, edited)
	})
	mustNotFail(t, err)
	if got := readText(t, path); got != line(feature("1", "first", r.widget))+"\n"+line(edited)+"\n" || mode(t, path) != 0o444 {
		t.Fatalf("replaced (mode %o):\n%s", mode(t, path), got)
	}
	_, err = writing(t, r.repo.Dir, false, func(e *strictcli.Effects, _ git.Repo) error {
		return changelog.Remove(e, r.repo.Dir, match)
	})
	if err == nil {
		t.Fatal("a removal from a file changed since it was read succeeded")
	}
	requireContains(t, err.Error(), "changed while it was being edited")
	mustNotFail(t, reading(t, r.repo.Dir, func(repo git.Repo) error {
		matches, err := subject(t, repo, w, "widget").Find(changelog.Selector{ID: id("1")}, true)
		if err != nil {
			return err
		}
		match = matches[0]
		return nil
	}))
	_, err = writing(t, r.repo.Dir, false, func(e *strictcli.Effects, _ git.Repo) error {
		return changelog.Remove(e, r.repo.Dir, match)
	})
	mustNotFail(t, err)
	if got := readText(t, path); got != line(edited)+"\n" || mode(t, path) != 0o444 {
		t.Fatalf("removed (mode %o):\n%s", mode(t, path), got)
	}
}

func TestSubjectAtNamesTheReleasableOfTheMemberAndRefusesADevNode(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	w := newWorkspace(t, root, twoReleasables)
	testsupport.WriteFile(t, filepath.Join(root, "widget", "sub", "x.txt"), "")
	got, err := changelog.SubjectAt(w, filepath.Join(root, "widget", "sub"))
	mustNotFail(t, err)
	if got != "widget" {
		t.Errorf("got %q", got)
	}
	_, err = changelog.SubjectAt(w, root)
	if err == nil {
		t.Fatal("the dev-node root selected a changelog")
	}
	requireContains(t, err.Error(), `"root"`, "versioned under no releasable")
}
