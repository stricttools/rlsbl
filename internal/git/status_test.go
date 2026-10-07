package git_test

import (
	"errors"
	"fmt"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// Paths git's default output would quote arrive verbatim, so they can be
// named again in a commit.
func TestChangedPathsArriveVerbatim(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("seed.txt", "s\n", "seed")
	names := []string{"pkgé/x.txt", "with space.txt", "tab\there.txt", "quote\"d.txt", "back\\slash.txt"}
	for _, n := range names {
		repo.Write(n, "x\n")
	}
	reading(t, repo.Dir, func(r git.Repo) error {
		got, err := r.ChangedPaths(nil, git.UntrackedAll)
		if err != nil {
			return err
		}
		want := slices.Clone(names)
		slices.Sort(want)
		slices.Sort(got)
		if !slices.Equal(got, want) {
			return fmt.Errorf("changed paths = %q, want %q", got, want)
		}
		return nil
	})
}

func TestARenameReportsOnlyItsNewPath(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("old.txt", "content that is long enough to be a rename\n", "seed")
	repo.Git("mv", "old.txt", "new.txt")
	reading(t, repo.Dir, func(r git.Repo) error {
		paths, err := r.ChangedPaths(nil, git.UntrackedNormal)
		if err != nil || !slices.Equal(paths, []string{"new.txt"}) {
			return fmt.Errorf("paths = %q, %v", paths, err)
		}
		records, err := r.Status(nil, git.UntrackedNormal)
		if err != nil || len(records) != 1 || !strings.HasPrefix(records[0], "R  ") {
			return fmt.Errorf("records = %q, %v", records, err)
		}
		return nil
	})
}

func TestStatusRecordsKeepTheirColumns(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "seed")
	if err := os.Remove(repo.Path("a.txt")); err != nil {
		t.Fatal(err)
	}
	reading(t, repo.Dir, func(r git.Repo) error {
		records, err := r.Status(nil, git.UntrackedNormal)
		if err != nil || !slices.Equal(records, []string{" D a.txt"}) {
			return fmt.Errorf("records = %q, %v", records, err)
		}
		return nil
	})
}

func TestStatusNarrowingAndUntrackedModes(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "seed")
	repo.Write("a.txt", "changed\n")
	repo.Write("fresh/one.txt", "1\n")
	repo.Write("fresh/two.txt", "2\n")
	reading(t, repo.Dir, func(r git.Repo) error {
		narrowed, err := r.ChangedPaths([]string{"a.txt"}, git.UntrackedNormal)
		if err != nil || !slices.Equal(narrowed, []string{"a.txt"}) {
			return fmt.Errorf("narrowed = %q, %v", narrowed, err)
		}
		none, err := r.ChangedPaths([]string{"nothing-here"}, git.UntrackedNormal)
		if err != nil || len(none) != 0 {
			return fmt.Errorf("a pathspec matching nothing = %q, %v", none, err)
		}
		collapsed, err := r.ChangedPaths(nil, git.UntrackedNormal)
		if err != nil || !slices.Equal(collapsed, []string{"a.txt", "fresh/"}) {
			return fmt.Errorf("normal = %q, %v", collapsed, err)
		}
		all, err := r.ChangedPaths(nil, git.UntrackedAll)
		if err != nil || !slices.Equal(all, []string{"a.txt", "fresh/one.txt", "fresh/two.txt"}) {
			return fmt.Errorf("all = %q, %v", all, err)
		}
		tracked, err := r.ChangedPaths(nil, git.UntrackedNo)
		if err != nil || !slices.Equal(tracked, []string{"a.txt"}) {
			return fmt.Errorf("no = %q, %v", tracked, err)
		}
		if clean, err := r.IsClean(); err != nil || clean {
			return errors.New("a changed tree was reported clean")
		}
		return nil
	})
	repo.Git("checkout", "--", "a.txt")
	if err := os.RemoveAll(repo.Path("fresh")); err != nil {
		t.Fatal(err)
	}
	reading(t, repo.Dir, func(r git.Repo) error {
		if clean, err := r.IsClean(); err != nil || !clean {
			return errors.New("a clean tree was reported changed")
		}
		return nil
	})
}

func TestStatusArgsRefuseAnUnknownModeAndKeepTheObservePrefix(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := git.StatusArgs(nil, "some"); err == nil {
		t.Fatal("an unknown untracked mode was accepted")
	}
	if _, err := git.StatusArgs(nil, ""); err == nil {
		t.Fatal("an unstated untracked mode was accepted")
	}
	args, err := git.StatusArgs([]string{"a.txt"}, git.UntrackedAll)
	if err != nil {
		t.Fatal(err)
	}
	if !previewapply.Allowed(append([]string{"git"}, args...)) {
		t.Fatalf("git %s is not an allowlisted observe", strings.Join(args, " "))
	}
	if !slices.Equal(args[len(args)-2:], []string{"--", "a.txt"}) || !slices.Contains(args, "--untracked-files=all") {
		t.Fatalf("args = %q", args)
	}
}

// The parsers read the raw capture: an unstaged record starts with a space,
// and a rename's origin field belongs to its record.
func TestTheParsersReadRawCaptureOutput(t *testing.T) {
	hygiene.Isolate(t)
	raw := " M a.txt\x00R  new.txt\x00old.txt\x00?? b c.txt\x00"
	if got := git.ParseStatusRecords(raw); !slices.Equal(got, []string{" M a.txt", "R  new.txt", "?? b c.txt"}) {
		t.Fatalf("records = %q", got)
	}
	if got := git.ParseStatusPaths(raw); !slices.Equal(got, []string{"a.txt", "new.txt", "b c.txt"}) {
		t.Fatalf("paths = %q", got)
	}
}

// The stash refusal names its fix; dropping the stash clears it.
func TestAStashIsRefusedUntilItIsDropped(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "seed")
	reading(t, repo.Dir, func(r git.Repo) error {
		return r.RefuseStash("release", "A release commits this working tree.")
	})
	repo.Write("a.txt", "changed\n")
	repo.Git("stash", "push", "-q")
	reading(t, repo.Dir, func(r git.Repo) error {
		entries, err := r.StashEntries()
		if err != nil || len(entries) != 1 {
			return fmt.Errorf("entries = %q, %v", entries, err)
		}
		err = r.RefuseStash("release", "A release commits this working tree.")
		if err == nil || !strings.Contains(err.Error(), "refusing to release: this repository has 1 stash entries") || !strings.Contains(err.Error(), "git stash drop") || !strings.Contains(err.Error(), "A release commits this working tree.") {
			return fmt.Errorf("refusal = %v", err)
		}
		return nil
	})
	repo.Git("stash", "drop", "-q")
	reading(t, repo.Dir, func(r git.Repo) error {
		return r.RefuseStash("release", "A release commits this working tree.")
	})
}
