package github

import (
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

var portal = Repository{Owner: "acme", Name: "portal"}

// withClient runs fn with a client bound to a throwaway command of the given
// classification, the observe allowlist installed.
func withClient(t *testing.T, effect string, dryRun bool, fn func(c Client) error) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: effect, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		c, err := New(ctx.Effects())
		if err != nil {
			return err
		}
		return fn(c)
	})
}

func mustPass(t *testing.T, r strictcli.Result) {
	t.Helper()
	if r.ExitCode != 0 {
		t.Fatalf("exit %d\nstdout:\n%s\nstderr:\n%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestRepositoryFromRemoteURL(t *testing.T) {
	hygiene.Isolate(t)
	for url, want := range map[string]string{
		"git@github.com:acme/portal.git":      "acme/portal",
		"git@gw:acme/portal.git":              "acme/portal",
		"gp:acme/portal":                      "acme/portal",
		"https://github.com/acme/portal.git":  "acme/portal",
		"https://github.com/acme/portal":      "acme/portal",
		"https://github.com/acme/portal/":     "acme/portal",
		"https://host/group/acme/portal.git":  "acme/portal",
		"http://github.com/acme/widget-2.git": "acme/widget-2",
	} {
		got, err := RepositoryFromRemoteURL(url)
		if err != nil || got.String() != want {
			t.Errorf("%s: %v, %v; want %s", url, got, err, want)
		}
	}
	for _, url := range []string{"", "/srv/git/portal.git", "file:///srv/portal", "git@github.com:portal.git", "git@github.com:a/b/c.git", "https://github.com/portal"} {
		if got, err := RepositoryFromRemoteURL(url); err == nil {
			t.Errorf("%q was read as %v", url, got)
		}
	}
}

func TestResolveRepositoryPrefersTheDeclaredSlug(t *testing.T) {
	hygiene.Isolate(t)
	got, err := ResolveRepository("acme/gadget", "git@github.com:acme/portal.git")
	if err != nil || got.String() != "acme/gadget" {
		t.Fatalf("%v, %v", got, err)
	}
	got, err = ResolveRepository("", "git@github.com:acme/portal.git")
	if err != nil || got.String() != "acme/portal" {
		t.Fatalf("%v, %v", got, err)
	}
	if _, err := ResolveRepository("", ""); err == nil || !strings.Contains(err.Error(), "github_repository") {
		t.Fatalf("no source: %v", err)
	}
	if _, err := ResolveRepository("portal", ""); err == nil {
		t.Fatal("a slug without an owner was accepted")
	}
}

func TestReleaseExistsReadsOnlyReleaseNotFoundAsAbsence(t *testing.T) {
	hygiene.Isolate(t)
	view := func(tag string) []string {
		return []string{"release", "view", tag, "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}
	}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: view("v1.0.0"), Stdout: "v1.0.0\n"},
		testsupport.GHAnswer{Args: view("v2.0.0"), Stderr: "release not found\n", Exit: 1},
		testsupport.GHAnswer{Args: view("v3.0.0"), Stderr: "HTTP 502: Bad Gateway\n", Exit: 1},
	)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		if ok, err := c.ReleaseExists(portal, "v1.0.0"); err != nil || !ok {
			t.Errorf("v1.0.0: %v, %v", ok, err)
		}
		if ok, err := c.ReleaseExists(portal, "v2.0.0"); err != nil || ok {
			t.Errorf("v2.0.0: %v, %v", ok, err)
		}
		if _, err := c.ReleaseExists(portal, "v3.0.0"); err == nil || !strings.Contains(err.Error(), "Bad Gateway") {
			t.Errorf("v3.0.0: %v", err)
		}
		return nil
	}))
}

func TestReleaseTagsRefusesAListingAtItsCap(t *testing.T) {
	hygiene.Isolate(t)
	list := []string{"release", "list", "--repo", "acme/portal", "--limit", "1000", "--json", "tagName", "--jq", ".[].tagName"}
	full := strings.Repeat("v0.0.1\n", ReleaseListLimit)
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: list, Stdout: "v0.2.0\nv0.1.0\n"},
		testsupport.GHAnswer{Args: list, Stdout: full},
		testsupport.GHAnswer{Args: list, Stderr: "HTTP 401: Bad credentials\n", Exit: 1},
	)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		if tags, err := c.ReleaseTags(portal); err != nil || !slices.Equal(tags, []string{"v0.2.0", "v0.1.0"}) {
			t.Errorf("first listing: %v, %v", tags, err)
		}
		if _, err := c.ReleaseTags(portal); err == nil || !strings.Contains(err.Error(), "limit") {
			t.Errorf("a capped listing: %v", err)
		}
		if _, err := c.ReleaseTags(portal); err == nil || !strings.Contains(err.Error(), "Bad credentials") {
			t.Errorf("a failed listing: %v", err)
		}
		return nil
	}))
}

func TestCreateReleaseVerifiesTheTagAndPipesTheNotes(t *testing.T) {
	hygiene.Isolate(t)
	create := []string{"release", "create", "v0.4.0", "--repo", "acme/portal", "--title", "v0.4.0", "--notes-file", "-", "--verify-tag", "--latest=false"}
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: create})
	mustPass(t, withClient(t, strictcli.EffectMutating, false, func(c Client) error {
		return c.CreateRelease(portal, NewRelease{Tag: "v0.4.0", Title: "v0.4.0", Notes: "<!-- rlsbl-ci-sha: x -->\nnotes", MovesLatest: false})
	}))
	calls := gh.Calls()
	if len(calls) != 1 || !slices.Equal(calls[0].Args, create) || calls[0].Stdin != "<!-- rlsbl-ci-sha: x -->\nnotes" {
		t.Fatalf("calls: %+v", calls)
	}
}

func TestADryRunRecordsReleaseWritesWithoutRunningThem(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t)
	r := withClient(t, strictcli.EffectMutating, true, func(c Client) error {
		if err := c.RewriteRelease(portal, "v0.4.0", ReleaseDocument{Notes: "yanked", Prerelease: true}); err != nil {
			return err
		}
		return c.DeleteRelease(portal, "v0.3.0")
	})
	mustPass(t, r)
	if calls := gh.Calls(); len(calls) != 0 {
		t.Fatalf("a dry run ran gh: %+v", calls)
	}
	if !strings.Contains(r.Stdout+r.Stderr, "release edit v0.4.0") || !strings.Contains(r.Stdout+r.Stderr, "release delete v0.3.0") {
		t.Fatalf("the preview does not name the writes:\n%s%s", r.Stdout, r.Stderr)
	}
}

func TestRewriteReleaseStatesThePrereleaseFlagBothWays(t *testing.T) {
	hygiene.Isolate(t)
	on := []string{"release", "edit", "v0.4.0", "--repo", "acme/portal", "--notes-file", "-", "--title", "v0.4.0 (yanked)", "--prerelease"}
	off := []string{"release", "edit", "v0.4.0", "--repo", "acme/portal", "--notes-file", "-", "--prerelease=false"}
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: on}, testsupport.GHAnswer{Args: off})
	mustPass(t, withClient(t, strictcli.EffectMutating, false, func(c Client) error {
		if err := c.RewriteRelease(portal, "v0.4.0", ReleaseDocument{Notes: "a", Title: "v0.4.0 (yanked)", Prerelease: true}); err != nil {
			return err
		}
		return c.RewriteRelease(portal, "v0.4.0", ReleaseDocument{Notes: "b"})
	}))
	if calls := gh.Calls(); len(calls) != 2 || !slices.Equal(calls[0].Args, on) || !slices.Equal(calls[1].Args, off) {
		t.Fatalf("calls: %+v", calls)
	}
}

func TestLatestReleaseTag(t *testing.T) {
	hygiene.Isolate(t)
	view := []string{"release", "view", "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: view, Stdout: "v0.3.0\n"},
		testsupport.GHAnswer{Args: view, Stderr: "release not found\n", Exit: 1},
		testsupport.GHAnswer{Args: view, Stderr: "connection refused\n", Exit: 1},
	)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		if tag, ok, err := c.LatestReleaseTag(portal); err != nil || !ok || tag != "v0.3.0" {
			t.Errorf("with a Latest: %q %v %v", tag, ok, err)
		}
		if _, ok, err := c.LatestReleaseTag(portal); err != nil || ok {
			t.Errorf("without one: %v %v", ok, err)
		}
		if _, _, err := c.LatestReleaseTag(portal); err == nil {
			t.Error("an unanswered question read as no Latest")
		}
		return nil
	}))
}

func TestTheReleasedCommitMarker(t *testing.T) {
	hygiene.Isolate(t)
	sha := strings.Repeat("ab", 20)
	marker, err := CISHAMarker(sha)
	if err != nil || marker != "<!-- rlsbl-ci-sha: "+sha+" -->" {
		t.Fatalf("%q, %v", marker, err)
	}
	if _, err := CISHAMarker("abc123"); err == nil {
		t.Fatal("an abbreviated id made a marker")
	}
	body := marker + "\n## Fixes\n- one\n"
	if got, ok := CISHAFromBody(body); !ok || got != sha {
		t.Fatalf("read back %q %v", got, ok)
	}
	if got := StripCISHAMarker(body); got != "## Fixes\n- one\n" {
		t.Fatalf("stripped %q", got)
	}
	if _, ok := CISHAFromBody("no marker here"); ok {
		t.Fatal("a body without a marker named a commit")
	}
}

func TestInfoReadsVisibilityAndRefusesAnUnknownOne(t *testing.T) {
	hygiene.Isolate(t)
	get := []string{"api", "--method", "GET", "repos/acme/portal"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: get, Stdout: `{"full_name":"acme/portal","visibility":"private","private":true,"archived":false,"permissions":{"push":true}}`},
		testsupport.GHAnswer{Args: get, Stdout: `{"full_name":"acme/portal","visibility":"secret","private":true}`},
		testsupport.GHAnswer{Args: get, Stdout: `{"full_name":"acme/portal","visibility":"public","private":true}`},
	)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		info, err := c.Info(portal)
		if err != nil || info.Visibility != Private || !info.CanPush || info.Archived {
			t.Errorf("%+v, %v", info, err)
		}
		if _, err := c.Info(portal); err == nil {
			t.Error("an unknown visibility was accepted")
		}
		if _, err := c.Info(portal); err == nil {
			t.Error("a public repository reported private was accepted")
		}
		return nil
	}))
}

func TestSecretPresenceAbsenceAndUnknown(t *testing.T) {
	hygiene.Isolate(t)
	get := []string{"api", "--method", "GET", "repos/acme/portal/actions/secrets/NPM_TOKEN"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: get, Stdout: `{"name":"NPM_TOKEN","updated_at":"2026-01-02T03:04:05Z"}`},
		testsupport.GHAnswer{Args: get, Stderr: "gh: Not Found (HTTP 404)\n", Exit: 1},
		testsupport.GHAnswer{Args: get, Stderr: "gh: Resource not accessible by integration (HTTP 403)\n", Exit: 1},
	)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		if s, err := c.Secret(portal, "NPM_TOKEN"); err != nil || !s.Present || s.UpdatedAt != "2026-01-02T03:04:05Z" {
			t.Errorf("present: %+v %v", s, err)
		}
		if s, err := c.Secret(portal, "NPM_TOKEN"); err != nil || s.Present {
			t.Errorf("absent: %+v %v", s, err)
		}
		if _, err := c.Secret(portal, "NPM_TOKEN"); err == nil || !strings.Contains(err.Error(), "403") {
			t.Errorf("forbidden: %v", err)
		}
		return nil
	}))
}

func TestSetSecretPipesTheValueAndNeverPutsItInTheArgv(t *testing.T) {
	hygiene.Isolate(t)
	set := []string{"secret", "set", "NPM_TOKEN", "--repo", "acme/portal"}
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: set})
	mustPass(t, withClient(t, strictcli.EffectMutating, false, func(c Client) error {
		return c.SetSecret(portal, "NPM_TOKEN", []byte("npm_secretvalue"))
	}))
	calls := gh.Calls()
	if len(calls) != 1 || calls[0].Stdin != "npm_secretvalue" || slices.Contains(calls[0].Args, "npm_secretvalue") {
		t.Fatalf("calls: %+v", calls)
	}
}

func TestEnsureTopicAddsOnlyAMissingTopic(t *testing.T) {
	hygiene.Isolate(t)
	get := []string{"api", "--method", "GET", "repos/acme/portal/topics"}
	put := []string{"api", "--method", "PUT", "repos/acme/portal/topics", "-f", "names[]=cli", "-f", "names[]=rlsbl"}
	gh := testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: get, Stdout: `{"names":["cli"]}`},
		testsupport.GHAnswer{Args: get, Stdout: `{"names":["cli","rlsbl"]}`},
		testsupport.GHAnswer{Args: put},
	)
	mustPass(t, withClient(t, strictcli.EffectMutating, false, func(c Client) error {
		if changed, err := c.EnsureTopic(portal, "rlsbl"); err != nil || !changed {
			t.Errorf("first: %v %v", changed, err)
		}
		if changed, err := c.EnsureTopic(portal, "rlsbl"); err != nil || changed {
			t.Errorf("second: %v %v", changed, err)
		}
		return nil
	}))
	var puts int
	for _, call := range gh.Calls() {
		if slices.Equal(call.Args, put) {
			puts++
		}
	}
	if puts != 1 {
		t.Fatalf("%d topic writes, want 1", puts)
	}
}

func TestSearchRefusesAnIncompleteListing(t *testing.T) {
	hygiene.Isolate(t)
	search := []string{"api", "--method", "GET", "--paginate", "search/repositories?q=topic:rlsbl&sort=updated&per_page=100", "--jq", `"total\t\(.total_count)", (.items[] | [.full_name, (.description // ""), .updated_at, .owner.login] | @tsv)`}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: search, Stdout: "total\t2\nacme/portal\tA portal\\twith a tab\t2026-01-01T00:00:00Z\tacme\nacme/widget\t\t2026-01-02T00:00:00Z\tacme\n"},
		testsupport.GHAnswer{Args: search, Stdout: "total\t1500\nacme/portal\tA portal\t2026-01-01T00:00:00Z\tacme\n"},
	)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		found, err := c.SearchRepositoriesByTopic("rlsbl")
		if err != nil || len(found) != 2 || found[0].Description != "A portal\twith a tab" || found[1].Owner != "acme" {
			t.Errorf("%+v, %v", found, err)
		}
		if _, err := c.SearchRepositoriesByTopic("rlsbl"); err == nil || !strings.Contains(err.Error(), "incomplete") {
			t.Errorf("an incomplete listing: %v", err)
		}
		return nil
	}))
}

func TestCheckAuthNamesTheLogin(t *testing.T) {
	hygiene.Isolate(t)
	status := []string{"auth", "status", "--hostname", "github.com"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: status, Stderr: "You are not logged into any GitHub hosts.\n", Exit: 1},
		testsupport.GHAnswer{Args: status, Stdout: "Logged in to github.com\n"},
	)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		if err := c.CheckAuth(); err == nil || !strings.Contains(err.Error(), "gh auth login") {
			t.Errorf("logged out: %v", err)
		}
		// The login the error names clears it.
		if err := c.CheckAuth(); err != nil {
			t.Errorf("logged in: %v", err)
		}
		return nil
	}))
}

func TestRunsRefusesAListingAtItsLimit(t *testing.T) {
	hygiene.Isolate(t)
	sha := strings.Repeat("c", 40)
	list := []string{"run", "list", "--repo", "acme/portal", "--commit", sha, "--limit", "2", "--json", runFields}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: list, Stdout: `[{"databaseId":7,"name":"CI","workflowName":"CI","status":"completed","conclusion":"success","headSha":"` + sha + `"}]`},
		testsupport.GHAnswer{Args: list, Stdout: `[{"databaseId":7},{"databaseId":8}]`},
	)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		runs, err := c.Runs(portal, RunQuery{Commit: sha, Limit: 2})
		if err != nil || len(runs) != 1 || runs[0].ID != 7 || runs[0].Conclusion != "success" {
			t.Errorf("%+v, %v", runs, err)
		}
		if _, err := c.Runs(portal, RunQuery{Commit: sha, Limit: 2}); err == nil {
			t.Error("a listing at its limit was accepted")
		}
		if _, err := c.Runs(portal, RunQuery{Commit: sha}); err == nil {
			t.Error("a listing without a limit was accepted")
		}
		return nil
	}))
}

func TestAPIGetRefusesTheRepositoryPlaceholders(t *testing.T) {
	hygiene.Isolate(t)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		if _, err := c.APIGet("repos/{owner}/{repo}/topics", false, ""); err == nil {
			t.Error("a placeholder path was sent")
		}
		return nil
	}))
}
