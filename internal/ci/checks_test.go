package ci

import (
	"errors"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

var corePattern = regexp.MustCompile(`^(core-ci) / `)

func run(id int64, name, status, conclusion, started string) CheckRun {
	return CheckRun{ID: id, Name: name, Status: status, Conclusion: conclusion, StartedAt: started}
}

func conclusions(runs []CheckRun) map[string]string {
	out := map[string]string{}
	for _, r := range runs {
		out[r.Name] = r.Conclusion
	}
	return out
}

func TestAVerdictSupersedesASkipWhicheverWasRecordedLater(t *testing.T) {
	hygiene.Isolate(t)
	for name, runs := range map[string][]CheckRun{
		"the skip recorded later": {
			run(2, "core-ci / test", "completed", "success", "2026-01-01T00:00:01Z"),
			run(3, "core-ci / test", "completed", "skipped", "2026-01-01T00:00:09Z"),
		},
		"the skip recorded earlier": {
			run(1, "core-ci / test", "completed", "skipped", "2026-01-01T00:00:01Z"),
			run(2, "core-ci / test", "completed", "success", "2026-01-01T00:00:09Z"),
		},
	} {
		if got := conclusions(LatestCheckRuns(runs, corePattern, 0)); got["core-ci / test"] != Passing {
			t.Errorf("%s: %v", name, got)
		}
	}
	failing := []CheckRun{
		run(2, "core-ci / test", "completed", "failure", "2026-01-01T00:00:01Z"),
		run(3, "core-ci / test", "completed", "skipped", "2026-01-01T00:00:09Z"),
	}
	if got := conclusions(LatestCheckRuns(failing, corePattern, 0)); got["core-ci / test"] != "failure" {
		t.Fatalf("a failing dispatched job was hidden: %v", got)
	}
	pending := []CheckRun{
		run(2, "core-ci / test", "in_progress", "", "2026-01-01T00:00:01Z"),
		run(3, "core-ci / test", "completed", "skipped", "2026-01-01T00:00:09Z"),
	}
	if got := conclusions(LatestCheckRuns(pending, corePattern, 0)); got["core-ci / test"] != Skipped {
		t.Fatalf("a pending run counted as a verdict: %v", got)
	}
}

func TestOnlyTheJobsOwnMatrixEntriesCoverItsSkip(t *testing.T) {
	hygiene.Isolate(t)
	entries := []CheckRun{
		run(1, "core-ci / test", "completed", "skipped", "2026-01-01T00:00:09Z"),
		run(2, "core-ci / test (3.12)", "completed", "success", "2026-01-01T00:00:01Z"),
		run(3, "core-ci / test (3.13)", "completed", "failure", "2026-01-01T00:00:01Z"),
	}
	got := conclusions(LatestCheckRuns(entries, corePattern, 0))
	if _, has := got["core-ci / test"]; has || got["core-ci / test (3.13)"] != "failure" {
		t.Fatalf("got %v", got)
	}
	for name, runs := range map[string][]CheckRun{
		"a sibling job": {
			run(1, "core-ci / test", "completed", "skipped", "2026-01-01T00:00:09Z"),
			run(2, "core-ci / lint", "completed", "success", "2026-01-01T00:00:01Z"),
		},
		"a name merely sharing the prefix": {
			run(1, "core-ci / test", "completed", "skipped", "2026-01-01T00:00:09Z"),
			run(2, "core-ci / test-extra", "completed", "success", "2026-01-01T00:00:01Z"),
		},
		"a skipped matrix entry": {
			run(1, "core-ci / test", "completed", "skipped", "2026-01-01T00:00:09Z"),
			run(2, "core-ci / test (3.12)", "completed", "skipped", "2026-01-01T00:00:01Z"),
		},
	} {
		if got := conclusions(LatestCheckRuns(runs, corePattern, 0)); got["core-ci / test"] != Skipped {
			t.Errorf("%s covered the skip: %v", name, got)
		}
	}
}

func TestAnExcludedRunsChecksAreLeftOut(t *testing.T) {
	hygiene.Isolate(t)
	runs := []CheckRun{{ID: 1, Name: "core-ci / test", Status: "completed", Conclusion: "success", DetailsURL: "https://github.com/acme/portal/actions/runs/77/job/1"}}
	if got := LatestCheckRuns(runs, corePattern, 77); len(got) != 0 {
		t.Fatalf("got %v", got)
	}
}

func verification(filters []CheckFilter, fetch func() ([]CheckRun, error), log *[]string) Verification {
	return Verification{
		SHA:      "0123456789abcdef0123456789abcdef01234567",
		Filters:  filters,
		Fetch:    fetch,
		Attempts: 3,
		Interval: time.Second,
		Sleep:    func(time.Duration) {},
		Log:      func(s string) { *log = append(*log, s) },
	}
}

func TestVerificationRefusesAbsentAndSkippedChecksNamingTheRunAllDispatch(t *testing.T) {
	hygiene.Isolate(t)
	var log []string
	calls := 0
	absent := func() ([]CheckRun, error) {
		calls++
		return []CheckRun{run(1, "web-ci / test", "completed", "success", "")}, nil
	}
	err := VerifyProjectCIRan(verification([]CheckFilter{{Label: "core", Pattern: corePattern.String()}}, absent, &log))
	var notRun *NotRunError
	if !errors.As(err, &notRun) || !strings.Contains(err.Error(), "no check run matches") || !strings.Contains(err.Error(), "run_all=true") {
		t.Fatalf("got %v", err)
	}
	if calls != 3 {
		t.Fatalf("absent checks were asked for %d times, want 3", calls)
	}
	skipped := func() ([]CheckRun, error) {
		return []CheckRun{run(1, "core-ci / test", "completed", "skipped", "")}, nil
	}
	err = VerifyProjectCIRan(verification([]CheckFilter{{Label: "core", Pattern: corePattern.String()}}, skipped, &log))
	if !errors.As(err, &notRun) || !strings.Contains(err.Error(), "core: core-ci / test: skipped") || !strings.Contains(err.Error(), "run_all=true") {
		t.Fatalf("got %v", err)
	}
}

func TestAProjectWithoutCIIsReportedAndNotRequired(t *testing.T) {
	hygiene.Isolate(t)
	var log []string
	err := VerifyProjectCIRan(verification([]CheckFilter{{Label: "docs"}}, func() ([]CheckRun, error) {
		t.Fatal("nothing to verify was fetched")
		return nil, nil
	}, &log))
	if err != nil || len(log) != 1 || !strings.Contains(log[0], "docs has no CI workflow of its own") {
		t.Fatalf("err %v, log %v", err, log)
	}
}

func TestAPassingProjectIsVerified(t *testing.T) {
	hygiene.Isolate(t)
	var log []string
	err := VerifyProjectCIRan(verification([]CheckFilter{{Label: "core", Pattern: corePattern.String()}}, func() ([]CheckRun, error) {
		return []CheckRun{run(1, "core-ci / test", "completed", "success", ""), run(2, "core-ci / lint", "completed", "success", "")}, nil
	}, &log))
	if err != nil || !strings.Contains(log[len(log)-1], "core (2 check runs)") {
		t.Fatalf("err %v, log %v", err, log)
	}
}
