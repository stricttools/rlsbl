package github

import (
	"reflect"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// The jobs of a run are read for its latest attempt, both pages of them.
func TestRunAttemptJobsReadsTheLatestAttemptsPages(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/acme/portal/actions/runs/7"}, Stdout: `{"id": 7, "run_attempt": 2}`},
		testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "--paginate", "repos/acme/portal/actions/runs/7/attempts/2/jobs?per_page=100"},
			Stdout: `{"total_count": 2, "jobs": [{"id": 1, "name": "a", "status": "completed", "conclusion": "success", "run_id": 7, "run_attempt": 2}]}` + "\n" +
				`{"total_count": 2, "jobs": [{"id": 2, "name": "b", "status": "completed", "conclusion": "failure", "run_id": 7, "run_attempt": 2}]}`},
	)
	var jobs []Job
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		var err error
		jobs, err = c.RunAttemptJobs(portal, 7)
		return err
	}))
	if len(jobs) != 2 || jobs[0].Name != "a" || jobs[1].Conclusion != "failure" || jobs[1].RunAttempt != 2 {
		t.Fatalf("got %+v", jobs)
	}
}

// GitHub not knowing a workflow is an answer; any other failure is an
// error.
func TestWorkflowStateReadsANotFoundAsUnknown(t *testing.T) {
	hygiene.Isolate(t)
	view := func(file string) []string {
		return []string{"api", "--method", "GET", "repos/acme/portal/actions/workflows/" + file, "--jq", ".state"}
	}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: view("publish.yml"), Stdout: "disabled_manually\n"},
		testsupport.GHAnswer{Args: view("new.yml"), Stderr: "gh: Not Found (HTTP 404)\n", Exit: 1},
		testsupport.GHAnswer{Args: view("limited.yml"), Stderr: "gh: API rate limit exceeded (HTTP 403)\n", Exit: 1},
	)
	type answer struct {
		state string
		known bool
		err   bool
	}
	got := map[string]answer{}
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		for _, f := range []string{"publish.yml", "new.yml", "limited.yml"} {
			s, k, err := c.WorkflowState(portal, f)
			got[f] = answer{s, k, err != nil}
		}
		return nil
	}))
	want := map[string]answer{"publish.yml": {"disabled_manually", true, false}, "new.yml": {"", false, false}, "limited.yml": {"", false, true}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v", got)
	}
}

func TestWorkflowRunsAtRefusesAListingInPart(t *testing.T) {
	hygiene.Isolate(t)
	list := []string{"api", "--method", "GET", "repos/acme/portal/actions/workflows/publish.yml/runs?head_sha=abc&per_page=100"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: list, Stdout: `{"total_count": 1, "workflow_runs": [{"id": 3, "head_branch": "v1.0.0", "event": "release"}]}`},
		testsupport.GHAnswer{Args: list, Stdout: `{"total_count": 150, "workflow_runs": []}`},
	)
	mustPass(t, withClient(t, strictcli.EffectReadOnly, false, func(c Client) error {
		runs, err := c.WorkflowRunsAt(portal, "publish.yml", "abc")
		if err != nil || len(runs) != 1 || runs[0].HeadBranch != "v1.0.0" {
			t.Fatalf("got %v, %v", runs, err)
		}
		if _, err := c.WorkflowRunsAt(portal, "publish.yml", "abc"); err == nil {
			t.Fatal("a listing in part was read")
		}
		return nil
	}))
}
