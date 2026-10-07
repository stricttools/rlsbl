// Package ci observes GitHub Actions for a release: whether the releasing
// project's own CI ran and passed on a commit (the question the release asks
// before it tags, and the publish workflow's wait-for-ci job asks again
// before anything is published), the verdict of every CI run on a pushed
// candidate (green, red, timed out, or no CI configured) with a classified
// retry of a failed run, the CI router's run_all dispatch, the publish
// workflows a published Release must start and whether they did, and the
// watch command's view of a commit.
package ci

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The check-run conclusions the verdicts turn on.
const (
	// Passing is the only conclusion proving a project's CI passed.
	Passing = "success"
	// Skipped is the conclusion GitHub records for a job its condition left
	// out: the absence of a verdict, never a verdict.
	Skipped = "skipped"
)

// matrixLegSuffix separates a matrix job's name from its leg's parameters:
// "cli-ci / test" runs as "cli-ci / test (3.12)".
const matrixLegSuffix = " ("

// CheckRun is one check run on a commit, or one job of a run, which Actions
// names alike.
type CheckRun struct {
	ID         int64
	Name       string
	Status     string
	Conclusion string
	StartedAt  string
	// DetailsURL carries the run id, as /actions/runs/<id>/.
	DetailsURL string
}

// FromJobs are the check runs a run's jobs produced.
func FromJobs(jobs []github.Job) []CheckRun {
	out := make([]CheckRun, 0, len(jobs))
	for _, j := range jobs {
		out = append(out, CheckRun{ID: j.ID, Name: j.Name, Status: j.Status, Conclusion: j.Conclusion, StartedAt: j.StartedAt, DetailsURL: j.HTMLURL})
	}
	return out
}

// CheckFilter is one project's check-run name pattern for a commit. Pattern
// is empty when the project has no CI workflow of its own: a fact reported,
// not a requirement enforced.
type CheckFilter struct {
	Label   string
	Pattern string
}

// ReleasableFilters are the check filters a release of the releasable must
// satisfy: in a standalone repository, the pattern of the CI jobs its
// targets' scaffolded CI runs; in a workspace, one per member of the
// releasable (one tag publishes them all), each the pattern of its inlined
// CI jobs in the router, or the root releasable's declared
// publish_ci_check_pattern for the root member, whose own CI is a workflow
// of its own, as the publish router reads it.
func ReleasableFilters(w *workspace.Workspace, releasable string) ([]CheckFilter, error) {
	r, ok := w.Declarations.Releasable(releasable)
	if !ok {
		return nil, fmt.Errorf("no releasable %q is declared in %s", releasable, declarations.ReleasablesFile)
	}
	if !w.IsWorkspace() {
		ts, err := targets.MemberTargets(w.Root, w.RootMember())
		if err != nil {
			return nil, err
		}
		var names []string
		for _, t := range ts {
			names = append(names, t.Name)
		}
		pattern, err := workflows.CheckPatternForTargets(names)
		if err != nil {
			return nil, fmt.Errorf("the release's CI check of %q: %w", releasable, err)
		}
		return []CheckFilter{{Label: releasable, Pattern: pattern}}, nil
	}
	members := w.MembersOf(releasable)
	sort.Slice(members, func(i, j int) bool { return members[i].Name < members[j].Name })
	var out []CheckFilter
	for _, m := range members {
		if m.IsRoot() && r.PublishCICheckPattern != "" {
			out = append(out, CheckFilter{Label: m.Name, Pattern: r.PublishCICheckPattern})
			continue
		}
		pattern, _, err := workflows.CheckPattern(m.Name, w.MemberDir(m))
		if err != nil {
			return nil, err
		}
		out = append(out, CheckFilter{Label: m.Name, Pattern: pattern})
	}
	return out, nil
}

// hasVerdict reports whether a check run concluded something other than
// skipped: a run still going has no conclusion, and a skip is none.
func hasVerdict(r CheckRun) bool {
	return r.Status == "completed" && r.Conclusion != Skipped
}

// newer reports whether a was started after b (started_at, then id).
func newer(a, b CheckRun) bool {
	if a.StartedAt != b.StartedAt {
		return a.StartedAt > b.StartedAt
	}
	return a.ID > b.ID
}

// LatestCheckRuns are the check runs matching pattern, minus those of the
// run excludeRunID (0 for none), collapsed to one per job: the newest of
// each name, except that a skipped newest gives way to the newest completed
// one of that name that is not skipped, in whichever run (a run_all
// dispatch's verdict beside a push run's skip, whatever order GitHub
// recorded them in). Then a skip is dropped when a completed, unskipped
// matrix leg of the same job exists ("<name> (...)"), since GitHub records a
// skipped matrix job under its bare name. Nothing else covers a skip. The
// publish workflow's wait-for-ci job collapses alike. Sorted by name.
func LatestCheckRuns(runs []CheckRun, pattern *regexp.Regexp, excludeRunID int64) []CheckRun {
	exclude := ""
	if excludeRunID != 0 {
		exclude = fmt.Sprintf("/actions/runs/%d/", excludeRunID)
	}
	grouped := map[string][]CheckRun{}
	for _, r := range runs {
		if !pattern.MatchString(r.Name) {
			continue
		}
		if exclude != "" && strings.Contains(r.DetailsURL, exclude) {
			continue
		}
		grouped[r.Name] = append(grouped[r.Name], r)
	}
	byName := map[string]CheckRun{}
	for name, group := range grouped {
		chosen := group[0]
		for _, r := range group[1:] {
			if newer(r, chosen) {
				chosen = r
			}
		}
		if chosen.Conclusion == Skipped {
			found := false
			var verdict CheckRun
			for _, r := range group {
				if hasVerdict(r) && (!found || newer(r, verdict)) {
					verdict, found = r, true
				}
			}
			if found {
				chosen = verdict
			}
		}
		byName[name] = chosen
	}
	names := make([]string, 0, len(byName))
	for n := range byName {
		names = append(names, n)
	}
	sort.Strings(names)
	var kept []CheckRun
	for _, name := range names {
		r := byName[name]
		if r.Conclusion == Skipped {
			covered := false
			for other, o := range byName {
				if strings.HasPrefix(other, name+matrixLegSuffix) && hasVerdict(o) {
					covered = true
					break
				}
			}
			if covered {
				continue
			}
		}
		kept = append(kept, r)
	}
	return kept
}

// Failure is one check run that did not pass, and how it ended.
type Failure struct {
	Name       string
	Conclusion string
}

// FailingCheckRuns are the runs that did not conclude success. A run still
// going counts: the release asks only once every workflow run concluded, so
// a check still in flight then is not a pass either.
func FailingCheckRuns(runs []CheckRun) []Failure {
	var out []Failure
	for _, r := range runs {
		switch {
		case r.Status != "completed":
			status := r.Status
			if status == "" {
				status = "pending"
			}
			out = append(out, Failure{Name: r.Name, Conclusion: status})
		case r.Conclusion != Passing:
			c := r.Conclusion
			if c == "" {
				c = "none"
			}
			out = append(out, Failure{Name: r.Name, Conclusion: c})
		}
	}
	return out
}

// NotRunError is a releasing project whose own CI did not run and pass on
// the candidate: not a red CI (the code may be fine), but a candidate that
// proved nothing about the project.
type NotRunError struct {
	Message string
}

func (e *NotRunError) Error() string { return e.Message }

// RunAllFix is the router-level way out for a member whose jobs the path
// filters left out of the candidate's run, named wherever such a refusal is
// reported, so nobody invents a commit to widen the candidate's push.
const RunAllFix = "When the candidate's commits honestly touch few members (a fix-forward that touches only what it fixes), do not invent a commit to widen the push. Run the same commit with the router's path filters bypassed, then resume:\n" +
	"  gh workflow run " + workflows.RouterFile + " --ref <branch> -f " + workflows.RunAllInput + "=true\n" +
	"  gh run watch <run-id>\n" +
	"  " + runstate.ResumeInvocation + "\n" +
	"The dispatched run executes every member's CI jobs on this commit; nothing is waived, and a job failing there still stops the release. Its verdicts supersede the skipped ones."

// Verification is the question whether every project of Filters ran and
// passed its own CI on SHA.
type Verification struct {
	SHA     string
	Filters []CheckFilter
	// Fetch reads the check runs the verdicts come from: the jobs of the
	// runs the release watched.
	Fetch func() ([]CheckRun, error)
	// Attempts and Interval bound how long absent check runs are asked for
	// again, since they can lag the run's own conclusion on GitHub's read
	// replicas.
	Attempts int
	Interval time.Duration
	Sleep    func(time.Duration)
	// Log reports progress and the projects with no CI of their own.
	Log func(string)
	// ExcludeRunID leaves out the check runs of one run (0 for none).
	ExcludeRunID int64
}

// The check discovery bounds the release uses.
const (
	CheckDiscoveryAttempts = 6
	CheckDiscoveryInterval = 5 * time.Second
)

// VerifyProjectCIRan refuses, with a *NotRunError, unless every project of
// the filters ran its own CI on the commit and every one of its collapsed
// check runs concluded success. A project with no CI of its own is
// reported through Log and not required.
func VerifyProjectCIRan(v Verification) error {
	if v.Fetch == nil || v.Log == nil || v.Sleep == nil || v.Attempts < 1 {
		return fmt.Errorf("the CI verification of %s needs a fetch, a log, a sleep, and at least one attempt", v.SHA)
	}
	type enforced struct {
		label   string
		pattern string
		re      *regexp.Regexp
	}
	var checks []enforced
	for _, f := range v.Filters {
		if f.Pattern == "" {
			v.Log(fmt.Sprintf("%s has no CI workflow of its own (.github/workflows/ci*.yml), so nothing in CI runs for it and there are no check runs to verify", f.Label))
			continue
		}
		re, err := regexp.Compile(f.Pattern)
		if err != nil {
			return fmt.Errorf("the check-run pattern %q of %s is not a regular expression: %w", f.Pattern, f.Label, err)
		}
		checks = append(checks, enforced{f.Label, f.Pattern, re})
	}
	if len(checks) == 0 {
		return nil
	}
	var all []CheckRun
	matched := map[string][]CheckRun{}
	for attempt := 1; ; attempt++ {
		runs, err := v.Fetch()
		if err != nil {
			return err
		}
		all = runs
		complete := true
		for _, c := range checks {
			matched[c.label] = LatestCheckRuns(runs, c.re, v.ExcludeRunID)
			if len(matched[c.label]) == 0 {
				complete = false
			}
		}
		if complete || attempt >= v.Attempts {
			break
		}
		v.Log(fmt.Sprintf("the CI check runs of %s are not visible yet (attempt %d of %d); asking again in %s", short(v.SHA), attempt, v.Attempts, v.Interval))
		v.Sleep(v.Interval)
	}
	var missing []enforced
	for _, c := range checks {
		if len(matched[c.label]) == 0 {
			missing = append(missing, c)
		}
	}
	if len(missing) > 0 {
		names := map[string]bool{}
		for _, r := range all {
			names[r.Name] = true
		}
		seen := make([]string, 0, len(names))
		for n := range names {
			seen = append(seen, n)
		}
		sort.Strings(seen)
		present := "none"
		if len(seen) > 0 {
			present = strings.Join(seen, ", ")
		}
		var lines []string
		lines = append(lines, fmt.Sprintf("the release candidate %s produced no CI check run for:", v.SHA))
		for _, c := range missing {
			lines = append(lines, fmt.Sprintf("  %s: no check run matches %s", c.label, c.pattern))
		}
		lines = append(lines,
			"Check runs on the candidate: "+present,
			"",
			"CI ran on the candidate but not for this project, so nothing was proven about it, and the publish workflow's wait-for-ci job would refuse the tag; the release stops here rather than tag a version that can never publish.",
			"",
			"The usual cause is the CI router's path filters: the candidate's commits touch no path the project's filter covers. The filter is derived from the workspace (the project's directory, its dependencies' directories, the root manifests and lockfiles) and is written out in the filters block of "+workflows.RouterPath+". The candidate must hold a commit that filter matches.",
			"",
			RunAllFix)
		return &NotRunError{Message: strings.Join(lines, "\n")}
	}
	var failures []string
	skipped := false
	for _, c := range checks {
		for _, f := range FailingCheckRuns(matched[c.label]) {
			failures = append(failures, fmt.Sprintf("  %s: %s: %s", c.label, f.Name, f.Conclusion))
			if f.Conclusion == Skipped {
				skipped = true
			}
		}
	}
	if len(failures) > 0 {
		lines := []string{fmt.Sprintf("the release candidate %s did not pass the project's own CI check runs:", v.SHA)}
		lines = append(lines, failures...)
		lines = append(lines, "", "Only '"+Passing+"' passes, here and in the publish workflow's wait-for-ci job: a skipped or cancelled check proves nothing about the commit.")
		if skipped {
			lines = append(lines, "",
				"A skipped job means CI ran on the candidate and left this project's part out, almost always because the CI router's path filters found no change under the project's paths. The candidate must hold a commit that filter matches; the filters block of "+workflows.RouterPath+" states it in full. Releasing the project with the commits that touch it, or adding a commit under its paths and resuming, makes its jobs run.",
				"",
				RunAllFix)
		}
		return &NotRunError{Message: strings.Join(lines, "\n")}
	}
	var verified []string
	for _, c := range checks {
		verified = append(verified, fmt.Sprintf("%s (%d check runs)", c.label, len(matched[c.label])))
	}
	v.Log(fmt.Sprintf("the project's own CI ran and passed on %s: %s", short(v.SHA), strings.Join(verified, ", ")))
	return nil
}

// short is a commit id's first 12 characters.
func short(sha string) string {
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}
