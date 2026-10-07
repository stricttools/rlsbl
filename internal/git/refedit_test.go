package git_test

import (
	"fmt"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestLocalRefsAreUnpeeledAndNarrowedToThePrefix(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	head := repo.CommitFile("a.txt", "a\n", "first")
	repo.Git("tag", "light")
	repo.Git("tag", "-a", "heavy", "-m", "an annotated tag")
	heavy := repo.Git("rev-parse", "refs/tags/heavy")
	reading(t, repo.Dir, func(r git.Repo) error {
		refs, err := r.LocalRefs("refs/tags/")
		if err != nil {
			return err
		}
		if len(refs) != 2 || refs["refs/tags/light"] != head || refs["refs/tags/heavy"] != heavy || heavy == head {
			return fmt.Errorf("refs: %v", refs)
		}
		if _, err := r.LocalRefs("tags/"); err == nil {
			return fmt.Errorf("a prefix outside refs/ was accepted")
		}
		return nil
	})
}

func TestRemoteRefsAreUnpeeled(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "first")
	repo.Git("tag", "-a", "heavy", "-m", "an annotated tag")
	heavy := repo.Git("rev-parse", "refs/tags/heavy")
	repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "refs/tags/heavy")
	reading(t, repo.Dir, func(r git.Repo) error {
		refs, err := r.RemoteRefs("origin", "refs/tags/*")
		if err != nil {
			return err
		}
		if len(refs) != 1 || refs["refs/tags/heavy"] != heavy {
			return fmt.Errorf("refs: %v", refs)
		}
		if _, err := r.RemoteRefs("no-such-remote", "refs/tags/*"); err == nil {
			return fmt.Errorf("an unreadable remote read as no refs")
		}
		return nil
	})
}

func TestHasObject(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	head := repo.CommitFile("a.txt", "a\n", "first")
	reading(t, repo.Dir, func(r git.Repo) error {
		if ok, err := r.HasObject(head); err != nil || !ok {
			return fmt.Errorf("HEAD's commit is not held: %v", err)
		}
		if ok, err := r.HasObject(strings.Repeat("1", 40)); err != nil || ok {
			return fmt.Errorf("a missing object is held: %v", err)
		}
		if _, err := r.HasObject("HEAD"); err == nil {
			return fmt.Errorf("a revision that is not an object id was accepted")
		}
		return nil
	})
}

func TestCreateRefAndDeleteRefAreGuardedByTheObject(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	second := repo.CommitFile("b.txt", "b\n", "second")
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error { return r.CreateRef("refs/kept/x", first) }); err != nil {
		t.Fatal(err)
	}
	if got := testsupport.Refs(t, repo.Dir)["refs/kept/x"]; got != first {
		t.Fatalf("refs/kept/x is %q", got)
	}
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error { return r.CreateRef("refs/kept/x", second) }); err == nil {
		t.Fatal("an existing ref was overwritten")
	}
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error { return r.DeleteRef("refs/kept/x", second) }); err == nil {
		t.Fatal("a ref holding another object was deleted")
	}
	if _, err := writing(t, repo.Dir, false, func(r git.Repo) error { return r.DeleteRef("refs/kept/x", first) }); err != nil {
		t.Fatal(err)
	}
	if _, ok := testsupport.Refs(t, repo.Dir)["refs/kept/x"]; ok {
		t.Fatal("refs/kept/x was not deleted")
	}
	if _, err := writing(t, repo.Dir, true, func(r git.Repo) error { return r.CreateRef("refs/kept/y", first) }); err != nil {
		t.Fatal(err)
	}
	if _, ok := testsupport.Refs(t, repo.Dir)["refs/kept/y"]; ok {
		t.Fatal("a dry run wrote a ref")
	}
}
