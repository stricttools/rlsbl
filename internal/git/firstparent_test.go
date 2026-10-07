package git_test

import (
	"fmt"
	"slices"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestFirstParentHistoryLeavesMergedBranchesOut(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	t.Setenv("GIT_COMMITTER_DATE", "2026-03-01T10:00:00+02:00")
	first := repo.CommitFile("a.txt", "a\n", "first")
	repo.Git("checkout", "-q", "-b", "side")
	side := repo.CommitFile("b.txt", "b\n", "side work")
	repo.Git("checkout", "-q", "main")
	second := repo.CommitFile("c.txt", "c\n", "second")
	repo.Git("merge", "-q", "--no-ff", "-m", "merge side", "side")
	merge := repo.Head()
	reading(t, repo.Dir, func(r git.Repo) error {
		history, err := r.FirstParentHistory("HEAD")
		if err != nil {
			return err
		}
		var shas []string
		for _, c := range history {
			shas = append(shas, c.SHA)
		}
		if !slices.Equal(shas, []string{first, second, merge}) || slices.Contains(shas, side) {
			return fmt.Errorf("history %v", shas)
		}
		last := history[2]
		if len(last.Parents) != 2 || last.Parents[0] != second || last.Subject != "merge side" {
			return fmt.Errorf("the merge reads %+v", last)
		}
		if got := last.Committed.Format("2006-01-02T15:04:05-07:00"); got != "2026-03-01T10:00:00+02:00" {
			return fmt.Errorf("committed %s", got)
		}
		return nil
	})
}
