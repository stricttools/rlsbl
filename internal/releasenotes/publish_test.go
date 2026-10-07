package releasenotes_test

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/releasenotes"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

var portal = github.Repository{Owner: "acme", Name: "portal"}

// The gh argvs the client issues for v0.4.0 of acme/portal.
var (
	viewBody   = []string{"release", "view", "v0.4.0", "--repo", "acme/portal", "--json", "body", "--jq", ".body"}
	viewExists = []string{"release", "view", "v0.4.0", "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}
	viewLatest = []string{"release", "view", "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}
	create     = []string{"release", "create", "v0.4.0", "--repo", "acme/portal", "--title", "v0.4.0", "--notes-file", "-", "--verify-tag"}
	editNotes  = []string{"release", "edit", "v0.4.0", "--repo", "acme/portal", "--notes-file", "-"}
	rewrite    = []string{"release", "edit", "v0.4.0", "--repo", "acme/portal", "--notes-file", "-", "--title", "v0.4.0", "--prerelease=false"}
	rewritePre = []string{"release", "edit", "v0.4.0", "--repo", "acme/portal", "--notes-file", "-", "--title", "v0.4.0", "--prerelease"}
)

// withGH runs fn with a gh client in a mutating command carrying rlsbl's
// observe allowlist, and returns fn's error.
func withGH(t *testing.T, fn func(gh github.Client) error) error {
	t.Helper()
	var ferr error
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		gh, err := github.New(ctx.Effects())
		if err != nil {
			ferr = err
			return err
		}
		ferr = fn(gh)
		return ferr
	})
	return ferr
}

func doc(t *testing.T, notices ...string) releasenotes.Document {
	t.Helper()
	return releasenotes.Document{Tag: "v0.4.0", Version: version(t, "0.4.0"), Notes: "- Fixed bug Y", Notices: notices, ReleaseCommit: commitA}
}

func requireCalls(t *testing.T, calls []testsupport.GHCall, want ...[]string) {
	t.Helper()
	if len(calls) != len(want) {
		t.Fatalf("calls %+v, want %v", calls, want)
	}
	for i := range want {
		if !slices.Equal(calls[i].Args, want[i]) {
			t.Fatalf("call %d is %q, want %q", i, calls[i].Args, want[i])
		}
	}
}

func TestCreateWritesTheComposedDocument(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: create})
	if err := withGH(t, func(c github.Client) error {
		return releasenotes.Create(c, portal, scanner(t, ""), doc(t), true)
	}); err != nil {
		t.Fatal(err)
	}
	calls := gh.Calls()
	requireCalls(t, calls, create)
	if calls[0].Stdin != "- Fixed bug Y\n\n"+markerA+"\n" {
		t.Fatalf("notes %q", calls[0].Stdin)
	}
}

func TestARepairCreationKeepsTheLatestBadgeWhereItIs(t *testing.T) {
	hygiene.Isolate(t)
	repair := append(append([]string(nil), create...), "--latest=false")
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: repair})
	if err := withGH(t, func(c github.Client) error {
		return releasenotes.Create(c, portal, scanner(t, ""), doc(t), false)
	}); err != nil {
		t.Fatal(err)
	}
	requireCalls(t, gh.Calls(), repair)
}

func TestRewriteEditsInPlaceAndStatesTheFlag(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: rewrite}, testsupport.GHAnswer{Args: rewritePre})
	if err := withGH(t, func(c github.Client) error {
		if err := releasenotes.Rewrite(c, portal, scanner(t, ""), doc(t)); err != nil {
			return err
		}
		return releasenotes.Rewrite(c, portal, scanner(t, ""), doc(t, deprecated))
	}); err != nil {
		t.Fatal(err)
	}
	calls := gh.Calls()
	requireCalls(t, calls, rewrite, rewritePre)
	if calls[1].Stdin != deprecated+"\n\n- Fixed bug Y\n\n"+markerA+"\n" {
		t.Fatalf("notes %q", calls[1].Stdin)
	}
	for _, c := range calls {
		if c.Args[1] == "delete" || c.Args[1] == "create" {
			t.Fatalf("a rewrite deleted or created a Release: %q", c.Args)
		}
	}
}

func TestRewriteOfAnUnrecoverableVersionKeepsTheMarkerGitHubHolds(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: viewBody, Stdout: "old notes\n\n" + markerB + "\n"},
		testsupport.GHAnswer{Args: rewrite},
	)
	d := doc(t)
	d.ReleaseCommit = ""
	if err := withGH(t, func(c github.Client) error { return releasenotes.Rewrite(c, portal, scanner(t, ""), d) }); err != nil {
		t.Fatal(err)
	}
	calls := gh.Calls()
	requireCalls(t, calls, viewBody, rewrite)
	if calls[1].Stdin != "- Fixed bug Y\n\n"+markerB+"\n" {
		t.Fatalf("notes %q", calls[1].Stdin)
	}
}

func TestEnsureMarkerWritesOnlyWhenTheMarkerIsMissingOrStale(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: viewBody, Stdout: "notes\n\n" + markerA + "\n"},
		testsupport.GHAnswer{Args: viewBody, Stdout: "notes\n\n" + markerB + "\n"},
		testsupport.GHAnswer{Args: editNotes},
	)
	var first, second bool
	if err := withGH(t, func(c github.Client) error {
		var err error
		if first, err = releasenotes.EnsureMarker(c, portal, scanner(t, ""), doc(t)); err != nil {
			return err
		}
		second, err = releasenotes.EnsureMarker(c, portal, scanner(t, ""), doc(t))
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if first || !second {
		t.Fatalf("wrote %v then %v", first, second)
	}
	calls := gh.Calls()
	requireCalls(t, calls, viewBody, viewBody, editNotes)
	if calls[2].Stdin != "notes\n\n"+markerA+"\n" {
		t.Fatalf("notes %q", calls[2].Stdin)
	}
}

func TestPublishCreatesOnlyWhatDoesNotExist(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: viewExists, Stderr: "release not found\n", Exit: 1},
		testsupport.GHAnswer{Args: viewExists, Stdout: "v0.4.0\n"},
		testsupport.GHAnswer{Args: create},
		testsupport.GHAnswer{Args: rewrite},
	)
	var created []bool
	if err := withGH(t, func(c github.Client) error {
		for range 2 {
			was, err := releasenotes.Publish(c, portal, scanner(t, ""), doc(t), true)
			if err != nil {
				return err
			}
			created = append(created, was)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(created, []bool{true, false}) {
		t.Fatalf("created %v", created)
	}
	requireCalls(t, gh.Calls(), viewExists, create, viewExists, rewrite)
}

func TestPublishRefusesWhenGitHubCannotSay(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: viewExists, Stderr: "HTTP 502\n", Exit: 1})
	err := withGH(t, func(c github.Client) error {
		_, err := releasenotes.Publish(c, portal, scanner(t, ""), doc(t), true)
		return err
	})
	if err == nil {
		t.Fatal("an unanswered existence question was read as an answer")
	}
	requireCalls(t, gh.Calls(), viewExists)
}

func TestRepairTakesLatest(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: viewLatest, Stderr: "release not found\n", Exit: 1},
		testsupport.GHAnswer{Args: viewLatest, Stdout: "v0.3.0\n"},
	)
	var none, newer, older, pre bool
	if err := withGH(t, func(c github.Client) error {
		var err error
		if none, err = releasenotes.RepairTakesLatest(c, portal, doc(t), nil); err != nil {
			return err
		}
		if newer, err = releasenotes.RepairTakesLatest(c, portal, doc(t), func(latest string) (bool, error) { return latest == "v0.3.0", nil }); err != nil {
			return err
		}
		if older, err = releasenotes.RepairTakesLatest(c, portal, doc(t), func(string) (bool, error) { return false, nil }); err != nil {
			return err
		}
		pre, err = releasenotes.RepairTakesLatest(c, portal, doc(t, deprecated), nil)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if !none || !newer || older || pre {
		t.Fatalf("no Latest %v, newer %v, older %v, deprecated %v", none, newer, older, pre)
	}
}

func TestTagNewerInHistory(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("a.txt", "a\n", "first")
	repo.Git("tag", "v0.3.0")
	repo.CommitFile("b.txt", "b\n", "second")
	repo.Git("tag", "v0.4.0")
	repo.Git("tag", "widget@v0.1.0")
	var answers []bool
	var missing error
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		r, err := git.Open(ctx.Effects(), repo.Dir)
		if err != nil {
			return err
		}
		for _, pair := range [][2]string{{"v0.4.0", "v0.3.0"}, {"v0.3.0", "v0.4.0"}, {"v0.4.0", "widget@v0.1.0"}} {
			newer, err := releasenotes.TagNewerInHistory(r, pair[0], pair[1])
			if err != nil {
				return err
			}
			answers = append(answers, newer)
		}
		_, missing = releasenotes.TagNewerInHistory(r, "v0.4.0", "v0.2.0")
		return nil
	})
	if !slices.Equal(answers, []bool{true, false, false}) {
		t.Fatalf("answers %v", answers)
	}
	if missing == nil || !strings.Contains(missing.Error(), "git fetch origin --tags") {
		t.Fatalf("a missing tag: %v", missing)
	}
}

// The missing-tag refusal names fetching the tags; once the tag exists
// locally the question is answered.
func TestAMissingTagIsAnsweredOnceTheTagExists(t *testing.T) {
	hygiene.Isolate(t)
	upstream := testsupport.NewRepo(t)
	upstream.CommitFile("a.txt", "a\n", "first")
	upstream.Git("tag", "v0.3.0")
	upstream.CommitFile("b.txt", "b\n", "second")
	upstream.Git("tag", "v0.4.0")
	clone := upstream.Clone()
	clone.Git("tag", "-d", "v0.3.0")
	ask := func() (bool, error) {
		var newer bool
		var ferr error
		testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
			r, err := git.Open(ctx.Effects(), clone.Dir)
			if err != nil {
				return err
			}
			newer, ferr = releasenotes.TagNewerInHistory(r, "v0.4.0", "v0.3.0")
			return nil
		})
		return newer, ferr
	}
	if _, err := ask(); err == nil {
		t.Fatal("a missing tag was answered")
	}
	clone.Git("fetch", "origin", "--tags")
	if newer, err := ask(); err != nil || !newer {
		t.Fatalf("after fetching the tags: %v, %v", newer, err)
	}
}

// diskWriter performs the index's writes, for a fixture.
type diskWriter struct{}

func (diskWriter) WriteFile(path string, data []byte) error { return os.WriteFile(path, data, 0o644) }
func (diskWriter) MkdirAll(path string) error               { return os.MkdirAll(path, 0o755) }

// scanner is the confidential-name scanner of a public repository whose
// index holds the name confidential, none when it is empty.
func scanner(t *testing.T, confidential string) *publishrules.Scanner {
	t.Helper()
	idx, err := index.Load(filepath.Join(t.TempDir(), index.FileName))
	if err != nil {
		t.Fatal(err)
	}
	if confidential != "" {
		if err := idx.Upsert(diskWriter{}, "https://github.com/acme/gadget.git", []string{confidential}); err != nil {
			t.Fatal(err)
		}
	}
	s, err := publishrules.NewScanner(&lifecycle.Record{}, time.Now(), idx)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestEveryReleaseBodyWriterRefusesAConfidentialName(t *testing.T) {
	hygiene.Isolate(t)
	leaky := releasenotes.Document{Tag: "v0.4.0", Version: version(t, "0.4.0"), Notes: "- Talks to Moonbeam now", ReleaseCommit: commitA}
	gh := testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: viewBody, Stdout: "- Talks to Moonbeam now\n"},
		testsupport.GHAnswer{Args: viewExists, Stdout: "v0.4.0\n"})
	writers := map[string]func(c github.Client, s *publishrules.Scanner, d releasenotes.Document) error{
		"Create": func(c github.Client, s *publishrules.Scanner, d releasenotes.Document) error {
			return releasenotes.Create(c, portal, s, d, true)
		},
		"Rewrite": func(c github.Client, s *publishrules.Scanner, d releasenotes.Document) error {
			return releasenotes.Rewrite(c, portal, s, d)
		},
		"EnsureMarker": func(c github.Client, s *publishrules.Scanner, d releasenotes.Document) error {
			_, err := releasenotes.EnsureMarker(c, portal, s, d)
			return err
		},
		"Publish": func(c github.Client, s *publishrules.Scanner, d releasenotes.Document) error {
			_, err := releasenotes.Publish(c, portal, s, d, true)
			return err
		},
	}
	for name, write := range writers {
		err := withGH(t, func(c github.Client) error { return write(c, scanner(t, "moonbeam"), leaky) })
		if err == nil || !strings.Contains(err.Error(), "confidential") || !strings.Contains(err.Error(), "the GitHub Release body of v0.4.0") {
			t.Errorf("%s wrote a body naming a confidential name: %v", name, err)
		}
		if err := withGH(t, func(c github.Client) error { return write(c, nil, doc(t)) }); err == nil {
			t.Errorf("%s wrote without a scanner", name)
		}
	}
	if err := withGH(t, func(c github.Client) error { return releasenotes.Check(scanner(t, "moonbeam"), leaky) }); err == nil {
		t.Error("Check passed a body naming a confidential name")
	}
	for _, c := range gh.Calls() {
		if c.Args[1] == "create" || c.Args[1] == "edit" {
			t.Fatalf("a refused body was written: %q", c.Args)
		}
	}
}
