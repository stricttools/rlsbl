package github

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// Job is one job of a workflow run's attempt, as GitHub's REST API reports
// it. Actions names a commit's check runs after the jobs that produced them,
// so a job carries what a check run does.
type Job struct {
	ID         int64  `json:"id"`
	Name       string `json:"name"`
	Status     string `json:"status"`
	Conclusion string `json:"conclusion"`
	StartedAt  string `json:"started_at"`
	HTMLURL    string `json:"html_url"`
	RunID      int64  `json:"run_id"`
	RunAttempt int    `json:"run_attempt"`
}

// decodePages decodes what `gh api --paginate` printed for a paginated
// object endpoint: one JSON object per page, back to back.
func decodePages(out string, each func(page json.RawMessage) error) error {
	dec := json.NewDecoder(strings.NewReader(out))
	for {
		var page json.RawMessage
		err := dec.Decode(&page)
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		if err := each(page); err != nil {
			return err
		}
	}
}

// RunAttemptJobs are the jobs of the run's latest attempt: the run is read
// for its attempt, then that attempt's job list, both keyed by the run's id.
// A rerun keeps the run's id and advances the attempt, and the job list of
// the run as a whole says nothing of which attempt a job belongs to, so it
// is never read.
func (c Client) RunAttemptJobs(repo Repository, runID int64) ([]Job, error) {
	runPath := "actions/runs/" + strconv.FormatInt(runID, 10)
	out, err := c.APIGet(repo.apiPath(runPath), false, "")
	if err != nil {
		return nil, err
	}
	var run struct {
		RunAttempt int `json:"run_attempt"`
	}
	if err := json.Unmarshal([]byte(out), &run); err != nil {
		return nil, fmt.Errorf("GitHub's answer about run %d of %s is not a run: %w", runID, repo, err)
	}
	if run.RunAttempt < 1 {
		return nil, fmt.Errorf("GitHub's answer about run %d of %s names no attempt", runID, repo)
	}
	out, err = c.APIGet(repo.apiPath(runPath+"/attempts/"+strconv.Itoa(run.RunAttempt)+"/jobs?per_page=100"), true, "")
	if err != nil {
		return nil, err
	}
	var jobs []Job
	err = decodePages(out, func(page json.RawMessage) error {
		var p struct {
			Jobs []Job `json:"jobs"`
		}
		if err := json.Unmarshal(page, &p); err != nil {
			return err
		}
		jobs = append(jobs, p.Jobs...)
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("GitHub's job list of run %d of %s is not one rlsbl can read: %w", runID, repo, err)
	}
	return jobs, nil
}

// RerunRun reruns every job of a run, as a new attempt under the same id.
func (c Client) RerunRun(repo Repository, id int64, extra ...strictcli.EffectOption) error {
	return c.write(nil, extra, "run", "rerun", strconv.FormatInt(id, 10), "--repo", repo.String())
}

// ActionsEnabled reports whether GitHub Actions is enabled for the
// repository. An answer without the field is an error.
func (c Client) ActionsEnabled(repo Repository) (bool, error) {
	out, err := c.APIGet(repo.apiPath("actions/permissions"), false, "")
	if err != nil {
		return false, err
	}
	var doc struct {
		Enabled *bool `json:"enabled"`
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil || doc.Enabled == nil {
		return false, fmt.Errorf("GitHub's answer about the Actions permissions of %s says nothing of whether Actions is enabled", repo)
	}
	return *doc.Enabled, nil
}

// notFound reports whether a failed gh api read was GitHub answering 404.
func notFound(res result) bool {
	return strings.Contains(res.stderr+res.stdout, "(HTTP 404)")
}

// WorkflowState is the state GitHub holds for the workflow file ("active",
// "disabled_manually", ...); known is false when GitHub does not know the
// workflow, which it learns when a push first brings the file. Any other
// failure is an error.
func (c Client) WorkflowState(repo Repository, file string) (state string, known bool, err error) {
	args := []string{"api", "--method", "GET", repo.apiPath("actions/workflows/" + url.PathEscape(file)), "--jq", ".state"}
	res, err := c.read(readTimeout, args...)
	if err != nil {
		return "", false, err
	}
	if res.code != 0 {
		if notFound(res) {
			return "", false, nil
		}
		return "", false, failed(args, res)
	}
	state = strings.TrimSpace(res.stdout)
	if state == "" {
		return "", false, fmt.Errorf("GitHub's answer about the workflow %s of %s names no state", file, repo)
	}
	return state, true, nil
}

// APIRun is one workflow run as the REST API lists it.
type APIRun struct {
	ID         int64  `json:"id"`
	HeadBranch string `json:"head_branch"`
	HeadSHA    string `json:"head_sha"`
	Event      string `json:"event"`
	Status     string `json:"status"`
	Conclusion string `json:"conclusion"`
}

// workflowRunsLimit is how many runs one listing of a workflow's runs at a
// commit asks for; a listing reporting more is refused.
const workflowRunsLimit = 100

// WorkflowRunsAt are the runs of the workflow file on the commit. A listing
// whose total exceeds what one page holds is refused rather than read in
// part.
func (c Client) WorkflowRunsAt(repo Repository, file, sha string) ([]APIRun, error) {
	out, err := c.APIGet(repo.apiPath("actions/workflows/"+url.PathEscape(file)+"/runs?head_sha="+url.QueryEscape(sha)+"&per_page="+strconv.Itoa(workflowRunsLimit)), false, "")
	if err != nil {
		return nil, err
	}
	var doc struct {
		TotalCount   *int     `json:"total_count"`
		WorkflowRuns []APIRun `json:"workflow_runs"`
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil || doc.TotalCount == nil {
		return nil, fmt.Errorf("GitHub's run listing of %s at %s in %s is not one rlsbl can read", file, sha, repo)
	}
	if *doc.TotalCount > len(doc.WorkflowRuns) {
		return nil, fmt.Errorf("GitHub reports %d runs of %s at %s in %s and listed %d; rlsbl does not read a listing in part", *doc.TotalCount, file, sha, repo, len(doc.WorkflowRuns))
	}
	return doc.WorkflowRuns, nil
}

// ReleaseAuthor is the login of the account that created the Release for
// tag.
func (c Client) ReleaseAuthor(repo Repository, tag string) (string, error) {
	out, err := c.output(readTimeout, "release", "view", tag, "--repo", repo.String(), "--json", "author", "--jq", ".author.login")
	if err != nil {
		return "", err
	}
	login := strings.TrimSpace(out)
	if login == "" {
		return "", fmt.Errorf("gh named no author for the Release %s of %s", tag, repo)
	}
	return login, nil
}

// ReleasePublishedAt is when the Release for tag was published.
func (c Client) ReleasePublishedAt(repo Repository, tag string) (time.Time, error) {
	out, err := c.output(readTimeout, "release", "view", tag, "--repo", repo.String(), "--json", "publishedAt", "--jq", ".publishedAt")
	if err != nil {
		return time.Time{}, err
	}
	at, err := time.Parse(time.RFC3339, strings.TrimSpace(out))
	if err != nil {
		return time.Time{}, fmt.Errorf("gh named %q as when the Release %s of %s was published: %w", strings.TrimSpace(out), tag, repo, err)
	}
	return at, nil
}

// RunRetentionDays is how many days the repository keeps its workflow runs,
// artifacts, and logs.
func (c Client) RunRetentionDays(repo Repository) (int, error) {
	out, err := c.APIGet(repo.apiPath("actions/permissions/artifact-and-log-retention"), false, "")
	if err != nil {
		return 0, err
	}
	var doc struct {
		Days *int `json:"days"`
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil || doc.Days == nil {
		return 0, fmt.Errorf("GitHub's answer about the run retention of %s names no number of days", repo)
	}
	return *doc.Days, nil
}
