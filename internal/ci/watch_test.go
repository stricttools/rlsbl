package ci

import (
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestClassificationTriesInfrastructureFirst(t *testing.T) {
	hygiene.Isolate(t)
	for log, want := range map[string]FailureClass{
		"":      Infrastructure,
		"   \n": Infrastructure,
		"The job was not acquired by a runner\nFAIL x": Infrastructure,
		"--- FAIL: TestX\nconnection reset by peer":    Deterministic,
		"dial tcp 1.2.3.4:443: i/o timeout":            Transient,
		"something nobody has seen":                    Unrecognized,
	} {
		if got := ClassifyFailure(log); got != want {
			t.Errorf("%q: got %s, want %s", log, got, want)
		}
	}
}

func TestTheFailureRegionKeepsTheErrorMarksWithTheirContext(t *testing.T) {
	hygiene.Isolate(t)
	var lines []string
	for i := 0; i < 20; i++ {
		lines = append(lines, "line")
	}
	lines[10] = "##[error]boom"
	got := FailureRegion(lines)
	if len(got) != 6 || got[5] != "##[error]boom" {
		t.Fatalf("got %v", got)
	}
	if got := FailureRegion([]string{"a", "b"}); !reflect.DeepEqual(got, []string{"a", "b"}) {
		t.Fatalf("a log without marks: %v", got)
	}
}

func TestAKnownFailureOutranksAnOpenQuestion(t *testing.T) {
	hygiene.Isolate(t)
	if Aggregate([]RunResult{{Passed: true}, {Passed: true}}) != Green {
		t.Fatal("all passed is not green")
	}
	if Aggregate([]RunResult{{Unresolved: true}, {}}) != Red {
		t.Fatal("a failure beside an unresolved run is not red")
	}
	if Aggregate([]RunResult{{Passed: true}, {Unresolved: true}}) != Timeout {
		t.Fatal("an unresolved run is not a timeout")
	}
}

func TestOnlyPushTriggeredWorkflowsCount(t *testing.T) {
	hygiene.Isolate(t)
	r := testsupport.NewRepo(t)
	r.Write(".github/workflows/ci.yml", "on:\n  push:\n    branches: [main]\njobs: {}\n")
	r.Write(".github/workflows/list.yaml", "on: [pull_request, push]\njobs: {}\n")
	r.Write(".github/workflows/publish.yml", "on:\n  release:\n    types: [published]\njobs: {}\n")
	r.Write(".github/workflows/notes.md", "push")
	got, err := PushTriggeredWorkflows(r.Dir)
	if err != nil || !reflect.DeepEqual(got, []string{"ci.yml", "list.yaml"}) {
		t.Fatalf("got %v, %v", got, err)
	}
	r.Write(".github/workflows/broken.yml", "on: [push\n")
	if _, err := PushTriggeredWorkflows(r.Dir); err == nil {
		t.Fatal("a workflow that is not YAML was read as no evidence")
	}
}

var (
	watch5   = []string{"run", "watch", "5", "--repo", "acme/portal", "--exit-status"}
	state5   = []string{"api", "--method", "GET", "repos/acme/portal/actions/runs/5"}
	jobs5    = []string{"api", "--method", "GET", "--paginate", "repos/acme/portal/actions/runs/5/attempts/1/jobs?per_page=100"}
	log9     = []string{"api", "--method", "GET", "--allow-escape-sequences", "repos/acme/portal/actions/jobs/9/logs"}
	rerun5   = []string{"run", "rerun", "5", "--repo", "acme/portal"}
	rerunAll = []string{"run", "rerun", "5", "--repo", "acme/portal", "--failed"}
)

// withWatcher runs fn with a Watcher over the fake gh, in a mutating
// command, since a watch runs failed runs again.
func withWatcher(t *testing.T, fn func(w Watcher)) []string {
	t.Helper()
	c := newClock()
	var log []string
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		gh, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		fn(Watcher{GH: gh, Repo: portal, Log: func(s string) { log = append(log, s) }, Sleep: c.Sleep, Now: c.Now})
		return nil
	})
	if res.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", res.ExitCode, res.Stdout, res.Stderr)
	}
	return log
}

// A watch gh could not carry through over a run still going is watched
// again, never read as a failure.
func TestAnEndedWatchOverARunStillGoingIsWatchedAgain(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: watch5, Stderr: "HTTP 502\n", Exit: 1},
		testsupport.GHAnswer{Args: watch5},
		testsupport.GHAnswer{Args: state5, Stdout: `{"status": "in_progress", "conclusion": null}`},
	)
	withWatcher(t, func(w Watcher) {
		if got := w.ToConclusion(5, "CI", "portal", time.Hour); got != Passed {
			t.Fatalf("got %s", got)
		}
	})
}

func TestAnUnreadableStateEstablishesNothing(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: watch5, Exit: 1},
		testsupport.GHAnswer{Args: state5, Stderr: "HTTP 404\n", Exit: 1},
	)
	withWatcher(t, func(w Watcher) {
		if got := w.ToConclusion(5, "CI", "portal", time.Hour); got != Unresolved {
			t.Fatalf("got %s", got)
		}
	})
}

func TestADeterministicFailureIsNotRunAgain(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: watch5, Exit: 1},
		testsupport.GHAnswer{Args: state5, Stdout: `{"status": "completed", "conclusion": "failure", "run_attempt": 1}`},
		testsupport.GHAnswer{Args: jobs5, Stdout: `{"jobs": [{"id": 9, "name": "test", "status": "completed", "conclusion": "failure"}]}`},
		testsupport.GHAnswer{Args: log9, Stdout: "ok\n--- FAIL: TestPortal\n##[error]Process completed with exit code 1.\n"},
	)
	log := withWatcher(t, func(w Watcher) {
		res := w.Watch(github.WorkflowRun{ID: 5, Name: "CI"}, "portal", time.Hour, map[string]bool{})
		if res.Passed || res.Unresolved {
			t.Fatalf("got %+v", res)
		}
	})
	for _, call := range gh.Calls() {
		if call.Args[0] == "run" && call.Args[1] == "rerun" {
			t.Fatalf("a deterministic failure was run again: %v", call.Args)
		}
	}
	if !strings.Contains(strings.Join(log, "\n"), "gh run rerun 5 --failed --repo acme/portal") {
		t.Fatalf("the way to run it by hand is not named:\n%s", strings.Join(log, "\n"))
	}
}

func TestAnInfrastructureFailureRunsItsFailedJobsAgainOnce(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: watch5, Exit: 1},
		testsupport.GHAnswer{Args: watch5},
		testsupport.GHAnswer{Args: state5, Stdout: `{"status": "completed", "conclusion": "failure", "run_attempt": 1}`},
		testsupport.GHAnswer{Args: jobs5, Stdout: `{"jobs": [{"id": 9, "name": "test", "status": "completed", "conclusion": "failure"}]}`},
		testsupport.GHAnswer{Args: log9, Stdout: "The job was not acquired by Runner of type hosted\n"},
		testsupport.GHAnswer{Args: rerunAll},
	)
	withWatcher(t, func(w Watcher) {
		retried := map[string]bool{}
		if res := w.Watch(github.WorkflowRun{ID: 5, Name: "CI"}, "portal", time.Hour, retried); !res.Passed {
			t.Fatalf("got %+v", res)
		}
		if !retried["CI"] {
			t.Fatal("the run of CI is not recorded as run again")
		}
	})
	reruns := 0
	for _, call := range gh.Calls() {
		if reflect.DeepEqual(call.Args, rerunAll) {
			reruns++
		}
		if reflect.DeepEqual(call.Args, rerun5) {
			t.Fatal("an infrastructure failure ran every job again")
		}
	}
	if reruns != 1 {
		t.Fatalf("the failed jobs were run again %d times", reruns)
	}
}

const sha = "0123456789abcdef0123456789abcdef01234567"

func TestTheRunAllDispatchIsFoundByCommit(t *testing.T) {
	hygiene.Isolate(t)
	dispatch := []string{"workflow", "run", "ci-router.yml", "--repo", "acme/portal", "--ref", "main", "-f", "run_all=true"}
	list := []string{"run", "list", "--repo", "acme/portal", "--commit", sha, "--workflow", "ci-router.yml", "--event", "workflow_dispatch", "--limit", "100", "--json", "databaseId,name,workflowName,status,conclusion,headBranch,headSha,event"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: dispatch},
		testsupport.GHAnswer{Args: list, Stdout: "[]"},
		testsupport.GHAnswer{Args: list, Stdout: `[{"databaseId": 41, "headSha": "` + sha + `", "event": "workflow_dispatch"}]`},
	)
	withWatcher(t, func(w Watcher) {
		r, err := w.DispatchRunAll("main", sha)
		if err != nil || r.ID != 41 {
			t.Fatalf("got %+v, %v", r, err)
		}
	})

	testsupport.FakeGH(t, testsupport.GHAnswer{Args: dispatch}, testsupport.GHAnswer{Args: list, Stdout: "[]"})
	withWatcher(t, func(w Watcher) {
		if _, err := w.DispatchRunAll("main", sha); err == nil || !strings.Contains(err.Error(), "no dispatched run appeared") {
			t.Fatalf("got %v", err)
		}
	})
}

func TestTheCIWaitReadsTheReleasingProjectsOwnChecks(t *testing.T) {
	hygiene.Isolate(t)
	r := testsupport.NewRepo(t)
	r.Write(".github/workflows/ci-router.yml", "on: [push]\njobs: {}\n")
	list := []string{"run", "list", "--repo", "acme/portal", "--commit", sha, "--limit", "100", "--json", "databaseId,name,workflowName,status,conclusion,headBranch,headSha,event"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: list, Stdout: `[{"databaseId": 5, "name": "CI Router", "headSha": "` + sha + `"}]`},
		testsupport.GHAnswer{Args: watch5},
		testsupport.GHAnswer{Args: state5, Stdout: `{"status": "completed", "conclusion": "success", "run_attempt": 1}`},
		testsupport.GHAnswer{Args: jobs5, Stdout: `{"jobs": [{"id": 1, "name": "detect", "status": "completed", "conclusion": "success"}, {"id": 2, "name": "core-ci / test", "status": "completed", "conclusion": "skipped"}]}`},
	)
	withWatcher(t, func(w Watcher) {
		verdict, results, err := w.WaitForCIGreen(WaitInputs{Commit: sha, Root: r.Dir, Timeout: time.Hour, DiscoveryGrace: DiscoveryGrace, Filters: []CheckFilter{{Label: "core", Pattern: corePattern.String()}}})
		var notRun *NotRunError
		if verdict != Green || len(results) != 1 || !errors.As(err, &notRun) || !strings.Contains(err.Error(), "core-ci / test: skipped") {
			t.Fatalf("verdict %s, results %+v, err %v", verdict, results, err)
		}
	})

	empty := testsupport.NewRepo(t)
	withWatcher(t, func(w Watcher) {
		if verdict, _, err := w.WaitForCIGreen(WaitInputs{Commit: sha, Root: empty.Dir, Timeout: time.Hour, DiscoveryGrace: DiscoveryGrace}); verdict != NotConfigured || err != nil {
			t.Fatalf("verdict %s, err %v", verdict, err)
		}
	})
}

func TestAWatchedCommitWithoutRunsFailsNamingTheWatchAgain(t *testing.T) {
	hygiene.Isolate(t)
	list := []string{"run", "list", "--repo", "acme/portal", "--commit", sha, "--limit", "100", "--json", "databaseId,name,workflowName,status,conclusion,headBranch,headSha,event"}
	testsupport.FakeGH(t, testsupport.GHAnswer{Args: list, Stdout: "[]"})
	withWatcher(t, func(w Watcher) {
		if _, err := w.WatchCommit(sha, "portal"); err == nil || !strings.Contains(err.Error(), "rlsbl watch "+sha) {
			t.Fatalf("got %v", err)
		}
	})
}

func TestNamedRunsAreReadThenWatched(t *testing.T) {
	hygiene.Isolate(t)
	view := []string{"run", "view", "5", "--repo", "acme/portal", "--json", "databaseId,name,workflowName,status,conclusion,headBranch,headSha,event"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: view, Stdout: `{"databaseId": 5, "name": "CI"}`},
		testsupport.GHAnswer{Args: watch5},
	)
	withWatcher(t, func(w Watcher) {
		results, err := w.WatchRunIDs([]int64{5}, "runs 5")
		if err != nil || len(results) != 1 || !results[0].Passed || results[0].Name != "CI" {
			t.Fatalf("got %+v, %v", results, err)
		}
	})
}
