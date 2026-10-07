package git_test

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestChangesNameEveryPathOfARenameAndEveryUntrackedFile(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("old.txt", "a\n", "first")
	repo.Git("mv", "old.txt", "new.txt")
	repo.Write("dir/one.txt", "1\n")
	repo.Write("dir/two.txt", "2\n")
	reading(t, repo.Dir, func(r git.Repo) error {
		changes, err := r.Changes()
		if err != nil {
			return err
		}
		var got []string
		for _, c := range changes {
			got = append(got, c.Status+" "+c.Path)
		}
		want := "?? dir/one.txt,?? dir/two.txt,A  new.txt,D  old.txt"
		sort.Strings(got)
		if strings.Join(got, ",") != want {
			return fmt.Errorf("changes: %v", got)
		}
		return nil
	})
}

func TestChangedBetweenAndTracksAndIgnored(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	repo.Write("b.txt", "b\n")
	repo.Write(".gitignore", "*.log\n")
	second := repo.Commit("second", "b.txt", ".gitignore")
	reading(t, repo.Dir, func(r git.Repo) error {
		paths, err := r.ChangedBetween(first, second)
		if err != nil {
			return err
		}
		if strings.Join(paths, ",") != ".gitignore,b.txt" {
			return fmt.Errorf("changed: %v", paths)
		}
		if tracked, err := r.Tracks("a.txt"); err != nil || !tracked {
			return fmt.Errorf("a.txt tracked: %v %v", tracked, err)
		}
		if tracked, err := r.Tracks("go.work"); err != nil || tracked {
			return fmt.Errorf("go.work tracked: %v %v", tracked, err)
		}
		if ignored, err := r.Ignored("x.log"); err != nil || !ignored {
			return fmt.Errorf("x.log ignored: %v %v", ignored, err)
		}
		if ignored, err := r.Ignored("a.txt"); err != nil || ignored {
			return fmt.Errorf("a.txt ignored: %v %v", ignored, err)
		}
		return nil
	})
}

func TestADetachedWorktreeIsAddedResetAndCleanedKeepingIgnoredFiles(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".gitignore", "cache/\n")
	head := repo.Commit("first", ".gitignore")
	path := filepath.Join(t.TempDir(), "checkout")
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error { return r.AddDetachedWorktree(path, head) }); err != nil {
		t.Fatal(err)
	}
	testsupport.WriteFile(t, filepath.Join(path, ".gitignore"), "scribbled\n")
	testsupport.WriteFile(t, filepath.Join(path, "stray.txt"), "untracked\n")
	testsupport.WriteFile(t, filepath.Join(path, "cache", "warm"), "kept\n")
	if _, err := writing(t, path, false, func(r git.Repo) error { return r.ResetDetached(head) }); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(filepath.Join(path, ".gitignore")); string(data) != "cache/\n" {
		t.Errorf("the tracked file was not reset: %q", data)
	}
	if _, err := os.Stat(filepath.Join(path, "stray.txt")); !os.IsNotExist(err) {
		t.Errorf("the untracked file was kept: %v", err)
	}
	if data, _ := os.ReadFile(filepath.Join(path, "cache", "warm")); string(data) != "kept\n" {
		t.Errorf("the ignored file was removed: %q", data)
	}
	if _, _, code := testsupport.RunGit(t, path, "symbolic-ref", "-q", "HEAD"); code == 0 {
		t.Error("the worktree's HEAD is not detached")
	}
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error { return r.AddDetachedWorktree("relative", head) }); err == nil {
		t.Error("a relative worktree path was accepted")
	}
}

func TestSwapRefMovesOnlyFromTheExpectedCommit(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	second := repo.CommitFile("a.txt", "b\n", "second")
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error {
		return r.SwapRef("refs/heads/other", first, second, "test")
	}); err == nil {
		t.Fatal("a swap from a commit the ref does not hold was accepted")
	}
	repo.Git("branch", "other", first)
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error {
		return r.SwapRef("refs/heads/other", second, first, "test")
	}); err != nil {
		t.Fatal(err)
	}
	if got := repo.Git("rev-parse", "refs/heads/other"); got != second {
		t.Errorf("other is at %s", got)
	}
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error { return r.SwapRef("other", first, second, "test") }); err == nil {
		t.Error("a short ref name was accepted")
	}
}

func TestRestorePathsTakesTheSourcesContentAndIsLiteral(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a*.txt", "first\n", "first")
	repo.CommitFile("a*.txt", "second\n", "second")
	repo.CommitFile("ab.txt", "untouched\n", "third")
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error { return r.RestorePaths(first, []string{"a*.txt"}) }); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(repo.Path("a*.txt")); string(data) != "first\n" {
		t.Errorf("a*.txt holds %q", data)
	}
	if data, _ := os.ReadFile(repo.Path("ab.txt")); string(data) != "untouched\n" {
		t.Errorf("the pathspec matched ab.txt as a glob: %q", data)
	}
}
