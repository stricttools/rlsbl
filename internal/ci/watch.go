package ci

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/workflows"
)

// FailureClass is what a failed run's log says about whether running it
// again could change the answer.
type FailureClass string

// The classes, in the order they are tried.
const (
	// Infrastructure: the run died below the code under test (no runner,
	// actions that did not resolve, nothing executed); it established
	// nothing, so its failed jobs are run again once.
	Infrastructure FailureClass = "infrastructure"
	// Deterministic: the failure recurs on every run (tests, compilation,
	// configuration, workflow syntax, a missing secret); never run again.
	Deterministic FailureClass = "deterministic"
	// Transient: a flake (rate limits, the network, 5xx, a lost runner);
	// run again once.
	Transient FailureClass = "transient"
	// Unrecognized: no signature matched; run again once.
	Unrecognized FailureClass = "unrecognized"
)

func signatures(patterns ...string) []*regexp.Regexp {
	out := make([]*regexp.Regexp, len(patterns))
	for i, p := range patterns {
		out[i] = regexp.MustCompile(`(?im)` + p)
	}
	return out
}

// The signatures. Infrastructure is tried first: the log of a run that never
// executed still holds job names, echoed commands, and workflow text, which
// the deterministic signatures match by accident. Deterministic outranks
// transient, so a hard error beside incidental network noise is read as
// the hard error.
var (
	infrastructureSignatures = signatures(
		`was not acquired by\s+(a\s+|the\s+)?runner`,
		`waiting for a runner to pick up this job.*(expired|timed out)`,
		`failed to resolve action download info`,
		`unable to (resolve|download) action`,
	)
	deterministicSignatures = signatures(
		`={3,}\s*\d+\s+failed`,
		`short test summary info`,
		`^FAILED\s+\S`,
		`--- FAIL:`,
		`^FAIL\b`,
		`\[build failed\]`,
		`Tests:.*\bfailed`,
		`npm ERR!\s+Test failed`,
		`compilation (failed|error)`,
		`\bbuild failed\b`,
		`error\[E\d+\]`,
		`\bSyntaxError\b`,
		`cannot find (module|package)`,
		`undefined reference to`,
		`undefined:\s`,
		`strictcli`,
		`registration error`,
		`\bConfigError\b`,
		`\bValidationError\b`,
		`invalid configuration`,
		`goreleaser\b.*\b(error|invalid)`,
		`only configuration files are allowed`,
		`Invalid workflow file`,
		`yaml:\s*line\s*\d+`,
		`workflow.*syntax error`,
		`Input required and not supplied`,
		`could not read (Username|Password)`,
		`Permission denied`,
		`denied: permission_denied`,
		`authentication failed`,
		`remote: Permission to .* denied`,
	)
	transientSignatures = signatures(
		`rate limit`,
		`\b429\b`,
		`Too Many Requests`,
		`i/o timeout`,
		`connection (reset|refused|timed out)`,
		`network is unreachable`,
		`temporary failure in name resolution`,
		`TLS handshake timeout`,
		`\bdial tcp\b`,
		`HTTP 5\d\d`,
		`5\d\d\s+(Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out)`,
		`internal server error`,
		`The runner has received a shutdown signal`,
		`lost communication with the server`,
		`The operation was canceled`,
		`received request to (deprovision|cancel)`,
	)
)

// ClassifyFailure classifies a failed run's failure log. An empty log is
// Infrastructure: a run whose failing jobs printed nothing, or that has no
// failing job, died before it executed anything.
func ClassifyFailure(log string) FailureClass {
	if strings.TrimSpace(log) == "" {
		return Infrastructure
	}
	for _, group := range []struct {
		class FailureClass
		sigs  []*regexp.Regexp
	}{{Infrastructure, infrastructureSignatures}, {Deterministic, deterministicSignatures}, {Transient, transientSignatures}} {
		for _, s := range group.sigs {
			if s.MatchString(log) {
				return group.class
			}
		}
	}
	return Unrecognized
}

// The failure log's bounds.
const (
	logTailLines     = 100
	logErrorContext  = 5
	logMaxFailedJobs = 5
)

// FailureRegion is the part of one job's log worth classifying: the
// ##[error] lines Actions marks a failure with, each with a few lines before
// it, or the log's tail when it has none.
func FailureRegion(lines []string) []string {
	var marks []int
	for i, l := range lines {
		if strings.Contains(l, "##[error]") {
			marks = append(marks, i)
		}
	}
	if len(marks) == 0 {
		if len(lines) > logTailLines {
			return lines[len(lines)-logTailLines:]
		}
		return lines
	}
	keep := map[int]bool{}
	for _, m := range marks {
		from := m - logErrorContext
		if from < 0 {
			from = 0
		}
		for i := from; i <= m; i++ {
			keep[i] = true
		}
	}
	idx := make([]int, 0, len(keep))
	for i := range keep {
		idx = append(idx, i)
	}
	sort.Ints(idx)
	out := make([]string, 0, len(idx))
	for _, i := range idx {
		out = append(out, lines[i])
	}
	if len(out) > logTailLines {
		out = out[len(out)-logTailLines:]
	}
	return out
}

// Watcher watches workflow runs of one repository.
type Watcher struct {
	GH   github.Client
	Repo github.Repository
	// Log reports progress on the command's diagnostic stream.
	Log   func(string)
	Sleep func(time.Duration)
	Now   func() time.Time
}

// FailureLog is the failure log of a run: each failing job's failure region
// under a header naming it, at most logMaxFailedJobs of them, the rest named
// only; empty when no failing job printed anything.
func (w Watcher) FailureLog(runID int64) (string, error) {
	jobs, err := w.GH.RunAttemptJobs(w.Repo, runID)
	if err != nil {
		return "", err
	}
	var failed []github.Job
	for _, j := range jobs {
		if j.Conclusion != "" && j.Conclusion != Passing && j.Conclusion != Skipped {
			failed = append(failed, j)
		}
	}
	var sections []string
	for i, j := range failed {
		if i >= logMaxFailedJobs {
			break
		}
		raw, err := w.GH.JobLog(w.Repo, j.ID)
		if err != nil {
			return "", err
		}
		var region []string
		for _, l := range FailureRegion(strings.Split(raw, "\n")) {
			if strings.TrimSpace(l) != "" {
				region = append(region, l)
			}
		}
		if len(region) == 0 {
			continue
		}
		sections = append(sections, fmt.Sprintf("--- %s (%s) ---\n%s", j.Name, j.Conclusion, strings.Join(region, "\n")))
	}
	if len(sections) == 0 {
		return "", nil
	}
	if len(failed) > logMaxFailedJobs {
		var rest []string
		for _, j := range failed[logMaxFailedJobs:] {
			rest = append(rest, j.Name)
		}
		sections = append(sections, fmt.Sprintf("--- and %d more failing jobs: %s ---", len(rest), strings.Join(rest, ", ")))
	}
	return strings.Join(sections, "\n"), nil
}

// Outcome is what watching a run established.
type Outcome string

// The outcomes. Only Failed is a verdict about the code.
const (
	Passed     Outcome = "passed"
	Failed     Outcome = "failed"
	TimedOut   Outcome = "timed-out"
	Unresolved Outcome = "unresolved"
)

// The bounds of reading a run's own state after a watch ended without a
// pass, and of watching a run again that is still going.
const (
	stateProbeAttempts   = 3
	stateProbeInterval   = 2 * time.Second
	watchRecheckAttempts = 6
	watchRecheckInterval = 15 * time.Second
)

// runState reads the run's status and conclusion, asking a few times; false
// when it could not be read.
func (w Watcher) runState(id int64) (github.RunState, bool) {
	for attempt := 1; attempt <= stateProbeAttempts; attempt++ {
		st, err := w.GH.ReadRunState(w.Repo, id)
		if err == nil {
			return st, true
		}
		if attempt < stateProbeAttempts {
			w.Sleep(stateProbeInterval)
		}
	}
	return github.RunState{}, false
}

// ToConclusion watches the run until it concludes or timeout passes. A watch
// gh ended without a pass is a verdict only when the run's own state says
// it completed: gh also ends a watch it could not carry through (an API
// error, a dropped connection), and then the run is watched again while it
// is still going, within the same budget. A state that cannot be read
// establishes nothing (Unresolved).
func (w Watcher) ToConclusion(id int64, name, label string, timeout time.Duration) Outcome {
	deadline := w.Now().Add(timeout)
	for attempt := 0; attempt <= watchRecheckAttempts; attempt++ {
		remaining := deadline.Sub(w.Now())
		if remaining <= 0 {
			return TimedOut
		}
		passed, err := w.GH.WatchRun(w.Repo, id, remaining)
		if err == nil && passed {
			return Passed
		}
		if !w.Now().Before(deadline) {
			return TimedOut
		}
		st, ok := w.runState(id)
		if !ok {
			w.Log(fmt.Sprintf("%s: [%s] the watch ended without a pass and the run's state could not be read; nothing was established about it", label, name))
			return Unresolved
		}
		if st.Status == "completed" {
			if st.Conclusion == Passing {
				return Passed
			}
			if st.Conclusion != "" {
				return Failed
			}
		}
		w.Log(fmt.Sprintf("%s: [%s] the watch ended, and the run is %s, not concluded; watching it again", label, name, st.Status))
		if attempt < watchRecheckAttempts {
			w.Sleep(watchRecheckInterval)
		}
	}
	return Unresolved
}

// RunResult is the result of watching one run.
type RunResult struct {
	Name   string `json:"name"`
	RunID  int64  `json:"run_id"`
	Passed bool   `json:"passed"`
	// Unresolved is a run that never concluded where rlsbl could see it (a
	// watch that ran out of time, or a state that could not be read): not a
	// failure verdict.
	Unresolved bool `json:"unresolved"`
}

// retryTimeout bounds the watch of a run run again.
const retryTimeout = time.Hour

// Watch watches one run and, when it failed for a reason a second run could
// change, runs it again once (at most once per workflow name across a
// watch, through retried) and watches the new attempt. A deterministic
// failure is never run again, and the way to run it by hand is named, since
// a run killed below the code can still read like a code failure.
func (w Watcher) Watch(run github.WorkflowRun, label string, timeout time.Duration, retried map[string]bool) RunResult {
	name := run.Name
	if name == "" {
		name = fmt.Sprintf("run %d", run.ID)
	}
	res := RunResult{Name: name, RunID: run.ID}
	switch w.ToConclusion(run.ID, name, label, timeout) {
	case Passed:
		w.Log(fmt.Sprintf("%s: [%s] passed", label, name))
		res.Passed = true
		return res
	case TimedOut:
		w.Log(fmt.Sprintf("%s: [%s] did not conclude within %s; check it with `gh run view %d --repo %s` and resume", label, name, timeout, run.ID, w.Repo))
		res.Unresolved = true
		return res
	case Unresolved:
		w.Log(fmt.Sprintf("%s: [%s] never concluded where rlsbl could see it; check it with `gh run view %d --repo %s` and resume", label, name, run.ID, w.Repo))
		res.Unresolved = true
		return res
	}
	w.Log(fmt.Sprintf("%s: [%s] FAILED: https://github.com/%s/actions/runs/%d", label, name, w.Repo, run.ID))
	class := Unrecognized
	failureLog, err := w.FailureLog(run.ID)
	if err != nil {
		w.Log(fmt.Sprintf("%s: [%s] the failure log could not be read (%v), so the failure is unclassified and the run is run again once", label, name, err))
	} else {
		if strings.TrimSpace(failureLog) != "" {
			w.Log(fmt.Sprintf("%s: [%s] the failure log:\n%s", label, name, failureLog))
		}
		class = ClassifyFailure(failureLog)
	}
	if class == Deterministic {
		w.Log(fmt.Sprintf("%s: [%s] a deterministic failure, which a second run would repeat, so it is not run again. If the run died below the code (jobs that never got a runner, actions that did not resolve, a run cancelled while queued), run its failed jobs again and resume:\n  gh run rerun %d --failed --repo %s\n  rlsbl release resume", label, name, run.ID, w.Repo))
		return res
	}
	if retried[name] {
		return res
	}
	retried[name] = true
	failedOnly := class == Infrastructure
	if failedOnly {
		w.Log(fmt.Sprintf("%s: [%s] an infrastructure failure: the run died below the code and established nothing; running its failed jobs again once", label, name))
		err = w.GH.RerunFailedJobs(w.Repo, run.ID)
	} else {
		w.Log(fmt.Sprintf("%s: [%s] a %s failure; running the run again once", label, name, class))
		err = w.GH.RerunRun(w.Repo, run.ID)
	}
	if err != nil {
		w.Log(fmt.Sprintf("%s: [%s] the run could not be started again: %v", label, name, err))
		return res
	}
	switch w.ToConclusion(run.ID, name, label, retryTimeout) {
	case Passed:
		w.Log(fmt.Sprintf("%s: [%s] passed when run again", label, name))
		res.Passed = true
	case Failed:
		w.Log(fmt.Sprintf("%s: [%s] failed again: https://github.com/%s/actions/runs/%d", label, name, w.Repo, run.ID))
		if again, err := w.FailureLog(run.ID); err != nil {
			w.Log(fmt.Sprintf("%s: [%s] the failure log could not be read: %v", label, name, err))
		} else if strings.TrimSpace(again) != "" {
			w.Log(fmt.Sprintf("%s: [%s] the failure log:\n%s", label, name, again))
		}
	default:
		w.Log(fmt.Sprintf("%s: [%s] the run started again never concluded where rlsbl could see it; check it with `gh run view %d --repo %s` and resume", label, name, run.ID, w.Repo))
		res.Unresolved = true
	}
	return res
}

// Verdict is the release's reading of every CI run on a candidate.
type Verdict string

// The verdicts.
const (
	Green Verdict = "green"
	Red   Verdict = "red"
	// Timeout: runs never concluded within the budget, or their state could
	// not be read. Not red: the fix is to check them and resume, never to
	// change code that may be fine.
	Timeout Verdict = "timeout"
	// NotConfigured: the repository declares no push-triggered workflow, so
	// no run will ever appear.
	NotConfigured Verdict = "no-ci"
)

// Aggregate is the verdict of watched runs: green when all passed, red when
// any failed (a known failure outranks an open question), timeout otherwise.
func Aggregate(results []RunResult) Verdict {
	all := true
	for _, r := range results {
		if !r.Passed {
			all = false
			if !r.Unresolved {
				return Red
			}
		}
	}
	if all {
		return Green
	}
	return Timeout
}

// PushTriggeredWorkflows are the workflow files under root's
// .github/workflows that trigger on push, by file name. A workflow that is
// not YAML is an error naming it: an unreadable file is no evidence either
// way.
func PushTriggeredWorkflows(root string) ([]string, error) {
	dir := filepath.Join(root, filepath.FromSlash(workflows.Dir))
	entries, err := os.ReadDir(dir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var found []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !(strings.HasSuffix(name, ".yml") || strings.HasSuffix(name, ".yaml")) {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return nil, err
		}
		var doc map[string]any
		if err := yaml.Unmarshal(data, &doc); err != nil {
			return nil, fmt.Errorf("%s/%s is not a workflow rlsbl can read: %w", workflows.Dir, name, err)
		}
		triggers := false
		switch on := doc["on"].(type) {
		case string:
			triggers = on == "push"
		case []any:
			for _, e := range on {
				if e == "push" {
					triggers = true
				}
			}
		case map[string]any:
			_, triggers = on["push"]
		}
		if triggers {
			found = append(found, name)
		}
	}
	sort.Strings(found)
	return found, nil
}

// The CI wait's discovery: how long runs may take to appear after a push,
// how often to look, the share of the budget discovery may take at most,
// and the shortest completion wait worth starting.
const (
	DiscoveryGrace        = 300 * time.Second
	discoveryInterval     = 5 * time.Second
	discoveryBudgetShare  = 2
	minimumCompletionWait = 30 * time.Second
	lateRunPause          = 5 * time.Second
	runListingLimit       = 100
)

// WaitError is a CI wait that reached no verdict: the repository declares
// push-triggered workflows and the candidate produced no run.
type WaitError struct {
	Message string
}

func (e *WaitError) Error() string { return e.Message }

// WaitInputs are the CI wait's question.
type WaitInputs struct {
	// Commit is the candidate's full commit id.
	Commit string
	// Root is the checkout whose .github/workflows says whether CI is
	// configured.
	Root string
	// Timeout is the whole budget, discovery included.
	Timeout time.Duration
	// DiscoveryGrace is how long runs may take to appear; at most half the
	// budget is spent on it.
	DiscoveryGrace time.Duration
	// Filters are the releasing projects whose own CI must have run and
	// passed (ReleasableFilters); every release states them, since leaving
	// them out would tag a commit whose project's jobs were all skipped.
	Filters []CheckFilter
	Label   string
}

// discover polls the commit's runs until one appears or the grace passes.
func (w Watcher) discover(commit string, grace time.Duration) ([]github.WorkflowRun, error) {
	deadline := w.Now().Add(grace)
	var lastErr error
	for {
		runs, err := w.GH.Runs(w.Repo, github.RunQuery{Commit: commit, Limit: runListingLimit})
		if err == nil && len(runs) > 0 {
			return runs, nil
		}
		lastErr = err
		if !w.Now().Before(deadline) {
			return nil, lastErr
		}
		w.Sleep(discoveryInterval)
	}
}

// WaitForCIGreen waits until every CI run on the candidate concluded, and
// returns the verdict with each run's result. Its answers:
//
//   - no push-triggered workflow: NotConfigured at once;
//   - push-triggered workflows and no run within the grace: a *WaitError;
//   - runs that conclude: Green or Red (a failure run again once unless it
//     is deterministic);
//   - runs still unresolved when the budget runs out: Timeout;
//   - every run green but a releasing project's own check runs absent or
//     not passed (its jobs skipped by the router's path filters): a
//     *NotRunError, the publish workflow's wait-for-ci question asked
//     before the release tags.
func (w Watcher) WaitForCIGreen(in WaitInputs) (Verdict, []RunResult, error) {
	expected, err := PushTriggeredWorkflows(in.Root)
	if err != nil {
		return "", nil, err
	}
	if len(expected) == 0 {
		return NotConfigured, nil, nil
	}
	label := in.Label
	if label == "" {
		label = "candidate " + short(in.Commit)
	}
	grace := in.DiscoveryGrace
	limit := in.Timeout / discoveryBudgetShare
	if limit < discoveryInterval {
		limit = discoveryInterval
	}
	if grace > limit {
		w.Log(fmt.Sprintf("run discovery is limited to %s (rather than %s): it is spent inside the %s CI budget, and at least half of that stays for the runs to complete", limit, grace, in.Timeout))
		grace = limit
	}
	start := w.Now()
	w.Log(fmt.Sprintf("waiting for CI on the release candidate %s (push-triggered workflows: %s)", short(in.Commit), strings.Join(expected, ", ")))
	runs, err := w.discover(in.Commit, grace)
	if len(runs) == 0 {
		detail := ""
		if err != nil {
			detail = fmt.Sprintf(" (the last listing failed: %v)", err)
		}
		return "", nil, &WaitError{Message: fmt.Sprintf("no CI run appeared for the release candidate %s within %s%s, and this repository declares push-triggered workflows: %s. The candidate is on the remote and nothing was tagged or published. Find out why the push started no run (branch or path filters, disabled workflows, the Actions quota), then run `rlsbl release resume`", in.Commit, grace, detail, strings.Join(expected, ", "))}
	}
	remaining := func() time.Duration { return in.Timeout - w.Now().Sub(start) }
	unresolvedAll := func(rs []github.WorkflowRun) []RunResult {
		var out []RunResult
		for _, r := range rs {
			out = append(out, RunResult{Name: r.Name, RunID: r.ID, Unresolved: true})
		}
		return out
	}
	if remaining() < minimumCompletionWait {
		w.Log(fmt.Sprintf("the %s CI budget is spent (%s left, below %s), so no completion wait is started", in.Timeout, remaining(), minimumCompletionWait))
		return Timeout, unresolvedAll(runs), nil
	}
	w.Log(fmt.Sprintf("found %d CI runs for %s; waiting for them to conclude", len(runs), short(in.Commit)))
	retried := map[string]bool{}
	known := map[int64]bool{}
	var results []RunResult
	for _, r := range runs {
		known[r.ID] = true
		results = append(results, w.Watch(r, label, remaining(), retried))
	}
	// A run started after the first listing (a second workflow, a late
	// matrix) must not escape the wait.
	w.Sleep(lateRunPause)
	again, err := w.GH.Runs(w.Repo, github.RunQuery{Commit: in.Commit, Limit: runListingLimit})
	if err != nil {
		return "", nil, fmt.Errorf("listing the CI runs of %s again for runs started late: %w", short(in.Commit), err)
	}
	var late []github.WorkflowRun
	for _, r := range again {
		if !known[r.ID] {
			late = append(late, r)
		}
	}
	if len(late) > 0 {
		if remaining() < minimumCompletionWait {
			w.Log(fmt.Sprintf("found %d CI runs started late, and the %s CI budget is spent; they stay unresolved", len(late), in.Timeout))
			results = append(results, unresolvedAll(late)...)
		} else {
			w.Log(fmt.Sprintf("found %d CI runs started late; waiting for them", len(late)))
			for _, r := range late {
				results = append(results, w.Watch(r, label, remaining(), retried))
			}
		}
	}
	verdict := Aggregate(results)
	if verdict != Green {
		return verdict, results, nil
	}
	var ids []int64
	seen := map[int64]bool{}
	for _, r := range results {
		if !seen[r.RunID] {
			seen[r.RunID] = true
			ids = append(ids, r.RunID)
		}
	}
	err = VerifyProjectCIRan(Verification{
		SHA:     in.Commit,
		Filters: in.Filters,
		Fetch: func() ([]CheckRun, error) {
			var all []CheckRun
			for _, id := range ids {
				jobs, err := w.GH.RunAttemptJobs(w.Repo, id)
				if err != nil {
					return nil, err
				}
				all = append(all, FromJobs(jobs)...)
			}
			return all, nil
		},
		Attempts: CheckDiscoveryAttempts,
		Interval: CheckDiscoveryInterval,
		Sleep:    w.Sleep,
		Log:      w.Log,
	})
	return verdict, results, err
}

// The run_all dispatch's correlation: how many times to look for the run it
// created, and how often.
const (
	RunAllAttempts = 12
	RunAllInterval = 5 * time.Second
)

// DispatchRunAll dispatches the CI router at branch with run_all=true, so
// every member's CI runs on the candidate commit whatever the path filters
// say, and returns the run it created. A dispatch names a ref, not a commit,
// so the run is found by commit: a run of another commit (something pushed
// to the branch in between) proves nothing about the candidate, and none
// appearing is an error.
func (w Watcher) DispatchRunAll(branch, commit string) (github.WorkflowRun, error) {
	w.Log(fmt.Sprintf("dispatching %s on %s with %s=true, so every member's CI runs on %s", workflows.RouterFile, branch, workflows.RunAllInput, short(commit)))
	if err := w.GH.DispatchWorkflow(w.Repo, workflows.RouterFile, branch, map[string]string{workflows.RunAllInput: "true"}); err != nil {
		return github.WorkflowRun{}, fmt.Errorf("%s could not be dispatched with %s=true on %s: %w. The candidate is on the remote, untagged; nothing was tagged, released, or finalized. A router generated before the %s input existed refuses the input: regenerate it with `rlsbl monorepo sync`, commit it, and resume", workflows.RouterFile, workflows.RunAllInput, branch, err, workflows.RunAllInput)
	}
	for attempt := 1; attempt <= RunAllAttempts; attempt++ {
		runs, err := w.GH.Runs(w.Repo, github.RunQuery{Commit: commit, Workflow: workflows.RouterFile, Event: "workflow_dispatch", Limit: runListingLimit})
		if err == nil {
			for _, r := range runs {
				if r.HeadSHA == commit {
					w.Log(fmt.Sprintf("the dispatched run %d is on %s", r.ID, short(commit)))
					return r, nil
				}
			}
		}
		if attempt < RunAllAttempts {
			w.Sleep(RunAllInterval)
		}
	}
	return github.WorkflowRun{}, fmt.Errorf("%s was dispatched with %s=true on %s, and no dispatched run appeared for the candidate %s within %s. The candidate is on the remote, untagged; nothing was tagged, released, or finalized. A dispatch resolves the ref when it is made, so a commit pushed to %s in between takes the run instead: check `gh run list --workflow %s --repo %s`, make %s point at the candidate again, and resume", workflows.RouterFile, workflows.RunAllInput, branch, commit, time.Duration(RunAllAttempts)*RunAllInterval, branch, workflows.RouterFile, w.Repo, branch)
}

// The watch command's bounds: how long a commit's runs may take to appear,
// and how long one run is watched.
const (
	WatchDiscovery = 120 * time.Second
	WatchTimeout   = time.Hour
)

// WatchCommit watches every CI run on the commit, then the runs that
// started after the first listing, each run again once when its failure
// allows. A commit on which no run appears within WatchDiscovery is an
// error naming the command to run once GitHub has started them.
func (w Watcher) WatchCommit(commit, label string) ([]RunResult, error) {
	runs, err := w.discover(commit, WatchDiscovery)
	if len(runs) == 0 {
		detail := ""
		if err != nil {
			detail = fmt.Sprintf(" (the last listing failed: %v)", err)
		}
		return nil, fmt.Errorf("no CI run appeared for %s within %s%s; GitHub may not have started them yet: run `rlsbl watch %s` again, and when a release's runs never start, `rlsbl release retry` starts its publish workflows", short(commit), WatchDiscovery, detail, commit)
	}
	w.Log(fmt.Sprintf("%s: found %d CI runs, watching them", label, len(runs)))
	retried := map[string]bool{}
	known := map[int64]bool{}
	var results []RunResult
	for _, r := range runs {
		known[r.ID] = true
		results = append(results, w.Watch(r, label, WatchTimeout, retried))
	}
	w.Sleep(lateRunPause)
	again, err := w.GH.Runs(w.Repo, github.RunQuery{Commit: commit, Limit: runListingLimit})
	if err != nil {
		return results, fmt.Errorf("listing the CI runs of %s again for runs started late: %w", short(commit), err)
	}
	for _, r := range again {
		if !known[r.ID] {
			w.Log(fmt.Sprintf("%s: [%s] started late, watching it", label, r.Name))
			results = append(results, w.Watch(r, label, WatchTimeout, retried))
		}
	}
	return results, nil
}

// WatchRunIDs watches the named runs, each run again once when its failure
// allows. A run id GitHub cannot answer about is an error before anything
// is watched.
func (w Watcher) WatchRunIDs(ids []int64, label string) ([]RunResult, error) {
	var runs []github.WorkflowRun
	for _, id := range ids {
		r, err := w.GH.Run(w.Repo, id)
		if err != nil {
			return nil, fmt.Errorf("the run %d of %s could not be read: %w", id, w.Repo, err)
		}
		runs = append(runs, r)
	}
	retried := map[string]bool{}
	var results []RunResult
	for _, r := range runs {
		results = append(results, w.Watch(r, label, WatchTimeout, retried))
	}
	return results, nil
}
