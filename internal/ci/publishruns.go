package ci

import (
	"errors"
	"fmt"
	"path"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/publishrules"
)

// The workflows a published Release starts, and whether they did.
//
// GitHub starts nothing, and reports no error, when the tagged commit's tree
// has no release-triggered workflow (the default branch's copy does not
// count), when the workflow or Actions is disabled, when the Release was
// created with a workflow's GITHUB_TOKEN, or when the event is dropped or a
// private repository is out of Actions minutes. Preflight runs before a
// release pushes anything; ConfirmRunsStarted runs after the Release exists.

// ReleaseWorkflow is a workflow whose triggers include release.
type ReleaseWorkflow struct {
	// Path is repository-relative.
	Path string
	// Types are the release activity types it filters on; nil when it takes
	// every type.
	Types []string
}

// File is the workflow's file name.
func (w ReleaseWorkflow) File() string { return path.Base(w.Path) }

// Starts reports whether publishing a Release starts the workflow: a
// published Release fires created, published, and released. A job's own
// condition does not matter; the run is created either way.
func (w ReleaseWorkflow) Starts() bool {
	if w.Types == nil {
		return true
	}
	for _, t := range w.Types {
		if t == "created" || t == "published" || t == "released" {
			return true
		}
	}
	return false
}

// releaseTypes reads a workflow's on value: whether it triggers on release,
// and the release types it filters on (nil for every type).
func releaseTypes(on any) (triggers bool, types []string, err error) {
	switch v := on.(type) {
	case string:
		return v == "release", nil, nil
	case []any:
		for _, e := range v {
			if e == "release" {
				return true, nil, nil
			}
		}
		return false, nil, nil
	case map[string]any:
		spec, ok := v["release"]
		if !ok {
			return false, nil, nil
		}
		m, isMap := spec.(map[string]any)
		if !isMap || m["types"] == nil {
			return true, nil, nil
		}
		switch t := m["types"].(type) {
		case string:
			return true, []string{t}, nil
		case []any:
			for _, e := range t {
				s, ok := e.(string)
				if !ok {
					return false, nil, errors.New("a release type is not a string")
				}
				types = append(types, s)
			}
			return true, types, nil
		}
		return false, nil, errors.New("the release types are neither a string nor a list")
	case nil:
		return false, nil, nil
	}
	return false, nil, errors.New("on is neither an event, a list of events, nor a mapping")
}

// ReleaseWorkflows are the release-triggered workflows in the commit's tree,
// by path. A workflow that is not YAML is an error naming it.
func ReleaseWorkflows(repo git.Repo, commit string) ([]ReleaseWorkflow, error) {
	files, err := publishrules.CommittedWorkflows(repo, commit)
	if err != nil {
		return nil, err
	}
	var out []ReleaseWorkflow
	for _, f := range files {
		var doc map[string]any
		if err := yaml.Unmarshal([]byte(f.Text), &doc); err != nil {
			return nil, fmt.Errorf("%s at %s is not a workflow rlsbl can read: %w", f.Path, short(commit), err)
		}
		triggers, types, err := releaseTypes(doc["on"])
		if err != nil {
			return nil, fmt.Errorf("%s at %s: %w", f.Path, short(commit), err)
		}
		if triggers {
			out = append(out, ReleaseWorkflow{Path: f.Path, Types: types})
		}
	}
	return out, nil
}

// PublishRuns asks GitHub about the publish workflows of one repository.
type PublishRuns struct {
	GH   github.Client
	Repo github.Repository
	Git  git.Repo
	Log  func(string)
	// Sleep waits between the confirmation's questions; Now reads the
	// clock its budget and the Release's age are measured on.
	Sleep func(time.Duration)
	Now   func() time.Time
}

// enableActions is the command enabling Actions for the repository.
func (p PublishRuns) enableActions() string {
	return "gh api --method PUT repos/" + p.Repo.String() + "/actions/permissions -F enabled=true"
}

// Preflight refuses a release whose Release would start no publish
// workflow. When the release publishes from CI, the tree at commit must hold
// a workflow a published Release starts; when any does, Actions must be
// enabled and each one GitHub already knows must be active.
func (p PublishRuns) Preflight(commit string, publishes bool) error {
	all, err := ReleaseWorkflows(p.Git, commit)
	if err != nil {
		return err
	}
	var starting []ReleaseWorkflow
	for _, w := range all {
		if w.Starts() {
			starting = append(starting, w)
		}
	}
	if publishes && len(starting) == 0 {
		return fmt.Errorf("the release publishes from CI, and no workflow in %s at %s starts when a GitHub Release is published, so nothing would publish it; run `rlsbl scaffold` (`rlsbl monorepo sync --auto-commit` in a workspace) to generate the publish workflow, commit it, and run the release again", publishrules.WorkflowsDir, short(commit))
	}
	if len(starting) == 0 {
		return nil
	}
	enabled, err := p.GH.ActionsEnabled(p.Repo)
	if err != nil {
		return fmt.Errorf("whether GitHub Actions is enabled for %s could not be read (%v), and a Release starts its publish workflows only when it is; check `gh auth status` (the token needs read access to the repository's Actions settings) and run the release again", p.Repo, err)
	}
	if !enabled {
		var paths []string
		for _, w := range starting {
			paths = append(paths, w.Path)
		}
		return fmt.Errorf("GitHub Actions is disabled for %s, so the published Release would start none of %s; enable it with `%s` and run the release again", p.Repo, strings.Join(paths, ", "), p.enableActions())
	}
	for _, w := range starting {
		state, known, err := p.GH.WorkflowState(p.Repo, w.File())
		if err != nil {
			return fmt.Errorf("the state of %s on GitHub could not be read (%v), and a disabled workflow starts nothing; check `gh auth status` and run the release again", w.Path, err)
		}
		if known && state != "active" {
			return fmt.Errorf("%s is %s on GitHub, so the published Release would not start it; enable it with `gh workflow enable %s --repo %s` and run the release again", w.Path, state, w.File(), p.Repo)
		}
	}
	return nil
}

// The confirmation's budget: how long a run may take to appear after the
// Release is published, and how often to look.
const (
	ConfirmDiscovery = 300 * time.Second
	ConfirmInterval  = 5 * time.Second
)

// runsForTag are the workflow's runs for the tag at sha: from the Release
// or from a dispatch at the tag.
func (p PublishRuns) runsForTag(file, tag, sha string) (int, error) {
	runs, err := p.GH.WorkflowRunsAt(p.Repo, file, sha)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, r := range runs {
		if r.HeadBranch == tag && (r.Event == "release" || r.Event == "workflow_dispatch") {
			n++
		}
	}
	return n, nil
}

// ConfirmRunsStarted requires a run for the tag of every workflow in the
// tagged commit's tree that a published Release starts, within discovery.
// A workflow whose runs query never answered is unconfirmed rather than
// absent: whether it started is unknown, and saying it did not would send
// someone to dispatch a second publish. A Release older than the
// repository's run retention has had its runs deleted, which is reported,
// not refused.
func (p PublishRuns) ConfirmRunsStarted(tag, sha string, discovery, interval time.Duration) error {
	all, err := ReleaseWorkflows(p.Git, sha)
	if err != nil {
		return err
	}
	var missing []ReleaseWorkflow
	for _, w := range all {
		if w.Starts() {
			missing = append(missing, w)
		}
	}
	if len(missing) == 0 {
		return nil
	}
	names := func(ws []ReleaseWorkflow) string {
		var out []string
		for _, w := range ws {
			out = append(out, w.Path)
		}
		return strings.Join(out, ", ")
	}
	expected := names(missing)
	p.Log(fmt.Sprintf("confirming the Release %s started %s", tag, expected))
	deadline := p.Now().Add(discovery)
	answered := map[string]bool{}
	var lastErr error
	for {
		var still []ReleaseWorkflow
		for _, w := range missing {
			n, err := p.runsForTag(w.File(), tag, sha)
			if err != nil {
				lastErr = err
				still = append(still, w)
				continue
			}
			answered[w.Path] = true
			if n == 0 {
				still = append(still, w)
			}
		}
		missing = still
		if len(missing) == 0 {
			p.Log(fmt.Sprintf("the publish runs for %s started: %s", tag, expected))
			return nil
		}
		if !p.Now().Before(deadline) {
			break
		}
		p.Sleep(interval)
	}
	var unconfirmed, absent []ReleaseWorkflow
	for _, w := range missing {
		if answered[w.Path] {
			absent = append(absent, w)
		} else {
			unconfirmed = append(unconfirmed, w)
		}
	}
	if len(absent) == 0 {
		return fmt.Errorf("%s is tagged and released, and whether %s started for it could not be confirmed: every runs query failed for %s (the last: %v). A run may well have started, so dispatch nothing yet: once GitHub answers, `rlsbl watch %s` confirms the start and names the fix if none did", tag, names(unconfirmed), discovery, lastErr, sha)
	}
	published, err := p.GH.ReleasePublishedAt(p.Repo, tag)
	var days int
	if err == nil && p.Now().Sub(published) >= 24*time.Hour {
		days, err = p.GH.RunRetentionDays(p.Repo)
	}
	if err != nil {
		return fmt.Errorf("no run of %s was found for %s, and whether none started or GitHub deleted them under the repository's run retention cannot be told: reading the Release's publication time or the retention failed (%v). A run may well have started, so dispatch nothing yet; run this again once GitHub answers", names(absent), tag, err)
	}
	if days > 0 && p.Now().Sub(published) > time.Duration(days)*24*time.Hour {
		p.Log(fmt.Sprintf("%s was published %s, longer ago than the repository's %d-day run retention, and GitHub has deleted runs of that age, so whether %s started for it can no longer be confirmed", tag, published.Format("2006-01-02"), days, names(absent)))
		return nil
	}
	lines := []string{fmt.Sprintf("%s is tagged and released, and no run of %s started for it within %s. A published Release starts these workflows, and GitHub reports no error when one does not start.", tag, names(absent), discovery)}
	if len(unconfirmed) > 0 {
		lines = append(lines, fmt.Sprintf("  Whether %s started could not be confirmed: its runs query failed every time (%v).", names(unconfirmed), lastErr))
	}
	found := p.diagnose(tag, absent)
	if len(found) > 0 {
		lines = append(lines, "  Found:")
		for _, f := range found {
			lines = append(lines, "    - "+f)
		}
	} else {
		lines = append(lines, "  Nothing rlsbl can read explains it: the event may have been dropped or throttled, and a private repository out of Actions minutes starts nothing.")
	}
	lines = append(lines, fmt.Sprintf("  The tag and the Release exist, so nothing needs releasing again. Fix the cause, start the workflows at the tag with `rlsbl release retry --watch`, and confirm with `rlsbl watch %s`.", sha))
	return errors.New(strings.Join(lines, "\n"))
}

// diagnose is what rlsbl can read about why the workflows did not start.
func (p PublishRuns) diagnose(tag string, missing []ReleaseWorkflow) []string {
	var found []string
	if author, err := p.GH.ReleaseAuthor(p.Repo, tag); err != nil {
		found = append(found, fmt.Sprintf("the Release's author could not be read (%v)", err))
	} else if strings.HasSuffix(author, "[bot]") {
		found = append(found, fmt.Sprintf("the Release was created by %s: events created with a workflow's GITHUB_TOKEN start no workflow run, so create the Release with a personal or app token", author))
	}
	if enabled, err := p.GH.ActionsEnabled(p.Repo); err != nil {
		found = append(found, fmt.Sprintf("whether GitHub Actions is enabled could not be read (%v)", err))
	} else if !enabled {
		found = append(found, "GitHub Actions is disabled for this repository")
	}
	for _, w := range missing {
		state, known, err := p.GH.WorkflowState(p.Repo, w.File())
		switch {
		case err != nil:
			found = append(found, fmt.Sprintf("the state of %s could not be read (%v)", w.Path, err))
		case !known:
			found = append(found, fmt.Sprintf("GitHub does not know %s", w.Path))
		case state != "active":
			found = append(found, fmt.Sprintf("%s is %s: enable it with `gh workflow enable %s --repo %s`", w.Path, state, w.File(), p.Repo))
		}
	}
	return found
}
