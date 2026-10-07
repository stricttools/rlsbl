package ci

import (
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

var portal = github.Repository{Owner: "acme", Name: "portal"}

const publishWorkflow = "name: Publish\non:\n  release:\n    types: [published]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n"
const ciWorkflow = "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n"

func api(path string, extra ...string) []string {
	return append([]string{"api", "--method", "GET", path}, extra...)
}

// clock is a fake clock that Sleep advances.
type clock struct{ now time.Time }

func (c *clock) Now() time.Time { return c.now }

func (c *clock) Sleep(d time.Duration) { c.now = c.now.Add(d) }

func newClock() *clock { return &clock{now: time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)} }

// withPublishRuns runs fn with a PublishRuns over a repository holding the
// files, committed, and returns the commit.
func withPublishRuns(t *testing.T, files map[string]string, c *clock, fn func(p PublishRuns, sha string) error) []string {
	t.Helper()
	r := testsupport.NewRepo(t)
	var paths []string
	for rel, content := range files {
		r.Write(rel, content)
		paths = append(paths, rel)
	}
	sha := r.Commit("workflows", paths...)
	var log []string
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		gh, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		repo, err := git.Open(ctx.Effects(), r.Dir)
		if err != nil {
			return err
		}
		return fn(PublishRuns{GH: gh, Repo: portal, Git: repo, Log: func(s string) { log = append(log, s) }, Sleep: c.Sleep, Now: c.Now}, sha)
	})
	if res.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", res.ExitCode, res.Stdout, res.Stderr)
	}
	return log
}

func TestTheReleaseWorkflowsAreReadFromTheTaggedTree(t *testing.T) {
	hygiene.Isolate(t)
	files := map[string]string{
		".github/workflows/publish.yml": publishWorkflow,
		".github/workflows/ci.yml":      ciWorkflow,
		".github/workflows/notes.yml":   "on:\n  release:\n    types: [edited]\njobs: {}\n",
		".github/workflows/any.yml":     "on: release\njobs: {}\n",
	}
	withPublishRuns(t, files, newClock(), func(p PublishRuns, sha string) error {
		got, err := ReleaseWorkflows(p.Git, sha)
		if err != nil {
			return err
		}
		starts := map[string]bool{}
		for _, w := range got {
			starts[w.File()] = w.Starts()
		}
		if len(got) != 3 || !starts["publish.yml"] || starts["notes.yml"] || !starts["any.yml"] {
			t.Fatalf("got %+v", got)
		}
		return nil
	})
}

func TestPreflightRefusesWhatWouldStartNoPublish(t *testing.T) {
	hygiene.Isolate(t)
	withPublishRuns(t, map[string]string{".github/workflows/ci.yml": ciWorkflow}, newClock(), func(p PublishRuns, sha string) error {
		if err := p.Preflight(sha, true); err == nil || !strings.Contains(err.Error(), "rlsbl scaffold") {
			t.Fatalf("got %v", err)
		}
		if err := p.Preflight(sha, false); err != nil {
			t.Fatalf("a release publishing nothing was refused: %v", err)
		}
		return nil
	})

	state := api("repos/acme/portal/actions/workflows/publish.yml", "--jq", ".state")
	perms := api("repos/acme/portal/actions/permissions")
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: perms, Stdout: `{"enabled": false}`},
		testsupport.GHAnswer{Args: perms, Stdout: `{"enabled": true}`},
		testsupport.GHAnswer{Args: state, Stdout: "disabled_manually\n"},
		testsupport.GHAnswer{Args: state, Stderr: "gh: Not Found (HTTP 404)\n", Exit: 1},
	)
	withPublishRuns(t, map[string]string{".github/workflows/publish.yml": publishWorkflow}, newClock(), func(p PublishRuns, sha string) error {
		if err := p.Preflight(sha, true); err == nil || !strings.Contains(err.Error(), "gh api --method PUT repos/acme/portal/actions/permissions -F enabled=true") {
			t.Fatalf("Actions disabled: %v", err)
		}
		if err := p.Preflight(sha, true); err == nil || !strings.Contains(err.Error(), "gh workflow enable publish.yml --repo acme/portal") {
			t.Fatalf("workflow disabled: %v", err)
		}
		// A workflow GitHub does not know yet arrives with the release's push.
		if err := p.Preflight(sha, true); err != nil {
			t.Fatalf("an unknown workflow was refused: %v", err)
		}
		return nil
	})
}

func TestConfirmationFindsTheRunForTheTag(t *testing.T) {
	hygiene.Isolate(t)
	c := newClock()
	withPublishRuns(t, map[string]string{".github/workflows/publish.yml": publishWorkflow}, c, func(p PublishRuns, sha string) error {
		runs := api("repos/acme/portal/actions/workflows/publish.yml/runs?head_sha=" + sha + "&per_page=100")
		testsupport.FakeGH(t,
			testsupport.GHAnswer{Args: runs, Stdout: `{"total_count": 0, "workflow_runs": []}`},
			testsupport.GHAnswer{Args: runs, Stdout: `{"total_count": 2, "workflow_runs": [{"id": 1, "head_branch": "main", "event": "push"}, {"id": 2, "head_branch": "v1.0.0", "event": "release"}]}`},
		)
		return p.ConfirmRunsStarted("v1.0.0", sha, time.Minute, 5*time.Second)
	})
}

func TestConfirmationRefusesNamingWhatItFound(t *testing.T) {
	hygiene.Isolate(t)
	c := newClock()
	withPublishRuns(t, map[string]string{".github/workflows/publish.yml": publishWorkflow}, c, func(p PublishRuns, sha string) error {
		testsupport.FakeGH(t,
			testsupport.GHAnswer{Args: api("repos/acme/portal/actions/workflows/publish.yml/runs?head_sha=" + sha + "&per_page=100"), Stdout: `{"total_count": 0, "workflow_runs": []}`},
			testsupport.GHAnswer{Args: []string{"release", "view", "v1.0.0", "--repo", "acme/portal", "--json", "publishedAt", "--jq", ".publishedAt"}, Stdout: "2026-10-07T11:59:00Z\n"},
			testsupport.GHAnswer{Args: []string{"release", "view", "v1.0.0", "--repo", "acme/portal", "--json", "author", "--jq", ".author.login"}, Stdout: "github-actions[bot]\n"},
			testsupport.GHAnswer{Args: api("repos/acme/portal/actions/permissions"), Stdout: `{"enabled": true}`},
			testsupport.GHAnswer{Args: api("repos/acme/portal/actions/workflows/publish.yml", "--jq", ".state"), Stdout: "active\n"},
		)
		err := p.ConfirmRunsStarted("v1.0.0", sha, time.Minute, 5*time.Second)
		if err == nil || !strings.Contains(err.Error(), "github-actions[bot]") || !strings.Contains(err.Error(), "rlsbl release retry") {
			t.Fatalf("got %v", err)
		}
		return nil
	})
}

func TestAnUnansweredConfirmationDispatchesNothing(t *testing.T) {
	hygiene.Isolate(t)
	c := newClock()
	withPublishRuns(t, map[string]string{".github/workflows/publish.yml": publishWorkflow}, c, func(p PublishRuns, sha string) error {
		testsupport.FakeGH(t, testsupport.GHAnswer{Args: api("repos/acme/portal/actions/workflows/publish.yml/runs?head_sha=" + sha + "&per_page=100"), Stderr: "gh: API rate limit exceeded (HTTP 403)\n", Exit: 1})
		err := p.ConfirmRunsStarted("v1.0.0", sha, time.Minute, 5*time.Second)
		if err == nil || !strings.Contains(err.Error(), "dispatch nothing yet") || !strings.Contains(err.Error(), "rlsbl watch "+sha) {
			t.Fatalf("got %v", err)
		}
		return nil
	})
}

func TestAReleaseOlderThanTheRetentionIsReportedNotRefused(t *testing.T) {
	hygiene.Isolate(t)
	c := newClock()
	log := withPublishRuns(t, map[string]string{".github/workflows/publish.yml": publishWorkflow}, c, func(p PublishRuns, sha string) error {
		testsupport.FakeGH(t,
			testsupport.GHAnswer{Args: api("repos/acme/portal/actions/workflows/publish.yml/runs?head_sha=" + sha + "&per_page=100"), Stdout: `{"total_count": 0, "workflow_runs": []}`},
			testsupport.GHAnswer{Args: []string{"release", "view", "v1.0.0", "--repo", "acme/portal", "--json", "publishedAt", "--jq", ".publishedAt"}, Stdout: "2026-01-01T00:00:00Z\n"},
			testsupport.GHAnswer{Args: api("repos/acme/portal/actions/permissions/artifact-and-log-retention"), Stdout: `{"days": 90}`},
		)
		return p.ConfirmRunsStarted("v1.0.0", sha, time.Minute, 5*time.Second)
	})
	if !strings.Contains(strings.Join(log, "\n"), "90-day run retention") {
		t.Fatalf("log: %v", log)
	}
}
