package git_test

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// reading runs fn in a read_only command whose observe allowlist is rlsbl's,
// so every git read fn makes must be an allowlisted observe, and fails the
// test when fn returns an error.
func reading(t *testing.T, dir string, fn func(r git.Repo) error) {
	t.Helper()
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		r, err := git.Open(e, dir)
		if err != nil {
			return err
		}
		return fn(r)
	})
}

// writing runs fn in a mutating command with rlsbl's observe allowlist and
// returns the dispatch's result and fn's error.
func writing(t *testing.T, dir string, dryRun bool, fn func(r git.Repo) error) (strictcli.Result, error) {
	t.Helper()
	var ferr error
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		r, err := git.Open(ctx.Effects(), dir)
		if err != nil {
			ferr = err
			return err
		}
		ferr = fn(r)
		return ferr
	})
	return res, ferr
}

func TestOpenRefusesARelativeDirectory(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly}, func(e *strictcli.Effects) error {
		if _, err := git.Open(e, "relative/dir"); err == nil || !strings.Contains(err.Error(), "not absolute") {
			return errors.New("a relative repository directory was accepted")
		}
		if _, err := git.Open(nil, "/abs"); err == nil {
			return errors.New("a missing effects handle was accepted")
		}
		return nil
	})
}

func TestResolveCommitAndHead(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	empty := testsupport.NewRepo(t)
	head := repo.CommitFile("a.txt", "a\n", "first")
	reading(t, repo.Dir, func(r git.Repo) error {
		sha, found, err := r.ResolveCommit("main")
		if err != nil || !found || sha != head {
			return errors.New("main did not resolve to HEAD")
		}
		if _, found, err := r.ResolveCommit("no-such-ref"); err != nil || found {
			return errors.New("a missing ref resolved")
		}
		if got, err := r.Head(); err != nil || got != head {
			return errors.New("Head is not HEAD")
		}
		return nil
	})
	reading(t, empty.Dir, func(r git.Repo) error {
		if _, err := r.Head(); err == nil || !strings.Contains(err.Error(), "no commit yet") {
			return errors.New("the HEAD of an empty repository resolved")
		}
		return nil
	})
}

// A directory that is not a repository of its own makes git walk up into
// the enclosing one; RequireToplevel refuses it.
func TestRequireToplevelRefusesADirectoryInsideAnotherRepository(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("sub/a.txt", "a\n", "first")
	reading(t, repo.Dir, func(r git.Repo) error {
		return r.RequireToplevel()
	})
	reading(t, repo.Path("sub"), func(r git.Repo) error {
		if err := r.RequireToplevel(); err == nil || !strings.Contains(err.Error(), "not the root") {
			return errors.New("a subdirectory passed as a repository root")
		}
		return nil
	})
}

func TestCurrentBranchRefusesADetachedHead(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	head := repo.CommitFile("a.txt", "a\n", "first")
	reading(t, repo.Dir, func(r git.Repo) error {
		if b, err := r.CurrentBranch(); err != nil || b != "main" {
			return errors.New("the branch is not main")
		}
		return nil
	})
	repo.Git("checkout", "-q", "--detach", head)
	reading(t, repo.Dir, func(r git.Repo) error {
		if _, err := r.CurrentBranch(); err == nil || !strings.Contains(err.Error(), "detached") {
			return errors.New("a detached HEAD was reported as a branch")
		}
		if b, attached, err := r.HeadBranch(); err != nil || attached || b != "" {
			return fmt.Errorf("HeadBranch on a detached HEAD: %q, %v, %v", b, attached, err)
		}
		return nil
	})
}

func TestTagsResolveToTheirCommitsPeeled(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	second := repo.CommitFile("b.txt", "b\n", "second")
	repo.Git("tag", "v0.1.0", first)
	repo.Git("tag", "-a", "-m", "annotated", "v0.2.0", second)
	reading(t, repo.Dir, func(r git.Repo) error {
		if sha, found, err := r.TagCommit("v0.2.0"); err != nil || !found || sha != second {
			return errors.New("the annotated tag did not peel to its commit")
		}
		if _, found, err := r.TagCommit("v9.9.9"); err != nil || found {
			return errors.New("a missing tag was found")
		}
		tags, err := r.TagCommits()
		if err != nil {
			return err
		}
		if len(tags) != 2 || tags["v0.1.0"] != first || tags["v0.2.0"] != second {
			return errors.New("TagCommits did not map every tag to its commit")
		}
		return nil
	})
}

func TestRemotesAndTheirTags(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	repo.Git("tag", "-a", "-m", "annotated", "v0.1.0", first)
	repo.Git("tag", "v0.0.1", first)
	bare := repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main", "v0.1.0", "v0.0.1")
	reading(t, repo.Dir, func(r git.Repo) error {
		if ok, err := r.RemoteConfigured("origin"); err != nil || !ok {
			return errors.New("origin is not configured")
		}
		if ok, err := r.RemoteConfigured("upstream"); err != nil || ok {
			return errors.New("a remote that is not configured was reported")
		}
		if url, err := r.RemoteURL("origin"); err != nil || url != "file://"+bare {
			return errors.New("the URL of origin is wrong")
		}
		if sha, found, err := r.RemoteTagCommit("origin", "v0.1.0"); err != nil || !found || sha != first {
			return errors.New("the remote annotated tag did not peel to its commit")
		}
		if _, found, err := r.RemoteTagCommit("origin", "v9.9.9"); err != nil || found {
			return errors.New("a tag the remote lacks was found")
		}
		tags, err := r.RemoteTagCommits("origin")
		if err != nil {
			return err
		}
		if len(tags) != 2 || tags["v0.1.0"] != first || tags["v0.0.1"] != first {
			return errors.New("RemoteTagCommits did not map every tag to its commit")
		}
		if obj, found, err := r.RemoteRef("origin", "refs/heads/main"); err != nil || !found || obj != first {
			return errors.New("the remote main is wrong")
		}
		if _, _, err := r.RemoteRef("origin", "main"); err == nil {
			return errors.New("a short ref name was accepted")
		}
		return nil
	})
}

// A remote that cannot be read is an error, never an empty answer.
func TestAnUnreachableRemoteIsAnErrorNotAbsence(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "first")
	repo.Git("remote", "add", "origin", "file://"+filepath.Join(t.TempDir(), "missing.git"))
	reading(t, repo.Dir, func(r git.Repo) error {
		if _, _, err := r.RemoteTagCommit("origin", "v0.1.0"); err == nil {
			return errors.New("an unreadable remote reported the tag absent")
		}
		if _, err := r.RemoteTagCommits("origin"); err == nil {
			return errors.New("an unreadable remote reported no tags")
		}
		return nil
	})
}

func TestTagPushPlan(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	second := repo.CommitFile("b.txt", "b\n", "second")
	repo.AddBareRemote("origin")
	repo.Git("tag", "v0.1.0", first)
	repo.Git("tag", "v0.2.0", second)
	reading(t, repo.Dir, func(r git.Repo) error {
		if needs, err := r.TagPushPlan("origin", []string{"v0.1.0"}); err != nil || !needs {
			return errors.New("a tag the remote lacks needs no push")
		}
		if _, err := r.TagPushPlan("origin", []string{"v9.9.9"}); err == nil {
			return errors.New("a tag missing locally was planned")
		}
		return nil
	})
	repo.Git("push", "-q", "origin", "v0.1.0")
	repo.Git("push", "-q", "origin", second+":refs/tags/v0.2.0")
	repo.Git("tag", "-f", "v0.2.0", first)
	reading(t, repo.Dir, func(r git.Repo) error {
		if needs, err := r.TagPushPlan("origin", []string{"v0.1.0"}); err != nil || needs {
			return errors.New("a tag the remote already holds at the same commit needs a push")
		}
		if _, err := r.TagPushPlan("origin", []string{"v0.2.0"}); err == nil || !strings.Contains(err.Error(), "never moves an existing tag") {
			return errors.New("a tag the remote holds at another commit was planned")
		}
		return nil
	})
}

func TestPushIsGuardedByItsLease(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	second := repo.CommitFile("b.txt", "b\n", "second")
	bare := repo.AddBareRemote("origin")
	push := func(u git.RefUpdate) error {
		_, err := writing(t, repo.Dir, false, func(r git.Repo) error { return r.Push("origin", u, time.Minute) })
		return err
	}
	if err := push(git.RefUpdate{Ref: "refs/heads/main", New: first}); err != nil {
		t.Fatalf("creating main: %v", err)
	}
	if err := push(git.RefUpdate{Ref: "refs/heads/main", New: first}); err == nil {
		t.Fatal("a push expecting no ref overwrote one")
	}
	if err := push(git.RefUpdate{Ref: "refs/heads/main", New: second, Expected: second}); err == nil {
		t.Fatal("a push with a stale lease landed")
	}
	if got := testsupport.Refs(t, bare)["refs/heads/main"]; got != first {
		t.Fatalf("a refused push moved main to %s", got)
	}
	if err := push(git.RefUpdate{Ref: "refs/heads/main", New: second, Expected: first}); err != nil {
		t.Fatalf("a push with the right lease: %v", err)
	}
	if err := push(git.RefUpdate{Ref: "refs/tags/v0.1.0", New: first}); err != nil {
		t.Fatalf("creating a tag: %v", err)
	}
	if err := push(git.RefUpdate{Ref: "refs/tags/v0.1.0", Expected: first}); err != nil {
		t.Fatalf("deleting a tag with its lease: %v", err)
	}
	refs := testsupport.Refs(t, bare)
	if refs["refs/heads/main"] != second || refs["refs/tags/v0.1.0"] != "" {
		t.Fatalf("remote refs = %v", refs)
	}
	for _, bad := range []git.RefUpdate{
		{Ref: "main", New: first},
		{Ref: "refs/tags/x"},
		{Ref: "refs/heads/main", New: "HEAD"},
		{Ref: "refs/heads/main", New: second, Expected: "abc"},
	} {
		if err := push(bad); err == nil {
			t.Errorf("push %+v was accepted", bad)
		}
	}
	_, err := writing(t, repo.Dir, false, func(r git.Repo) error {
		return r.Push("origin", git.RefUpdate{Ref: "refs/heads/x", New: first}, 0)
	})
	if err == nil {
		t.Error("a push without a timeout was accepted")
	}
}

func TestADryRunRecordsThePushInsteadOfPushing(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "first")
	bare := repo.AddBareRemote("origin")
	res, err := writing(t, repo.Dir, true, func(r git.Repo) error {
		return r.Push("origin", git.RefUpdate{Ref: "refs/heads/main", New: first}, time.Minute)
	})
	if err != nil || res.ExitCode != 0 {
		t.Fatalf("exit %d: %v", res.ExitCode, err)
	}
	if refs := testsupport.Refs(t, bare); len(refs) != 0 {
		t.Fatalf("a dry run pushed: %v", refs)
	}
	if !strings.Contains(res.Stdout+res.Stderr, "push") {
		t.Fatalf("the dry run did not record the push: %q %q", res.Stdout, res.Stderr)
	}
}

func TestFetchOriginUpdatesTheTrackingRefs(t *testing.T) {
	hygiene.Isolate(t)
	upstream := testsupport.NewRepo(t)
	upstream.CommitFile("a.txt", "a\n", "first")
	clone := upstream.Clone()
	head := upstream.CommitFile("b.txt", "b\n", "second")
	reading(t, clone.Dir, func(r git.Repo) error {
		if err := r.FetchOrigin(); err != nil {
			return err
		}
		sha, found, err := r.RemoteTrackingCommit("origin", "main")
		if err != nil || !found || sha != head {
			return errors.New("the fetch did not move origin/main")
		}
		return nil
	})
}

func TestIsRepositoryRoot(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	if !git.IsRepositoryRoot(repo.Dir) {
		t.Error("a repository root was not recognized")
	}
	plain := t.TempDir()
	if git.IsRepositoryRoot(plain) {
		t.Error("a plain directory was taken for a repository")
	}
	linked := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(linked, ".git"), "gitdir: /somewhere/.git/worktrees/x\n")
	if !git.IsRepositoryRoot(linked) {
		t.Error("a .git file naming its git directory was not recognized")
	}
	junk := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(junk, ".git"), "not a pointer\n")
	if git.IsRepositoryRoot(junk) {
		t.Error("a .git file that names nothing was taken for a repository")
	}
	headless := t.TempDir()
	if err := os.Mkdir(filepath.Join(headless, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if git.IsRepositoryRoot(headless) {
		t.Error("a .git directory without HEAD was taken for a repository")
	}
}

func TestObjectIDs(t *testing.T) {
	hygiene.Isolate(t)
	full := strings.Repeat("a1", 20)
	if !git.IsObjectID(full) || !git.IsObjectID(strings.Repeat("b2", 32)) {
		t.Error("a full object id was refused")
	}
	for _, s := range []string{"", "abc1234", strings.ToUpper(full), full + "0", strings.Repeat("g", 40)} {
		if git.IsObjectID(s) {
			t.Errorf("%q was taken for an object id", s)
		}
	}
	if !git.IsNullObjectID(strings.Repeat("0", 40)) || git.IsNullObjectID(full) {
		t.Error("the null object id is misjudged")
	}
}

func TestRefNamesListsOneNamespace(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	sha := repo.CommitFile("a.txt", "a\n", "first")
	repo.Git("update-ref", "refs/tags-of/github.com/acme/portal/v0.2.0", sha)
	repo.Git("update-ref", "refs/tags-of/github.com/acme/portal/v0.1.0", sha)
	repo.Git("update-ref", "refs/tags-of/github.com/acme/widget/v0.1.0", sha)
	repo.Git("tag", "v0.3.0")
	reading(t, repo.Dir, func(r git.Repo) error {
		got, err := r.RefNames("refs/tags-of/github.com/acme/portal/")
		if err != nil {
			return err
		}
		want := []string{"refs/tags-of/github.com/acme/portal/v0.1.0", "refs/tags-of/github.com/acme/portal/v0.2.0"}
		if !slices.Equal(got, want) {
			return fmt.Errorf("refs %v, want %v", got, want)
		}
		if none, err := r.RefNames("refs/upstream/"); err != nil || len(none) != 0 {
			return fmt.Errorf("an empty namespace: %v, %v", none, err)
		}
		if _, err := r.RefNames("refs/tags-of"); err == nil {
			return errors.New("a namespace without its final slash was accepted")
		}
		return nil
	})
}
