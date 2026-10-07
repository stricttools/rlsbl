package cli

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestWatchRefusesACommitBesideRunIDs(t *testing.T) {
	hygiene.Isolate(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"watch", "HEAD", "--run-id", "5"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "not both") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestWatchRefusesARunIDThatIsNotANumber(t *testing.T) {
	hygiene.Isolate(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"watch", "--run-id", "five"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, `"five" is not one`) {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

// The runs --run-id names are read and watched in the repository the origin
// remote names, and the payload says how each ended.
func TestWatchWatchesTheNamedRuns(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Git("remote", "add", "origin", "https://github.com/acme/portal.git")
	hygiene.Chdir(t, repo.Dir)
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: []string{"run", "view", "5", "--repo", "acme/portal", "--json", "databaseId,name,workflowName,status,conclusion,headBranch,headSha,event"}, Stdout: `{"databaseId": 5, "name": "CI"}`},
		testsupport.GHAnswer{Args: []string{"run", "watch", "5", "--repo", "acme/portal", "--exit-status"}},
	)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"watch", "--run-id", "5", "--json"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	payload := jsonPayload(t, r)
	runs, _ := payload["runs"].([]any)
	if payload["passed"] != true || len(runs) != 1 {
		t.Fatalf("payload: %v", payload)
	}
	run := runs[0].(map[string]any)
	if run["name"] != "CI" || run["run_id"].(float64) != 5 || run["passed"] != true {
		t.Fatalf("run: %v", run)
	}
}
