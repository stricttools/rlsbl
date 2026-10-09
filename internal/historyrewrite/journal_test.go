package historyrewrite_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/historyrewrite"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// readJournal reads the journal of the repository at dir.
func readJournal(t *testing.T, dir string) (historyrewrite.Journal, bool, error) {
	t.Helper()
	var j historyrewrite.Journal
	var found bool
	var jerr error
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: [][]string{{"git", "rev-parse"}}}, func(e *strictcli.Effects) error {
		repo, err := git.Open(e, dir)
		if err != nil {
			return err
		}
		j, found, jerr = historyrewrite.ReadJournal(repo)
		return nil
	})
	return j, found, jerr
}

func TestTheJournalsLastRewriteIsRead(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "a")
	if _, found, err := readJournal(t, repo.Dir); err != nil || found {
		t.Fatalf("a repository without a journal: %v, %v", found, err)
	}
	a, b, c := strings.Repeat("a", 40), strings.Repeat("b", 40), strings.Repeat("c", 40)
	writeJournal(t, repo, "first", map[string]string{a: b}, true)
	writeJournal(t, repo, "second", map[string]string{b: c}, false)
	j, found, err := readJournal(t, repo.Dir)
	if err != nil || !found {
		t.Fatalf("%v, %v", found, err)
	}
	if j.ID != "second" || j.CommitMap[b] != c || j.Complete {
		t.Fatalf("read %+v; want the last rewrite, which stopped part-way", j)
	}
}

func TestACorruptJournalLineIsAnErrorNamingIt(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "a")
	writeJournal(t, repo, "first", map[string]string{strings.Repeat("a", 40): strings.Repeat("b", 40)}, true)
	path := filepath.Join(repo.Path(".git"), "safegit", "rewrite-maps.jsonl")
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString("{not json\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()
	_, _, err = readJournal(t, repo.Dir)
	if err == nil || !strings.Contains(err.Error(), "corrupt at line 3") {
		t.Fatalf("a corrupt line read as %v", err)
	}
}

// TestASafegitRewriteMayTakeLongerInALongerHistory: a history rewrite's time
// grows with the history it walks, so the bound on it grows with the
// repository's commit count instead of being one fixed figure a large
// repository exceeds however healthy the rewrite is.
func TestASafegitRewriteMayTakeLongerInALongerHistory(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "a")
	bound := func() time.Duration {
		t.Helper()
		var d time.Duration
		testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: [][]string{{"git", "rev-parse"}, {"git", "rev-list"}}}, func(e *strictcli.Effects) error {
			r, err := git.Open(e, repo.Dir)
			if err != nil {
				return err
			}
			d, err = historyrewrite.SafegitRewriteTimeout(r)
			if err != nil {
				t.Fatal(err)
			}
			return nil
		})
		return d
	}
	short := bound()
	for i := 0; i < 20; i++ {
		repo.CommitFile("a.txt", strings.Repeat("a", i+2)+"\n", "more")
	}
	long := bound()
	if long <= short {
		t.Fatalf("the bound for 21 commits (%v) is not above the bound for 1 (%v)", long, short)
	}
	if per := (long - short) / 20; per*10000 < 5*time.Minute {
		t.Fatalf("each commit adds %v to the bound: ten thousand commits would get less than five minutes more than one", per)
	}
}
