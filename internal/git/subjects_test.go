package git_test

import (
	"errors"
	"fmt"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestCommitSubjectsListsARangeNewestFirst(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	second := repo.CommitFile("b.txt", "b\n", "second subject\n\nbody line")
	third := repo.CommitFile("c.txt", "c\n", "third: with a colon")
	reading(t, repo.Dir, func(r git.Repo) error {
		got, err := r.CommitSubjects([]string{third}, []string{first})
		if err != nil {
			return err
		}
		want := []git.CommitSubject{{SHA: third, Subject: "third: with a colon"}, {SHA: second, Subject: "second subject"}}
		if fmt.Sprint(got) != fmt.Sprint(want) {
			return fmt.Errorf("subjects = %v, want %v", got, want)
		}
		all, err := r.CommitSubjects([]string{"HEAD"}, nil)
		if err != nil || len(all) != 3 || all[2].SHA != first {
			return fmt.Errorf("all = %v, %v", all, err)
		}
		if _, err := r.CommitSubjects(nil, nil); err == nil {
			return errors.New("a listing without a start was accepted")
		}
		return nil
	})
}
