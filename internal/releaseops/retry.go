package releaseops

import (
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/ci"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/runstate"
)

// RetryRequest is a `release retry`.
type RetryRequest struct {
	// Dir is the working directory, absolute.
	Dir string
	// Watch watches the dispatched runs until they conclude.
	Watch bool
	// Sleep waits between the listings that find the dispatched runs.
	Sleep func(time.Duration)
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
	workflows, err := ci.DispatchableWorkflows(s.Repo, commit)
	if err != nil {
		return err
	}
	gh, slug, err := openGitHub(e, s)
	if err != nil {
		return err
	}
	if err := refuseRepublishing(gh, slug, s, commit); err != nil {
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
			retry.Workflows = append(retry.Workflows, w.File)
		}
		if err := runstate.SaveRetry(e, s.Root(), s.Releasable.Name, retry); err != nil {
			return err
		}
		ctx.Info(fmt.Sprintf("wrote %s: ref %s, workflows %s", retryPath, tag, strings.Join(retry.Workflows, ", ")))
	}
	if retry.Ref != tag && retry.Ref != "refs/tags/"+tag {
		return fmt.Errorf("%s sets ref = %q, and a retry dispatches at the release tag %s: a run dispatched at any other ref carries neither the tag nor the tagged commit, so nothing can confirm it started for this release. Set ref = %q in %s, or remove the file and run the retry again, which writes it with the tag", retryPath, retry.Ref, tag, tag, retryPath)
	}
	byFile := map[string]ci.Dispatchable{}
	for _, w := range workflows {
		byFile[w.File] = w
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
				files[i] = w.File
			}
			names = strings.Join(files, ", ")
		}
		return fmt.Errorf("%s names %s, which the tree %s tags holds no dispatchable workflow of; the dispatchable workflows there are %s. Correct the file, or remove it and run the retry again, which writes it from the tagged tree", retryPath, strings.Join(unknown, ", "), tag, names)
	}
	before := map[string]map[int64]bool{}
	if req.Watch && !ctx.DryRun() {
		for _, f := range retry.Workflows {
			if before[f], err = ci.DispatchedRuns(gh, slug, f, commit); err != nil {
				return err
			}
		}
	}
	for i, f := range retry.Workflows {
		if err := ci.Dispatch(gh, slug, byFile[f], retry.Ref, tag); err != nil {
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
		id, err := ci.NewDispatchedRun(gh, slug, f, commit, before[f], req.Sleep)
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

// refuseRepublishing refuses a retry release validation would refuse: the
// dispatched workflows publish the release again, so the releasable's
// lifecycle and the publishing rules over the workflows the tag holds are
// judged as a release judges them, before anything is written or
// dispatched.
func refuseRepublishing(gh github.Client, slug github.Repository, s Selection, commit string) error {
	record, err := lifecycle.Load(s.Root())
	if err != nil {
		return err
	}
	info, err := gh.Info(slug)
	if err != nil {
		return fmt.Errorf("GitHub could not be asked about %s (%v), and a retry must know whether the repository is public before it publishes again; check `gh auth status` and run the retry again", slug, err)
	}
	if err := release.RefuseRepublishing(s.Workspace, s.Releasable, record, s.Repo, commit, info, time.Now()); err != nil {
		return fmt.Errorf("%w\n  Nothing was dispatched: a retry publishes the release again, and is refused what a release is refused", err)
	}
	return nil
}
