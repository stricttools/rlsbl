package git_test

import (
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestAncestry(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	second := repo.CommitFile("b.txt", "b\n", "second")
	repo.Git("checkout", "-q", "-b", "side", first)
	side := repo.CommitFile("c.txt", "c\n", "side")
	reading(t, repo.Dir, func(r git.Repo) error {
		for _, c := range []struct {
			a, d string
			want git.Ancestry
		}{
			{first, second, git.IsAncestor},
			{second, second, git.IsAncestor},
			{second, first, git.NotAncestor},
			{side, second, git.NotAncestor},
			{strings.Repeat("d", 40), second, git.AncestryIndeterminable},
		} {
			got, err := r.Ancestry(c.a, c.d)
			if err != nil {
				return err
			}
			if got != c.want {
				return fmt.Errorf("Ancestry(%s, %s) = %s, want %s", c.a[:7], c.d[:7], got, c.want)
			}
		}
		return nil
	})
}

// In a shallow clone git answers "no" for a commit whose connecting history
// was never fetched; that answer is reported as indeterminable.
func TestAShallowNoIsIndeterminable(t *testing.T) {
	hygiene.Isolate(t)
	upstream := testsupport.NewRepo(t)
	first := upstream.CommitFile("a.txt", "a\n", "first")
	upstream.Git("tag", "v0.1.0", first)
	upstream.CommitFile("b.txt", "b\n", "second")
	head := upstream.CommitFile("c.txt", "c\n", "third")
	shallow := t.TempDir() + "/shallow"
	if _, stderr, code := testsupport.RunGit(t, t.TempDir(), "clone", "-q", "--depth", "1", "file://"+upstream.Dir, shallow); code != 0 {
		t.Fatalf("shallow clone: %s", stderr)
	}
	// A second depth-1 fetch brings the first commit in without the history
	// connecting it to HEAD: both commits are present, and git still says no.
	if _, stderr, code := testsupport.RunGit(t, shallow, "fetch", "-q", "--depth", "1", "origin", "tag", "v0.1.0"); code != 0 {
		t.Fatalf("shallow fetch: %s", stderr)
	}
	if _, _, code := testsupport.RunGit(t, shallow, "merge-base", "--is-ancestor", first, head); code != 1 {
		t.Fatalf("git merge-base exited %d in the shallow clone, want its unfounded no (1)", code)
	}
	reading(t, shallow, func(r git.Repo) error {
		if ok, err := r.IsShallow(); err != nil || !ok {
			return errors.New("the shallow clone is not reported shallow")
		}
		got, err := r.Ancestry(head, head)
		if err != nil || got != git.IsAncestor {
			return fmt.Errorf("a commit is its own ancestor: %s, %v", got, err)
		}
		got, err = r.Ancestry(first, head)
		if err != nil || got != git.AncestryIndeterminable {
			return fmt.Errorf("a shallow no = %s, %v; want indeterminable", got, err)
		}
		return nil
	})
	reading(t, upstream.Dir, func(r git.Repo) error {
		if ok, err := r.IsShallow(); err != nil || ok {
			return errors.New("a full repository is reported shallow")
		}
		return nil
	})
}

func TestTrees(t *testing.T) {
	hygiene.Isolate(t)
	if got := git.TreeRevSpec("abc", "."); got != "abc^{tree}" {
		t.Errorf("root spec = %q", got)
	}
	if got := git.TreeRevSpec("abc", ""); got != "abc^{tree}" {
		t.Errorf("empty-path spec = %q", got)
	}
	if got := git.TreeRevSpec("abc", "pkg/core"); got != "abc:pkg/core" {
		t.Errorf("subdirectory spec = %q", got)
	}
	repo := testsupport.NewRepo(t)
	head := repo.CommitFile("pkg/core/a.txt", "a\n", "first")
	reading(t, repo.Dir, func(r git.Repo) error {
		root, found, err := r.TreeAt(head, ".")
		if err != nil || !found || root != repo.Git("rev-parse", head+"^{tree}") {
			return errors.New("the root tree is wrong")
		}
		sub, found, err := r.TreeAt(head, "pkg/core")
		if err != nil || !found || sub != repo.Git("rev-parse", head+":pkg/core") {
			return errors.New("the subdirectory tree is wrong")
		}
		if _, found, err := r.TreeAt(head, "pkg/missing"); err != nil || found {
			return errors.New("a missing path resolved to a tree")
		}
		return nil
	})
}

// The commit that created the project lists every file it added, and a
// path git would quote in its default output arrives verbatim.
func TestCommitFilesListsARootCommitAndAwkwardPathsVerbatim(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write("README.md", "x\n")
	repo.Write("pkgé/x.txt", "x\n")
	repo.Write("with space.txt", "x\n")
	root := repo.Commit("first", "README.md", "pkgé/x.txt", "with space.txt")
	second := repo.CommitFile("src/b.txt", "b\n", "second")
	reading(t, repo.Dir, func(r git.Repo) error {
		files, err := r.CommitFiles(root)
		if err != nil {
			return err
		}
		slices.Sort(files)
		if strings.Join(files, "|") != "README.md|pkgé/x.txt|with space.txt" {
			return fmt.Errorf("root commit files = %q", files)
		}
		files, err = r.CommitFiles(second)
		if err != nil || strings.Join(files, "|") != "src/b.txt" {
			return fmt.Errorf("second commit files = %q, %v", files, err)
		}
		if _, err := r.CommitFiles(strings.Repeat("e", 40)); err == nil || !strings.Contains(err.Error(), "cannot determine the files") {
			return errors.New("a missing commit was given a file list")
		}
		return nil
	})
}

// A merge is charged with what it brought into the first parent.
func TestCommitFilesOfAMergeIsItsFirstParentDiff(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "first")
	repo.Git("checkout", "-q", "-b", "side")
	repo.CommitFile("side.txt", "s\n", "side")
	repo.Git("checkout", "-q", "main")
	repo.CommitFile("main.txt", "m\n", "main")
	repo.Git("merge", "-q", "--no-ff", "-m", "merge side", "side")
	merge := repo.Head()
	reading(t, repo.Dir, func(r git.Repo) error {
		files, err := r.CommitFiles(merge)
		if err != nil || strings.Join(files, "|") != "side.txt" {
			return fmt.Errorf("merge files = %q, %v", files, err)
		}
		return nil
	})
}

func TestCommitsCountsAndMessages(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	second := repo.CommitFile("b.txt", "b\n", "second subject\n\nbody line")
	third := repo.CommitFile("c.txt", "c\n", "third")
	reading(t, repo.Dir, func(r git.Repo) error {
		commits, err := r.Commits([]string{"HEAD"}, []string{first})
		if err != nil || !slices.Equal(commits, []string{third, second}) {
			return fmt.Errorf("commits = %q, %v", commits, err)
		}
		all, err := r.Commits([]string{"HEAD"}, nil)
		if err != nil || len(all) != 3 {
			return fmt.Errorf("all commits = %q, %v", all, err)
		}
		if _, err := r.Commits(nil, nil); err == nil {
			return errors.New("a listing without a start was accepted")
		}
		n, err := r.CountCommits([]string{"HEAD"}, []string{first})
		if err != nil || n != 2 {
			return fmt.Errorf("count = %d, %v", n, err)
		}
		subject, err := r.CommitSubject(second)
		if err != nil || subject != "second subject" {
			return fmt.Errorf("subject = %q, %v", subject, err)
		}
		message, err := r.CommitMessage(second)
		if err != nil || message != "second subject\n\nbody line" {
			return fmt.Errorf("message = %q, %v", message, err)
		}
		when, err := r.CommitterDate(first)
		if err != nil || time.Since(when) > time.Hour || time.Since(when) < -time.Minute {
			return fmt.Errorf("committer date = %v, %v", when, err)
		}
		return nil
	})
}

func TestTheAutogeneratedTrailer(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	plain := repo.CommitFile("a.txt", "a\n", "plain")
	marked := repo.CommitFile("b.txt", "b\n", "marked\n\nAutogenerated: true")
	unmarked := repo.CommitFile("c.txt", "c\n", "unmarked\n\nAutogenerated: false")
	misspelled := repo.CommitFile("d.txt", "d\n", "misspelled\n\nAutogenerated: yes")
	twice := repo.CommitFile("e.txt", "e\n", "twice\n\nAutogenerated: true\nAutogenerated: true")
	reading(t, repo.Dir, func(r git.Repo) error {
		for _, c := range []struct {
			sha  string
			want bool
		}{{plain, false}, {marked, true}, {unmarked, false}} {
			got, err := r.IsAutogenerated(c.sha)
			if err != nil || got != c.want {
				return fmt.Errorf("IsAutogenerated(%s) = %v, %v", c.sha[:7], got, err)
			}
		}
		for _, sha := range []string{misspelled, twice} {
			if _, err := r.IsAutogenerated(sha); err == nil {
				return fmt.Errorf("an ambiguous marker on %s decided coverage", sha[:7])
			}
		}
		values, err := r.TrailerValues(marked, "Autogenerated")
		if err != nil || !slices.Equal(values, []string{"true"}) {
			return fmt.Errorf("trailer values = %q, %v", values, err)
		}
		if _, err := r.TrailerValues(marked, "bad,key"); err == nil {
			return errors.New("a malformed trailer key was accepted")
		}
		return nil
	})
}

// File contents come back byte for byte, a final newline or its absence
// included.
func TestFileAtIsByteExact(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write("with.txt", "line\n")
	repo.Write("without.txt", "line")
	repo.Write("blank-end.txt", "line\n\n")
	head := repo.Commit("files", "with.txt", "without.txt", "blank-end.txt")
	reading(t, repo.Dir, func(r git.Repo) error {
		for path, want := range map[string]string{"with.txt": "line\n", "without.txt": "line", "blank-end.txt": "line\n\n"} {
			got, found, err := r.FileAt(head, path)
			if err != nil || !found || got != want {
				return fmt.Errorf("FileAt(%s) = %q, %v, %v; want %q", path, got, found, err, want)
			}
		}
		if _, found, err := r.FileAt(head, "missing.txt"); err != nil || found {
			return errors.New("a missing file was found")
		}
		if _, err := r.Blob(repo.Git("rev-parse", head+"^{tree}")); err == nil {
			return errors.New("a tree was read as a blob")
		}
		return nil
	})
}

func TestTrackedFiles(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".gitignore", "ignored.txt\n")
	repo.Write("tracked.txt", "t\n")
	repo.Commit("first", ".gitignore", "tracked.txt")
	repo.Write("untracked.txt", "u\n")
	repo.Write("ignored.txt", "i\n")
	reading(t, repo.Dir, func(r git.Repo) error {
		tracked, err := r.TrackedFiles()
		if err != nil || strings.Join(tracked, "|") != ".gitignore|tracked.txt" {
			return fmt.Errorf("tracked = %q, %v", tracked, err)
		}
		listed, err := r.TrackedAndUntrackedFiles()
		slices.Sort(listed)
		if err != nil || strings.Join(listed, "|") != ".gitignore|tracked.txt|untracked.txt" {
			return fmt.Errorf("listed = %q, %v", listed, err)
		}
		return nil
	})
}

// FilesAt reads the committed tree, not the working tree, and a directory
// the revision lacks lists nothing.
func TestFilesAtListsACommittedDirectory(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".github/workflows/ci.yml", "a\n")
	repo.Write(".github/workflows/nested/publish.yml", "b\n")
	repo.Write("other.txt", "c\n")
	head := repo.Commit("files", ".github/workflows/ci.yml", ".github/workflows/nested/publish.yml", "other.txt")
	repo.Write(".github/workflows/uncommitted.yml", "d\n")
	reading(t, repo.Dir, func(r git.Repo) error {
		got, err := r.FilesAt(head, ".github/workflows")
		if err != nil || strings.Join(got, "|") != ".github/workflows/ci.yml|.github/workflows/nested/publish.yml" {
			return fmt.Errorf("FilesAt = %q, %v", got, err)
		}
		if got, err := r.FilesAt(head, "missing"); err != nil || len(got) != 0 {
			return fmt.Errorf("a missing directory listed %q, %v", got, err)
		}
		if _, err := r.FilesAt("refs/heads/none", "."); err == nil {
			return errors.New("a revision that names nothing listed files")
		}
		return nil
	})
}
