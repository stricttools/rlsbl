package git_test

import (
	"fmt"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestThreeWayMergeCleanAndConflicting(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	reading(t, repo.Dir, func(r git.Repo) error {
		base := "a\nb\nc\nd\ne\nf\n"
		merged, conflicts, err := r.ThreeWayMerge("a\nlocal\nc\nd\ne\nf\n", base, "a\nb\nc\nd\ne\ntemplate\n")
		if err != nil || conflicts != 0 || merged != "a\nlocal\nc\nd\ne\ntemplate\n" {
			return fmt.Errorf("clean merge = %q, %d, %v", merged, conflicts, err)
		}
		merged, conflicts, err = r.ThreeWayMerge("a\nX\nc\n", "a\nb\nc\n", "a\nY\nc\n")
		if err != nil || conflicts != 1 {
			return fmt.Errorf("conflicting merge = %d, %v", conflicts, err)
		}
		if merged != "a\n<<<<<<< ours\nX\n=======\nY\n>>>>>>> theirs\nc\n" {
			return fmt.Errorf("conflict markers = %q", merged)
		}
		return nil
	})
}

// The merged text keeps its final newline, or its absence: the result is
// read from the object store, not from a program's trimmed output.
func TestThreeWayMergeKeepsTheFinalNewlineExactly(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	reading(t, repo.Dir, func(r git.Repo) error {
		for _, end := range []string{"", "\n", "\n\n"} {
			text := "one\ntwo" + end
			merged, conflicts, err := r.ThreeWayMerge(text, text, text)
			if err != nil || conflicts != 0 || merged != text {
				return fmt.Errorf("merge of %q = %q, %d, %v", text, merged, conflicts, err)
			}
		}
		return nil
	})
}

// A preview merges the way a run does: every step is an allowlisted observe,
// so it runs for real under --dry-run and writes no file.
func TestThreeWayMergeRunsUnderADryRun(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "seed")
	_, err := writing(t, repo.Dir, true, func(r git.Repo) error {
		merged, conflicts, err := r.ThreeWayMerge("x\n", "x\n", "y\n")
		if err != nil || conflicts != 0 || merged != "y\n" {
			return fmt.Errorf("merge under a dry run = %q, %d, %v", merged, conflicts, err)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if status := repo.Git("status", "--porcelain"); strings.TrimSpace(status) != "" {
		t.Fatalf("the merge left files behind: %q", status)
	}
}
