package github

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// WorkflowRun is one GitHub Actions workflow run.
type WorkflowRun struct {
	ID           int64  `json:"databaseId"`
	Name         string `json:"name"`
	WorkflowName string `json:"workflowName"`
	Status       string `json:"status"`
	Conclusion   string `json:"conclusion"`
	HeadBranch   string `json:"headBranch"`
	HeadSHA      string `json:"headSha"`
	Event        string `json:"event"`
}

// runFields is the --json field list every run read asks for.
const runFields = "databaseId,name,workflowName,status,conclusion,headBranch,headSha,event"

// RunQuery selects workflow runs. Every set field narrows the listing, and
// Limit is required: gh's own default would silently cap the answer.
type RunQuery struct {
	// Commit is the full commit id the runs ran on.
	Commit string
	// Workflow is the workflow's file name.
	Workflow string
	// Event is the triggering event, such as push or workflow_dispatch.
	Event string
	Limit int
}

// Runs lists the repository's workflow runs the query selects, newest
// first, as gh returns them.
func (c Client) Runs(repo Repository, q RunQuery) ([]WorkflowRun, error) {
	if q.Limit <= 0 {
		return nil, errors.New("a workflow run listing needs a positive limit")
	}
	args := []string{"run", "list", "--repo", repo.String()}
	if q.Commit != "" {
		args = append(args, "--commit", q.Commit)
	}
	if q.Workflow != "" {
		args = append(args, "--workflow", q.Workflow)
	}
	if q.Event != "" {
		args = append(args, "--event", q.Event)
	}
	args = append(args, "--limit", strconv.Itoa(q.Limit), "--json", runFields)
	out, err := c.output(readTimeout, args...)
	if err != nil {
		return nil, err
	}
	var runs []WorkflowRun
	if err := json.Unmarshal([]byte(out), &runs); err != nil {
		return nil, fmt.Errorf("`gh %s` printed something that is not a run list: %w", strings.Join(args, " "), err)
	}
	if len(runs) >= q.Limit {
		return nil, fmt.Errorf("the workflow run listing of %s came back at its %d-run limit, so it may be truncated; narrow the query or raise the limit", repo, q.Limit)
	}
	return runs, nil
}

// Run reads one workflow run.
func (c Client) Run(repo Repository, id int64) (WorkflowRun, error) {
	args := []string{"run", "view", strconv.FormatInt(id, 10), "--repo", repo.String(), "--json", runFields}
	out, err := c.output(readTimeout, args...)
	if err != nil {
		return WorkflowRun{}, err
	}
	var run WorkflowRun
	if err := json.Unmarshal([]byte(out), &run); err != nil {
		return WorkflowRun{}, fmt.Errorf("`gh %s` printed something that is not a run: %w", strings.Join(args, " "), err)
	}
	return run, nil
}

// WatchRun blocks until the run concludes, through `gh run watch
// --exit-status`, and reports whether it passed. A run that concluded
// without passing is (false, nil); a watch gh could not carry out (a
// timeout included) is an error, and the caller reads the run's state to
// learn what happened.
func (c Client) WatchRun(repo Repository, id int64, timeout time.Duration) (bool, error) {
	args := []string{"run", "watch", strconv.FormatInt(id, 10), "--repo", repo.String(), "--exit-status"}
	done, err := c.r.Run(ghArgv(args), strictcli.Check(false), strictcli.Timeout(timeout))
	if err != nil {
		return false, fmt.Errorf("gh %s: %w", strings.Join(args, " "), err)
	}
	return done.ExitCode() == 0, nil
}

// RerunFailedJobs reruns the failed jobs of a run.
func (c Client) RerunFailedJobs(repo Repository, id int64, extra ...strictcli.EffectOption) error {
	return c.write(nil, extra, "run", "rerun", strconv.FormatInt(id, 10), "--repo", repo.String(), "--failed")
}

// DispatchWorkflow starts the workflow file at ref through its
// workflow_dispatch trigger, with the inputs given, in sorted input order.
func (c Client) DispatchWorkflow(repo Repository, workflow, ref string, inputs map[string]string, extra ...strictcli.EffectOption) error {
	if workflow == "" || ref == "" {
		return errors.New("a workflow dispatch needs a workflow file and a ref")
	}
	args := []string{"workflow", "run", workflow, "--repo", repo.String(), "--ref", ref}
	keys := make([]string, 0, len(inputs))
	for k := range inputs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		args = append(args, "-f", k+"="+inputs[k])
	}
	return c.write(nil, extra, args...)
}

// JobLog is the log of one Actions job.
func (c Client) JobLog(repo Repository, jobID int64) (string, error) {
	return c.output(readTimeout, "api", "--method", "GET", "--allow-escape-sequences", repo.apiPath("actions/jobs/"+strconv.FormatInt(jobID, 10)+"/logs"))
}

// RunState is a run's status and conclusion as the REST API reports them.
type RunState struct {
	Status     string `json:"status"`
	Conclusion string `json:"conclusion"`
}

// ReadRunState reads a run's status and conclusion from the REST API. An
// answer without a status is an error.
func (c Client) ReadRunState(repo Repository, id int64) (RunState, error) {
	out, err := c.APIGet(repo.apiPath("actions/runs/"+strconv.FormatInt(id, 10)), false, "")
	if err != nil {
		return RunState{}, err
	}
	var st RunState
	if err := json.Unmarshal([]byte(out), &st); err != nil || st.Status == "" {
		return RunState{}, fmt.Errorf("GitHub's answer about run %d of %s carries no status", id, repo)
	}
	return st, nil
}
