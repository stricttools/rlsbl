package workspace

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
)

// How a member depends on another.
const (
	// FormVersioned is a dependency on a published version.
	FormVersioned = "versioned"
	// FormPath is a dependency on a local path.
	FormPath = "path"
	// FormWorkspace is an npm workspace: dependency.
	FormWorkspace = "workspace"
	// FormExplicit is a dependency the declarations' depends_on states.
	FormExplicit = "explicit"
)

// The scopes a dependency is needed in.
const (
	ScopeRuntime  = "runtime"
	ScopeDev      = "dev"
	ScopePeer     = "peer"
	ScopeExplicit = "explicit"
)

// Dependency is one member's dependency on another member.
type Dependency struct {
	// Name is the member depended on.
	Name string
	// Form is FormVersioned, FormPath, FormWorkspace, or FormExplicit.
	Form string
	// Constraint is the version constraint or path as the manifest writes
	// it, and empty for an explicit dependency.
	Constraint string
	// Scope is ScopeRuntime, ScopeDev, ScopePeer, or ScopeExplicit.
	Scope string
}

// ScanError is a manifest the graph could not read, attributed to its
// member.
type ScanError struct {
	Member string
	Path   string
	Err    error
}

func (e ScanError) Error() string {
	return fmt.Sprintf("the member %q: reading %s: %v", e.Member, e.Path, e.Err)
}

// CycleError is a dependency graph with a cycle; Members are the members on
// or behind it, sorted.
type CycleError struct {
	Members []string
}

func (e *CycleError) Error() string {
	return "the members' dependencies form a cycle through " + strings.Join(e.Members, ", ") + "; a release order needs a graph without one"
}

type dependent struct {
	name, scope string
}

// Graph is the members' dependency graph: the dependencies each member's
// manifests (pyproject.toml, package.json) declare on other members, and
// those its depends_on states.
//
// A manifest that cannot be read contributes no edge and is recorded in
// ScanErrors, so a consumer that renders the graph can go on, and a consumer
// that derives something narrowing from it (a CI path filter, a release
// order) refuses while ScanErrors is not empty: it cannot tell "no
// dependencies" from "nobody could read them".
type Graph struct {
	names      []string
	deps       map[string][]Dependency
	dependents map[string][]dependent
	// ScanErrors are the manifests that could not be read, in member order.
	ScanErrors []ScanError
}

var pypiSeparators = regexp.MustCompile(`[-_.]+`)

// NormalizePyPI is a PyPI name normalized per PEP 503.
func NormalizePyPI(name string) string {
	return pypiSeparators.ReplaceAllString(strings.ToLower(name), "-")
}

// NewGraph builds the graph of the workspace's members from their manifests
// on disk and their depends_on.
func NewGraph(w *Workspace) *Graph {
	g := &Graph{deps: map[string][]Dependency{}, dependents: map[string][]dependent{}}
	members := w.Members()
	names := map[string]bool{}
	pypiNames := map[string]string{}
	for _, m := range members {
		g.names = append(g.names, m.Name)
		names[m.Name] = true
		pypiNames[NormalizePyPI(m.Name)] = m.Name
	}
	// A member whose PyPI name differs from its member name is reached by
	// either. A manifest that cannot be read here is recorded by its scan.
	for _, m := range members {
		project, _, err := readPyproject(filepath.Join(w.MemberDir(m), "pyproject.toml"))
		if err != nil || project == nil {
			continue
		}
		if name, ok := project["name"].(string); ok {
			if _, taken := pypiNames[NormalizePyPI(name)]; !taken {
				pypiNames[NormalizePyPI(name)] = m.Name
			}
		}
	}

	for _, m := range members {
		dir := w.MemberDir(m)
		var found []Dependency
		pypi, err := scanPyPI(dir, pypiNames)
		if err != nil {
			g.ScanErrors = append(g.ScanErrors, ScanError{Member: m.Name, Path: filepath.Join(dir, "pyproject.toml"), Err: err})
		}
		found = append(found, pypi...)
		npm, err := scanNPM(dir, names)
		if err != nil {
			g.ScanErrors = append(g.ScanErrors, ScanError{Member: m.Name, Path: filepath.Join(dir, "package.json"), Err: err})
		}
		found = append(found, npm...)
		for _, dep := range m.DependsOn {
			found = append(found, Dependency{Name: dep, Form: FormExplicit, Scope: ScopeExplicit})
		}
		// One edge per member depended on, the first found winning.
		seen := map[string]bool{}
		for _, dep := range found {
			if dep.Name == m.Name || seen[dep.Name] {
				continue
			}
			seen[dep.Name] = true
			g.deps[m.Name] = append(g.deps[m.Name], dep)
		}
	}
	for _, name := range g.names {
		for _, dep := range g.deps[name] {
			g.dependents[dep.Name] = append(g.dependents[dep.Name], dependent{name: name, scope: dep.Scope})
		}
	}
	return g
}

// readPyproject reads a pyproject.toml's [project] table; nil when the file
// does not exist or has none.
func readPyproject(path string) (map[string]any, bool, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, true, err
	}
	doc, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return nil, true, err
	}
	project, _ := (*doc)["project"].(map[string]any)
	return project, true, nil
}

// parsePyPIRequirement reads the name of a PEP 508 requirement, whether it
// is a path requirement ("name @ file:..."), and its constraint (the version
// specifier, or the URL of a path requirement).
func parsePyPIRequirement(req string) (name string, isPath bool, constraint string) {
	req = strings.TrimSpace(req)
	if req == "" {
		return "", false, ""
	}
	if before, after, found := strings.Cut(req, " @ "); found {
		name = strings.TrimSpace(before)
		if i := strings.Index(name, "["); i >= 0 {
			name = name[:i]
		}
		return name, true, strings.TrimSpace(after)
	}
	end := strings.IndexAny(req, "[ >=<!~;")
	if end < 0 {
		return req, false, ""
	}
	name = req[:end]
	constraint = strings.TrimSpace(req[end:])
	if strings.HasPrefix(constraint, "[") {
		if closing := strings.Index(constraint, "]"); closing >= 0 {
			constraint = strings.TrimSpace(constraint[closing+1:])
		}
	}
	return name, false, constraint
}

func scanPyPI(dir string, pypiNames map[string]string) ([]Dependency, error) {
	project, found, err := readPyproject(filepath.Join(dir, "pyproject.toml"))
	if err != nil || !found || project == nil {
		return nil, err
	}
	var deps []Dependency
	add := func(requirements any, scope string) {
		list, _ := requirements.([]any)
		for _, item := range list {
			req, ok := item.(string)
			if !ok {
				continue
			}
			name, isPath, constraint := parsePyPIRequirement(req)
			member, ok := pypiNames[NormalizePyPI(name)]
			if name == "" || !ok {
				continue
			}
			form := FormVersioned
			if isPath {
				form = FormPath
			}
			deps = append(deps, Dependency{Name: member, Form: form, Constraint: constraint, Scope: scope})
		}
	}
	add(project["dependencies"], ScopeRuntime)
	if extras, ok := project["optional-dependencies"].(map[string]any); ok {
		groups := make([]string, 0, len(extras))
		for group := range extras {
			groups = append(groups, group)
		}
		sort.Strings(groups)
		for _, group := range groups {
			add(extras[group], ScopeDev)
		}
	}
	return deps, nil
}

func scanNPM(dir string, names map[string]bool) ([]Dependency, error) {
	data, err := os.ReadFile(filepath.Join(dir, "package.json"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var manifest map[string]any
	if err := json.Unmarshal(data, &manifest); err != nil {
		return nil, err
	}
	var deps []Dependency
	for _, section := range []struct{ key, scope string }{
		{"dependencies", ScopeRuntime},
		{"devDependencies", ScopeDev},
		{"peerDependencies", ScopePeer},
	} {
		table, _ := manifest[section.key].(map[string]any)
		keys := make([]string, 0, len(table))
		for name := range table {
			keys = append(keys, name)
		}
		sort.Strings(keys)
		for _, name := range keys {
			if !names[name] {
				continue
			}
			spec, _ := table[name].(string)
			form := FormVersioned
			switch {
			case strings.HasPrefix(spec, "workspace:"):
				form = FormWorkspace
			case strings.HasPrefix(spec, "file:"):
				form = FormPath
			}
			deps = append(deps, Dependency{Name: name, Form: form, Constraint: spec, Scope: section.scope})
		}
	}
	return deps, nil
}

// Members are the graph's members, in declaration order.
func (g *Graph) Members() []string { return append([]string(nil), g.names...) }

// Dependencies are the member's dependencies on other members.
func (g *Graph) Dependencies(member string) []Dependency {
	return append([]Dependency(nil), g.deps[member]...)
}

// Dependents are the members depending on the member.
func (g *Graph) Dependents(member string) []string {
	var out []string
	for _, d := range g.dependents[member] {
		out = append(out, d.name)
	}
	return out
}

// DependencyCount is the number of the member's dependencies.
func (g *Graph) DependencyCount(member string) int { return len(g.deps[member]) }

// DependentCount is the number of members depending on the member.
func (g *Graph) DependentCount(member string) int { return len(g.dependents[member]) }

// TopologicalOrder is every member, each after the members it depends on;
// among the members ready at once, by name. A cycle is a *CycleError.
func (g *Graph) TopologicalOrder() ([]string, error) {
	remaining := map[string]int{}
	var ready []string
	for _, name := range g.names {
		remaining[name] = len(g.deps[name])
		if remaining[name] == 0 {
			ready = append(ready, name)
		}
	}
	var order []string
	for len(ready) > 0 {
		sort.Strings(ready)
		next := ready[0]
		ready = ready[1:]
		order = append(order, next)
		for _, d := range g.dependents[next] {
			remaining[d.name]--
			if remaining[d.name] == 0 {
				ready = append(ready, d.name)
			}
		}
	}
	if len(order) != len(g.names) {
		var stuck []string
		for _, name := range g.names {
			if remaining[name] > 0 {
				stuck = append(stuck, name)
			}
		}
		sort.Strings(stuck)
		return nil, &CycleError{Members: stuck}
	}
	return order, nil
}

// HasCycles reports whether the graph has a cycle.
func (g *Graph) HasCycles() bool {
	_, err := g.TopologicalOrder()
	return err != nil
}

func (g *Graph) known(member string) error {
	if _, ok := g.deps[member]; ok {
		return nil
	}
	for _, name := range g.names {
		if name == member {
			return nil
		}
	}
	return fmt.Errorf("no member is named %q; the members are %s", member, strings.Join(g.names, ", "))
}

// walk is a breadth-first walk from member along next, excluding member
// itself, at most maxDepth steps (unlimited when maxDepth is negative).
func walk(member string, maxDepth int, next func(string) []string) []string {
	if maxDepth == 0 {
		return nil
	}
	visited := map[string]bool{member: true}
	type step struct {
		name  string
		depth int
	}
	var queue []step
	var out []string
	for _, n := range next(member) {
		if !visited[n] {
			visited[n] = true
			queue = append(queue, step{n, 1})
			out = append(out, n)
		}
	}
	for len(queue) > 0 {
		current := queue[0]
		queue = queue[1:]
		if maxDepth >= 0 && current.depth >= maxDepth {
			continue
		}
		for _, n := range next(current.name) {
			if !visited[n] {
				visited[n] = true
				queue = append(queue, step{n, current.depth + 1})
				out = append(out, n)
			}
		}
	}
	return out
}

// TransitiveDependencies are the members the member depends on, directly or
// not, in breadth-first order, at most maxDepth steps away (any distance when
// maxDepth is negative, none when it is 0).
func (g *Graph) TransitiveDependencies(member string, maxDepth int) ([]string, error) {
	if err := g.known(member); err != nil {
		return nil, err
	}
	return walk(member, maxDepth, func(n string) []string {
		var out []string
		for _, d := range g.deps[n] {
			out = append(out, d.Name)
		}
		return out
	}), nil
}

// TransitiveDependents are the members depending on the member, directly or
// not, in breadth-first order, at most maxDepth steps away (any distance when
// maxDepth is negative). A non-empty scope follows only edges of that scope.
func (g *Graph) TransitiveDependents(member string, maxDepth int, scope string) ([]string, error) {
	if err := g.known(member); err != nil {
		return nil, err
	}
	return walk(member, maxDepth, func(n string) []string {
		var out []string
		for _, d := range g.dependents[n] {
			if scope == "" || d.scope == scope {
				out = append(out, d.name)
			}
		}
		return out
	}), nil
}
