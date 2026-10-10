package cli

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

var discoverSearch = []string{"api", "--method", "GET", "--paginate", "search/repositories?q=topic:rlsbl&sort=updated&per_page=100", "--jq", `"total\t\(.total_count)", (.items[] | [.full_name, (.description // ""), .updated_at, .owner.login] | @tsv)`}

func TestDiscoverListsTheTaggedRepositories(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, testsupport.GHAnswer{Args: discoverSearch, Stdout: "total\t2\nacme/portal\tA portal\t2026-01-01T00:00:00Z\tacme\nacme/widget\t\t2026-01-02T00:00:00Z\tacme\n"})
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"discover", "--no-mine"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "rlsbl ecosystem (2 projects)") || !strings.Contains(r.Stdout, "acme/portal  A portal") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"discover", "--no-mine", "--json"})
	repos := jsonPayload(t, r)["repositories"].([]any)
	if r.ExitCode != 0 || len(repos) != 2 || repos[1].(map[string]any)["full_name"] != "acme/widget" {
		t.Fatalf("exit %d: %v", r.ExitCode, repos)
	}
}

func TestDiscoverMineWithNoneOwned(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: discoverSearch, Stdout: "total\t1\nother/widget\t\t2026-01-02T00:00:00Z\tother\n"},
		testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "user", "--jq", ".login"}, Stdout: "acme\n"},
	)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"discover", "--mine"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "No rlsbl-tagged repositories found for your account.") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

// A search gh cannot answer is an error naming the login, never an empty
// listing.
func TestDiscoverReportsAnUnansweredSearch(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, testsupport.GHAnswer{Args: discoverSearch, Stderr: "HTTP 401: Bad credentials\n", Exit: 1})
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"discover", "--no-mine"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "gh auth login") || strings.Contains(r.Stdout, "No rlsbl-tagged") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

// TestValuesTheCallerMustState: each command line omits a value rlsbl used to
// pick when it was left out, and the parser refuses it, naming what is missing.
func TestValuesTheCallerMustState(t *testing.T) {
	hygiene.Isolate(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	for _, tc := range []struct {
		args []string
		want string
	}{
		{[]string{"discover"}, "--mine"},
		{[]string{"dev", "install", "--target", "global"}, "--uninstall"},
		{[]string{"secrets", "sync-npm-token", "--approve-consequential"}, "--all"},
		{[]string{"release", "edit"}, "version"},
		{[]string{"changelog", "amend", "--version", "0.1.0", "--commits", "abc"}, "--user-facing"},
	} {
		if r := app.Test(tc.args); r.ExitCode != 1 || !strings.Contains(r.Stderr, tc.want) {
			t.Errorf("%v: exit %d, want the parser's refusal (1) naming %s: %s", tc.args, r.ExitCode, tc.want, r.Stderr)
		}
	}
}
