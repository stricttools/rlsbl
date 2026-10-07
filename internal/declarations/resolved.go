package declarations

import (
	"fmt"
	"sort"
	"strings"
)

// ResolvedTarget is one publishable (target, pipeline) pair of a member: the
// flat view a release iterates over. A target published by several
// pipelines is one record per pipeline; a target no pipeline publishes is
// one record with no pipeline.
type ResolvedTarget struct {
	Target Target
	// Dir is the target's directory, canonical and repository-relative.
	Dir string
	// Pipeline is nil for a target no pipeline publishes.
	Pipeline    *Pipeline
	PublishMode PublishMode
	// Primary marks the release's primary target: at most one record, the
	// first whose target is the one the release file names first.
	Primary bool
}

// TargetDir is the canonical repository-relative directory of a member's
// target.
func (m Member) TargetDir(t Target) string {
	if t.Path == "" {
		return m.Path
	}
	return Join(m.Path, t.Path)
}

// ResolveTargets pairs the member's targets (declared, or detected from its
// manifests when it declares none) with its pipelines, in target order and,
// within a target, pipeline order. primary names the release's primary
// target, or is empty outside a release. A pipeline publishing a target not
// among targets is an error naming both.
func ResolveTargets(m Member, targets []Target, mode PublishMode, primary string) ([]ResolvedTarget, error) {
	byTarget := map[string][]Pipeline{}
	for _, p := range m.Pipelines {
		byTarget[p.Target] = append(byTarget[p.Target], p)
	}
	known := map[string]bool{}
	var names []string
	for _, t := range targets {
		known[t.Name] = true
		names = append(names, t.Name)
	}
	var dangling []string
	for target, pipelines := range byTarget {
		if known[target] {
			continue
		}
		for _, p := range pipelines {
			dangling = append(dangling, fmt.Sprintf("%s (publishing %s)", p.Name, target))
		}
	}
	if len(dangling) > 0 {
		sort.Strings(dangling)
		return nil, fmt.Errorf("the member %q has pipelines publishing targets it does not have: %s; its targets are %s", m.Name, strings.Join(dangling, ", "), strings.Join(names, ", "))
	}
	var out []ResolvedTarget
	for _, t := range targets {
		pipelines := byTarget[t.Name]
		if len(pipelines) == 0 {
			out = append(out, ResolvedTarget{Target: t, Dir: m.TargetDir(t), PublishMode: mode})
			continue
		}
		for i := range pipelines {
			p := pipelines[i]
			out = append(out, ResolvedTarget{Target: t, Dir: m.TargetDir(t), Pipeline: &p, PublishMode: mode})
		}
	}
	if primary != "" {
		for i := range out {
			if out[i].Target.Name == primary {
				out[i].Primary = true
				break
			}
		}
	}
	return out, nil
}
