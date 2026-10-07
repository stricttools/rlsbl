package releaseops

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/ci"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/runstate"
)

// tagInput is the workflow_dispatch input the generated publish workflow
// checks out when it is set.
const tagInput = "tag"

// RetryRequest is a `release retry`.
type RetryRequest struct {
	// Dir is the working directory, absolute.
	Dir string
	// Watch watches the dispatched runs until they conclude.
	Watch bool
	// Sleep waits between the listings that find the dispatched runs.
	Sleep func(time.Duration)
}

// The correlation of a dispatch with the run it created: a dispatch returns
// no run id, so the workflow's dispatched runs on the tagged commit are
// listed until one appears that was not there before.
const (
	dispatchedRunAttempts = 24
	dispatchedRunInterval = 5 * time.Second
	runListingLimit       = 100
)

// dispatchable is one workflow of the tagged tree that can be dispatched.
type dispatchable struct {
	file string
	// takesTag is whether it declares the tag input.
	takesTag bool
}

// Retry dispatches the workflows of the releasable's latest release again
// at its tag, for a release whose Release exists while its publish runs
// never started or failed for reasons outside it. What it dispatches is
// the releasable's retry file (.strictmetadata/.release-state/<releasable>/
// retry.toml): ref, which must be the release tag, and the workflow files.
// A missing retry file is written first, naming every workflow of the
// tagged tree that has a workflow_dispatch trigger. Each workflow declaring
// a tag input is passed the tag. Workflows are dispatched one at a time, and
// a dispatch that fails stops the run with the retry file rewritten to hold
// the workflows not yet dispatched, so running retry again never dispatches
// one twice; the file is removed once every workflow was dispatched.
func Retry(ctx *strictcli.Context, req RetryRequest) (err error) {
	if req.Sleep == nil {
		return errors.New("a retry needs a Sleep to wait between the listings that find its dispatched runs")
	}
	e := ctx.Effects()
	s, err := Select(e, req.Dir)
	if err != nil {
		return err
	}
	l, err := lock(ctx, s.Root())
	if err != nil {
		return err
	}
	// The lock is given back before the runs are watched: watching writes
	// no release state, and may take an hour.
	held := true
	defer func() {
		if held {
			unlock(l, &err)
		}
	}()
	latest, found, err := s.latestReleased()
	if err != nil {
		return err
	}
	if !found {
		return fmt.Errorf("the release archives of %s in %s record no release, so there is nothing to retry. `rlsbl release retry` dispatches the workflows of a release that exists; a release that stopped part-way is finished with `"+runstate.ResumeInvocation+"`", s.Releasable.Name, s.Record.Dir())
	}
	a, err := s.releasedArchive(latest)
	if err != nil {
		return err
	}
	tag := s.tagOf(a)
	commit, tagged, err := s.Repo.TagCommit(tag)
	if err != nil {
		return err
	}
	if !tagged {
		return fmt.Errorf("the tag %s of the latest release, %s, is not in this repository, so the tree its workflows run from cannot be read. Fetch the tags (`git fetch origin --tags`) and run the retry again", tag, latest)
	}
	workflows, err := dispatchableWorkflows(s.Repo, commit)
	if err != nil {
		return err
	}
	gh, slug, err := openGitHub(e, s)
	if err != nil {
		return err
	}
	if err := requireRelease(gh, slug, tag); err != nil {
		return err
	}
	retryPath := runstate.RetryPath(s.Releasable.Name)
	retry, haveFile, err := runstate.LoadRetry(s.Root(), s.Releasable.Name)
	if err != nil {
		return fmt.Errorf("%w\n  Correct the file, or remove it and run the retry again, which writes it naming every dispatchable workflow at %s. `rlsbl release retry` dispatches the workflows of a completed release; a release that stopped part-way is finished with `"+runstate.ResumeInvocation+"`", err, tag)
	}
	if !haveFile {
		if len(workflows) == 0 {
			return fmt.Errorf("the tree %s tags (%s) holds no workflow with a workflow_dispatch trigger, so there is nothing to dispatch", tag, commit)
		}
		retry = runstate.Retry{Ref: tag}
		for _, w := range workflows {
			retry.Workflows = append(retry.Workflows, w.file)
		}
		if err := runstate.SaveRetry(e, s.Root(), s.Releasable.Name, retry); err != nil {
			return err
		}
		ctx.Info(fmt.Sprintf("wrote %s: ref %s, workflows %s", retryPath, tag, strings.Join(retry.Workflows, ", ")))
	}
	if retry.Ref != tag && retry.Ref != "refs/tags/"+tag {
		return fmt.Errorf("%s sets ref = %q, and a retry dispatches at the release tag %s: a run dispatched at any other ref carries neither the tag nor the tagged commit, so nothing can confirm it started for this release. Set ref = %q in %s, or remove the file and run the retry again, which writes it with the tag", retryPath, retry.Ref, tag, tag, retryPath)
	}
	byFile := map[string]dispatchable{}
	for _, w := range workflows {
		byFile[w.file] = w
	}
	var unknown []string
	for _, f := range retry.Workflows {
		if _, ok := byFile[f]; !ok {
			unknown = append(unknown, f)
		}
	}
	if len(unknown) > 0 {
		names := "none"
		if len(workflows) > 0 {
			files := make([]string, len(workflows))
			for i, w := range workflows {
				files[i] = w.file
			}
			names = strings.Join(files, ", ")
		}
		return fmt.Errorf("%s names %s, which the tree %s tags holds no dispatchable workflow of; the dispatchable workflows there are %s. Correct the file, or remove it and run the retry again, which writes it from the tagged tree", retryPath, strings.Join(unknown, ", "), tag, names)
	}
	before := map[string]map[int64]bool{}
	if req.Watch && !ctx.DryRun() {
		for _, f := range retry.Workflows {
			if before[f], err = dispatchedRuns(gh, slug, f, commit); err != nil {
				return err
			}
		}
	}
	for i, f := range retry.Workflows {
		inputs := map[string]string{}
		if byFile[f].takesTag {
			inputs[tagInput] = tag
		}
		if err := gh.DispatchWorkflow(slug, f, retry.Ref, inputs); err != nil {
			rest := runstate.Retry{Ref: retry.Ref, Workflows: retry.Workflows[i:]}
			if serr := runstate.SaveRetry(e, s.Root(), s.Releasable.Name, rest); serr != nil {
				return fmt.Errorf("%w; and %s could not be rewritten to hold the workflows not dispatched (%v), so remove the ones already dispatched (%s) from it before running the retry again", err, retryPath, serr, strings.Join(retry.Workflows[:i], ", "))
			}
			return fmt.Errorf("%w\n  Dispatched before it: %s. %s now holds the workflows not yet dispatched; run the retry again once the cause is fixed", err, joinOrNone(retry.Workflows[:i]), retryPath)
		}
		ctx.Info(fmt.Sprintf("dispatched %s at %s", f, retry.Ref))
	}
	if err := runstate.RemoveRetry(e, s.Root(), s.Releasable.Name); err != nil {
		return err
	}
	if ctx.DryRun() {
		ctx.Out(fmt.Sprintf("Would dispatch %s at %s (%s)", strings.Join(retry.Workflows, ", "), tag, commit))
		return nil
	}
	ctx.Out(fmt.Sprintf("Dispatched %s at %s (%s)", strings.Join(retry.Workflows, ", "), tag, commit))
	if !req.Watch {
		ctx.Out(fmt.Sprintf("Watch the runs: rlsbl watch %s", commit))
		return nil
	}
	held = false
	if err := l.Release(); err != nil {
		return err
	}
	var ids []int64
	for _, f := range retry.Workflows {
		id, err := newDispatchedRun(gh, slug, f, commit, before[f], req.Sleep)
		if err != nil {
			return err
		}
		ids = append(ids, id)
	}
	w := ci.Watcher{GH: gh, Repo: slug, Log: ctx.Info, Sleep: req.Sleep, Now: time.Now}
	results, err := w.WatchRunIDs(ids, tag)
	if err != nil {
		return err
	}
	var failed []string
	for _, r := range results {
		if !r.Passed {
			failed = append(failed, r.Name)
		}
	}
	if len(failed) > 0 {
		return fmt.Errorf("the runs of %s that did not pass: %s", tag, strings.Join(failed, ", "))
	}
	ctx.Out(fmt.Sprintf("Every dispatched run of %s passed", tag))
	return nil
}

// dispatchedRuns are the ids of the workflow's dispatched runs on commit.
func dispatchedRuns(gh github.Client, slug github.Repository, file, commit string) (map[int64]bool, error) {
	runs, err := gh.Runs(slug, github.RunQuery{Commit: commit, Workflow: file, Event: "workflow_dispatch", Limit: runListingLimit})
	if err != nil {
		return nil, err
	}
	ids := map[int64]bool{}
	for _, r := range runs {
		ids[r.ID] = true
	}
	return ids, nil
}

// newDispatchedRun is the run a dispatch of the workflow created on commit:
// the first dispatched run not among before.
func newDispatchedRun(gh github.Client, slug github.Repository, file, commit string, before map[int64]bool, sleep func(time.Duration)) (int64, error) {
	for attempt := 1; ; attempt++ {
		runs, err := dispatchedRuns(gh, slug, file, commit)
		if err != nil {
			return 0, err
		}
		for id := range runs {
			if !before[id] {
				return id, nil
			}
		}
		if attempt == dispatchedRunAttempts {
			return 0, fmt.Errorf("%s was dispatched at %s, and no new dispatched run of it appeared on that commit within %s. Look for it with `gh run list --workflow %s --repo %s`, and watch it with `rlsbl watch --run-id <id>`", file, commit, time.Duration(dispatchedRunAttempts)*dispatchedRunInterval, file, slug)
		}
		sleep(dispatchedRunInterval)
	}
}

// dispatchableWorkflows are the workflows in the commit's tree with a
// workflow_dispatch trigger, by file name, sorted, each saying whether it
// declares the tag input.
func dispatchableWorkflows(repo git.Repo, commit string) ([]dispatchable, error) {
	files, err := publishrules.CommittedWorkflows(repo, commit)
	if err != nil {
		return nil, err
	}
	var out []dispatchable
	for _, f := range files {
		var doc map[string]any
		if err := yaml.Unmarshal([]byte(f.Text), &doc); err != nil {
			return nil, fmt.Errorf("%s at %s is not a workflow rlsbl can read: %w", f.Path, commit, err)
		}
		dispatch, ok := workflowDispatch(doc["on"])
		if !ok {
			continue
		}
		_, takesTag := dispatch[tagInput]
		out = append(out, dispatchable{file: f.Path[strings.LastIndex(f.Path, "/")+1:], takesTag: takesTag})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].file < out[j].file })
	return out, nil
}

// workflowDispatch reads a workflow's on value: whether it has the
// workflow_dispatch trigger, and the inputs that trigger declares.
func workflowDispatch(on any) (inputs map[string]any, ok bool) {
	switch v := on.(type) {
	case string:
		return nil, v == "workflow_dispatch"
	case []any:
		for _, e := range v {
			if e == "workflow_dispatch" {
				return nil, true
			}
		}
	case map[string]any:
		spec, present := v["workflow_dispatch"]
		if !present {
			return nil, false
		}
		if m, isMap := spec.(map[string]any); isMap {
			if declared, isInputs := m["inputs"].(map[string]any); isInputs {
				return declared, true
			}
		}
		return nil, true
	}
	return nil, false
}
