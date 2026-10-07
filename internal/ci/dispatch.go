package ci

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/publishrules"
)

// TagInput is the workflow_dispatch input the generated publish workflow
// checks out when it is set.
const TagInput = "tag"

// The correlation of a dispatch with the run it created: a dispatch returns
// no run id, so the workflow's dispatched runs on the tagged commit are
// listed until one appears that was not there before.
const (
	dispatchedRunAttempts = 24
	dispatchedRunInterval = 5 * time.Second
)

// Dispatchable is one workflow of a tagged tree that can be dispatched.
type Dispatchable struct {
	// File is the workflow's file name under .github/workflows/.
	File string
	// TakesTag is whether it declares the tag input.
	TakesTag bool
}

// Dispatch dispatches the workflow at ref, passing tag as the tag input
// when the workflow declares it.
func Dispatch(gh github.Client, slug github.Repository, w Dispatchable, ref, tag string) error {
	inputs := map[string]string{}
	if w.TakesTag {
		inputs[TagInput] = tag
	}
	return gh.DispatchWorkflow(slug, w.File, ref, inputs)
}

// DispatchedRuns are the ids of the workflow's dispatched runs on commit.
func DispatchedRuns(gh github.Client, slug github.Repository, file, commit string) (map[int64]bool, error) {
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

// NewDispatchedRun is the run a dispatch of the workflow created on commit:
// the first dispatched run not among before.
func NewDispatchedRun(gh github.Client, slug github.Repository, file, commit string, before map[int64]bool, sleep func(time.Duration)) (int64, error) {
	for attempt := 1; ; attempt++ {
		runs, err := DispatchedRuns(gh, slug, file, commit)
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

// DispatchableWorkflows are the workflows in the commit's tree with a
// workflow_dispatch trigger, by file name, sorted, each saying whether it
// declares the tag input.
func DispatchableWorkflows(repo git.Repo, commit string) ([]Dispatchable, error) {
	files, err := publishrules.CommittedWorkflows(repo, commit)
	if err != nil {
		return nil, err
	}
	var out []Dispatchable
	for _, f := range files {
		var doc map[string]any
		if err := yaml.Unmarshal([]byte(f.Text), &doc); err != nil {
			return nil, fmt.Errorf("%s at %s is not a workflow rlsbl can read: %w", f.Path, commit, err)
		}
		dispatch, ok := workflowDispatch(doc["on"])
		if !ok {
			continue
		}
		_, takesTag := dispatch[TagInput]
		out = append(out, Dispatchable{File: f.Path[strings.LastIndex(f.Path, "/")+1:], TakesTag: takesTag})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].File < out[j].File })
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
