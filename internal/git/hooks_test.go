package git_test

import (
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

var (
	idA  = strings.Repeat("a", 40)
	idB  = strings.Repeat("b", 40)
	idC  = strings.Repeat("c", 40)
	null = strings.Repeat("0", 40)
)

func TestParseRewriteMapReadsHookInputAndCommitMaps(t *testing.T) {
	hygiene.Isolate(t)
	m, err := git.ParseRewriteMap("old new\n" + idA + " " + idB + "\n\n# a comment\n" + idB + " " + idC + " amend\n")
	if err != nil {
		t.Fatal(err)
	}
	if len(m) != 2 || m[idA] != idB || m[idB] != idC {
		t.Fatalf("map = %v", m)
	}
}

func TestParseRewriteMapRefusesWhatItCannotRemap(t *testing.T) {
	hygiene.Isolate(t)
	for name, text := range map[string]string{
		"a one-field line":         idA + "\n",
		"an abbreviated id":        "abc1234 " + idB + "\n",
		"a header after content":   idA + " " + idB + "\nold new\n",
		"two targets for one id":   idA + " " + idB + "\n" + idA + " " + idC + "\n",
		"a target that is no id":   idA + " HEAD\n",
		"a mapping to the null id": idA + " " + null + "\n",
	} {
		if _, err := git.ParseRewriteMap(text); err == nil || !strings.Contains(err.Error(), "line ") {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// The null-id refusal names its fix: take the pruned commit out of the map
// (after removing it from the entry naming it); the map without that line
// parses.
func TestARewriteMapWithoutThePrunedLineParses(t *testing.T) {
	hygiene.Isolate(t)
	pruned := idA + " " + null + "\n"
	kept := idB + " " + idC + "\n"
	_, err := git.ParseRewriteMap(kept + pruned)
	if err == nil || !strings.Contains(err.Error(), "taken out of the map") {
		t.Fatalf("err = %v", err)
	}
	m, err := git.ParseRewriteMap(kept)
	if err != nil || m[idB] != idC {
		t.Fatalf("after taking the line out: %v, %v", m, err)
	}
}

func TestParsePrePushLines(t *testing.T) {
	hygiene.Isolate(t)
	refs, err := git.ParsePrePushLines([]string{"refs/heads/main " + idA + " refs/heads/main " + idB, "", "(delete) " + null + " refs/heads/old " + idC})
	if err != nil {
		t.Fatal(err)
	}
	want := []git.PushedRef{
		{LocalRef: "refs/heads/main", LocalID: idA, RemoteRef: "refs/heads/main", RemoteID: idB},
		{LocalRef: "(delete)", LocalID: null, RemoteRef: "refs/heads/old", RemoteID: idC},
	}
	if !slices.Equal(refs, want) {
		t.Fatalf("refs = %+v", refs)
	}
	if _, err := git.ParsePrePushLines([]string{"refs/heads/main " + idA}); err == nil {
		t.Fatal("a line without four fields was accepted")
	}
}

// A push from any local branch to a release branch on the remote is a
// manual push to that release branch; the ref it lands on decides, not the
// ref it was sent from.
func TestManualPushBranchesAreReadFromTheRemoteRef(t *testing.T) {
	hygiene.Isolate(t)
	release := []string{"main", "master"}
	cases := []struct {
		name string
		refs []git.PushedRef
		want []string
	}{
		{"main to main", []git.PushedRef{{LocalRef: "refs/heads/main", RemoteRef: "refs/heads/main"}}, []string{"main"}},
		{"a feature branch to main", []git.PushedRef{{LocalRef: "refs/heads/feature", RemoteRef: "refs/heads/main"}}, []string{"main"}},
		{"main to a feature branch", []git.PushedRef{{LocalRef: "refs/heads/main", RemoteRef: "refs/heads/feature"}}, nil},
		{"deleting master", []git.PushedRef{{LocalRef: "(delete)", RemoteRef: "refs/heads/master"}}, []string{"master"}},
		{"a tag", []git.PushedRef{{LocalRef: "refs/tags/v1.0.0", RemoteRef: "refs/tags/v1.0.0"}}, nil},
		{"main twice", []git.PushedRef{{RemoteRef: "refs/heads/main"}, {RemoteRef: "refs/heads/main"}}, []string{"main"}},
	}
	for _, c := range cases {
		if got := git.ManualPushBranches(c.refs, release); !slices.Equal(got, c.want) {
			t.Errorf("%s: %q, want %q", c.name, got, c.want)
		}
	}
}

func TestPushChangedFiles(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "first")
	repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	pushed := repo.Head()
	repo.CommitFile("b.txt", "b\n", "second")
	repo.CommitFile("pkgé/c.txt", "c\n", "third")
	head := repo.Head()
	reading(t, repo.Dir, func(r git.Repo) error {
		update, err := r.PushChangedFiles([]git.PushedRef{{LocalRef: "refs/heads/main", LocalID: head, RemoteRef: "refs/heads/main", RemoteID: pushed}})
		slices.Sort(update)
		if err != nil || !slices.Equal(update, []string{"b.txt", "pkgé/c.txt"}) {
			return fmt.Errorf("update = %q, %v", update, err)
		}
		created, err := r.PushChangedFiles([]git.PushedRef{{LocalRef: "refs/heads/feature", LocalID: head, RemoteRef: "refs/heads/feature", RemoteID: null}})
		slices.Sort(created)
		if err != nil || !slices.Equal(created, []string{"b.txt", "pkgé/c.txt"}) {
			return fmt.Errorf("new branch = %q, %v", created, err)
		}
		deleted, err := r.PushChangedFiles([]git.PushedRef{{LocalRef: "(delete)", LocalID: null, RemoteRef: "refs/heads/old", RemoteID: head}})
		if err != nil || len(deleted) != 0 {
			return fmt.Errorf("deletion = %q, %v", deleted, err)
		}
		return nil
	})
}
