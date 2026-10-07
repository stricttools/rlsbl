package checks

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/workflows"
)

// The router family: a workspace's generated CI router against the
// workspace it is derived from, through internal/workflows: present when a
// member has CI of its own, routing every member's jobs, and filtering
// pushes the way a fresh derivation would.
func routerChecks() []check {
	return []check{
		errorCheck("workspace-ci-router", checkWorkspaceCIRouter),
		errorCheck("workspace-ci-synced", checkWorkspaceCISynced),
		errorCheck("router-filters-fresh", checkRouterFiltersFresh),
	}
}

// syncFix regenerates the routers.
const syncFix = "regenerate the routers with `rlsbl monorepo sync` and commit the result"

// routerText is the committed CI router; found is false without one.
func routerText(c *Context) (string, bool) {
	data, err := os.ReadFile(filepath.Join(c.Root(), filepath.FromSlash(workflows.RouterPath)))
	if errors.Is(err, os.ErrNotExist) {
		return "", false
	}
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return string(data), true
}

// routedMembers are the members of the workspace with CI of their own,
// which the router routes.
func routedMembers(c *Context) []string {
	var names []string
	for _, m := range c.Workspace().Members() {
		prefixes, err := workflows.JobPrefixes(m.Name, c.Workspace().MemberDir(m))
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if len(prefixes) > 0 {
			names = append(names, m.Name)
		}
	}
	return names
}

func checkWorkspaceCIRouter(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	routed := routedMembers(c)
	text, found := routerText(c)
	switch {
	case len(routed) > 0 && !found:
		return reportErrors(r, []string{fmt.Sprintf("%s does not exist, so the CI of %s never runs: %s", workflows.RouterPath, strings.Join(routed, ", "), syncFix)}, workflows.RouterPath+" not found", "")
	case len(routed) == 0 && found && workflows.IsGenerated(text):
		return reportErrors(r, []string{fmt.Sprintf("%s is a generated router, and no member has CI of its own (.github/workflows/ci*.yml) for it to route: %s, which removes it", workflows.RouterPath, syncFix)}, workflows.RouterPath+" routes nothing", "")
	case len(routed) == 0:
		return r.Passed("no member has CI of its own, so there is nothing to route")
	}
	return r.Passed(fmt.Sprintf("%s exists and routes the CI of %d member(s)", workflows.RouterPath, len(routed)))
}

// routerDocument is the part of the CI router the checks read.
type routerDocument struct {
	Jobs map[string]struct {
		Outputs map[string]any `yaml:"outputs"`
		Steps   []struct {
			With map[string]any `yaml:"with"`
		} `yaml:"steps"`
	} `yaml:"jobs"`
}

// parseRouter reads the committed router's jobs.
func parseRouter(text string) (routerDocument, error) {
	var doc routerDocument
	if err := yaml.Unmarshal([]byte(text), &doc); err != nil {
		return routerDocument{}, fmt.Errorf("%s does not parse: %w", workflows.RouterPath, err)
	}
	return doc, nil
}

func checkWorkspaceCISynced(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	type required struct {
		member   string
		prefixes []string
	}
	var need []required
	for _, m := range c.Members() {
		prefixes, err := workflows.JobPrefixes(m.Name, c.Workspace().MemberDir(m))
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if len(prefixes) == 0 {
			r.Note(fmt.Sprintf("%s: no CI workflow of its own (.github/workflows/ci*.yml), so `rlsbl monorepo sync` inlines nothing for it", m.Name))
			continue
		}
		need = append(need, required{m.Name, prefixes})
	}
	if len(need) == 0 {
		return r.Passed("no member in scope has CI of its own to inline")
	}
	text, found := routerText(c)
	if !found {
		return reportErrors(r, []string{fmt.Sprintf("%s does not exist: %s", workflows.RouterPath, syncFix)}, workflows.RouterPath+" not found", "")
	}
	doc, err := parseRouter(text)
	if err != nil {
		return reportErrors(r, []string{err.Error() + ": " + syncFix}, workflows.RouterPath+" does not parse", "")
	}
	var problems []string
	for _, n := range need {
		for _, prefix := range n.prefixes {
			inlined := false
			for key := range doc.Jobs {
				if key == prefix || strings.HasPrefix(key, prefix+"-") {
					inlined = true
					break
				}
			}
			if !inlined {
				problems = append(problems, fmt.Sprintf("%s: %s holds no job of %s, so that CI workflow never runs: %s", n.member, workflows.RouterPath, prefix, syncFix))
			}
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d member CI workflow(s) not inlined in %s", len(problems), workflows.RouterPath), fmt.Sprintf("the CI of all %d member(s) is inlined in %s", len(need), workflows.RouterPath))
}

// committedFilters are the router's filters by member, its predicate
// quantifier, and the members its detect job has outputs for.
func committedFilters(doc routerDocument) (filters map[string][]string, quantifier string, routed []string, err error) {
	detect, ok := doc.Jobs["detect"]
	if !ok {
		return nil, "", nil, errors.New("the router has no detect job")
	}
	for name := range detect.Outputs {
		routed = append(routed, name)
	}
	sort.Strings(routed)
	for _, step := range detect.Steps {
		block, ok := step.With["filters"]
		if !ok {
			continue
		}
		text, ok := block.(string)
		if !ok {
			return nil, "", nil, errors.New("the detect job's filters are not a block of text")
		}
		var declared map[string]any
		if err := yaml.Unmarshal([]byte(text), &declared); err != nil {
			return nil, "", nil, fmt.Errorf("the detect job's filters do not parse: %w", err)
		}
		filters = map[string][]string{}
		for name, value := range declared {
			switch v := value.(type) {
			case string:
				filters[name] = []string{v}
			case []any:
				for _, p := range v {
					s, ok := p.(string)
					if !ok {
						return nil, "", nil, fmt.Errorf("the filter of %q holds a pattern that is not a string", name)
					}
					filters[name] = append(filters[name], s)
				}
			default:
				return nil, "", nil, fmt.Errorf("the filter of %q is neither a pattern nor a list of patterns", name)
			}
		}
		quantifier, _ = step.With["predicate-quantifier"].(string)
		return filters, quantifier, routed, nil
	}
	return nil, "", nil, errors.New("the detect job has no dorny/paths-filter step with a filters block")
}

func checkRouterFiltersFresh(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	text, found := routerText(c)
	if !found {
		return r.Skipped("no " + workflows.RouterPath + " to compare (workspace-ci-router reports a missing one)")
	}
	if !workflows.IsGenerated(text) {
		return r.Skipped(fmt.Sprintf("%s does not open with %q, so it is not a generated router", workflows.RouterPath, workflows.Header))
	}
	doc, err := parseRouter(text)
	if err != nil {
		return reportErrors(r, []string{err.Error() + ": " + syncFix}, "the router's filters cannot be read", "")
	}
	committed, quantifier, routed, err := committedFilters(doc)
	if err != nil {
		return reportErrors(r, []string{fmt.Sprintf("%s: %v: %s", workflows.RouterPath, err, syncFix)}, "the router's filters cannot be read", "")
	}
	derived, err := workflows.NewFilters(c.Workspace())
	if err != nil {
		return reportErrors(r, []string{err.Error()}, "the router's filters cannot be derived from this workspace", "")
	}
	fresh := map[string][]string{}
	for _, m := range c.Workspace().Members() {
		patterns, err := derived.PatternsFor(m)
		if err != nil {
			return reportErrors(r, []string{err.Error()}, "the router's filters cannot be derived from this workspace", "")
		}
		fresh[m.Name] = patterns
	}
	problems := filterDrift(committed, fresh, routed)
	if quantifier != workflows.PredicateQuantifier {
		problems = append(problems, fmt.Sprintf("predicate-quantifier is %q and must be %q: under the action's default a negated pattern matches everything outside itself", quantifier, workflows.PredicateQuantifier))
	}
	if len(problems) > 0 {
		problems = append(problems, "the filters no longer match the workspace they are derived from: "+syncFix)
	}
	return reportErrors(r, problems, fmt.Sprintf("%d stale filter entry/entries in %s", len(problems)-1, workflows.RouterPath), fmt.Sprintf("the %d filter(s) of %s match the workspace", len(committed), workflows.RouterPath))
}

// filterDrift names every difference between the committed filters and a
// fresh derivation. The whole of both is compared, so an entry deleted by
// hand (whose member's CI then never runs) is seen. A member gets a filter
// only when the router routes its jobs, as sync writes it.
func filterDrift(committed, fresh map[string][]string, routed []string) []string {
	expected := map[string]bool{}
	for _, name := range routed {
		if _, ok := fresh[name]; ok {
			expected[name] = true
		}
	}
	names := map[string]bool{}
	for name := range committed {
		names[name] = true
	}
	for name := range expected {
		names[name] = true
	}
	sorted := make([]string, 0, len(names))
	for name := range names {
		sorted = append(sorted, name)
	}
	sort.Strings(sorted)
	var details []string
	for _, name := range sorted {
		patterns, inCommitted := committed[name]
		_, member := fresh[name]
		switch {
		case !inCommitted:
			details = append(details, fmt.Sprintf("%s: the router routes its jobs, but the filters hold no entry for it, so nothing sets its detect output and its CI never runs", name))
		case !member:
			details = append(details, fmt.Sprintf("%s: no longer a member of the workspace", name))
		case !expected[name]:
			details = append(details, fmt.Sprintf("%s: the filters hold an entry for a member the router routes no jobs for", name))
		case !slices.Equal(patterns, fresh[name]):
			details = append(details, fmt.Sprintf("%s: committed %s, derived %s", name, strings.Join(patterns, ", "), strings.Join(fresh[name], ", ")))
		}
	}
	return details
}
