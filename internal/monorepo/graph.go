package monorepo

import (
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/workspace"
)

// The renderings of the graph.
const (
	FormatDot  = "dot"
	FormatTree = "tree"
)

// Graph is the members' dependency graph as `monorepo graph` reports it:
// the members and the edges between them in topological order (every
// member after the members it depends on), narrowed to a member's
// dependencies or dependents when one is named.
type Graph struct {
	// Order is the members' names in topological order.
	Order   []string      `json:"order"`
	Members []GraphMember `json:"members"`
	// Edges are ordered by the topological position of the depending
	// member, then of the member depended on.
	Edges []Edge `json:"edges"`
}

// GraphMember is one member of the graph, in topological order.
type GraphMember struct {
	Name string `json:"name"`
	Path string `json:"path"`
	// Version is the version the member's first target reads, and null for
	// a member with no target.
	Version *string  `json:"version"`
	Targets []string `json:"targets"`
	// Releasable is null for a member versioned under no releasable.
	Releasable *string `json:"releasable"`
	DevOnly    bool    `json:"dev_only"`
	Library    bool    `json:"library"`
	// Dependencies and Dependents are names within the graph, sorted.
	Dependencies []string `json:"dependencies"`
	Dependents   []string `json:"dependents"`
	// RuntimeDependents is whether a member of the graph depends on it at
	// run time (a runtime or explicit dependency).
	RuntimeDependents bool `json:"runtime_dependents"`
	// Leaf is whether no member of the graph depends on it.
	Leaf bool `json:"leaf"`
}

// Edge is one member's dependency on another.
type Edge struct {
	From string `json:"from"`
	To   string `json:"to"`
	// Form is versioned, path, workspace, or explicit.
	Form       string `json:"form"`
	Constraint string `json:"constraint"`
	// Scope is runtime, dev, peer, or explicit.
	Scope string `json:"scope"`
}

// GraphRequest narrows the graph: to Root and the members it depends on,
// or to Reverse and the members depending on it, at most Depth steps away
// (any distance when Depth is negative). At most one of Root and Reverse is
// named.
type GraphRequest struct {
	Root    string
	Reverse string
	Depth   int
}

// ReadGraph builds the graph of the workspace's members. A manifest that
// cannot be read, a cycle, and a member version that cannot be read are
// refused: the graph would state an order or a version nobody established.
func ReadGraph(ws *workspace.Workspace, req GraphRequest) (Graph, error) {
	if req.Root != "" && req.Reverse != "" {
		return Graph{}, fmt.Errorf("--root narrows the graph to what %q depends on and --reverse to what depends on %q; name one of them", req.Root, req.Reverse)
	}
	g := workspace.NewGraph(ws)
	if len(g.ScanErrors) > 0 {
		return Graph{}, scanErrors(g)
	}
	order, err := g.TopologicalOrder()
	if err != nil {
		return Graph{}, err
	}
	keep := map[string]bool{}
	switch {
	case req.Root != "":
		names, err := g.TransitiveDependencies(req.Root, req.Depth)
		if err != nil {
			return Graph{}, fmt.Errorf("--root: %w", err)
		}
		keep[req.Root] = true
		for _, n := range names {
			keep[n] = true
		}
	case req.Reverse != "":
		names, err := g.TransitiveDependents(req.Reverse, req.Depth, "")
		if err != nil {
			return Graph{}, fmt.Errorf("--reverse: %w", err)
		}
		keep[req.Reverse] = true
		for _, n := range names {
			keep[n] = true
		}
	default:
		for _, n := range order {
			keep[n] = true
		}
	}

	position := map[string]int{}
	out := Graph{Order: []string{}, Members: []GraphMember{}, Edges: []Edge{}}
	for _, name := range order {
		if !keep[name] {
			continue
		}
		position[name] = len(out.Order)
		out.Order = append(out.Order, name)
	}
	runtime := map[string]bool{}
	for _, name := range out.Order {
		for _, dep := range g.Dependencies(name) {
			if !keep[dep.Name] {
				continue
			}
			out.Edges = append(out.Edges, Edge{From: name, To: dep.Name, Form: dep.Form, Constraint: dep.Constraint, Scope: dep.Scope})
			if dep.Scope == workspace.ScopeRuntime || dep.Scope == workspace.ScopeExplicit {
				runtime[dep.Name] = true
			}
		}
	}
	sort.SliceStable(out.Edges, func(i, j int) bool {
		a, b := out.Edges[i], out.Edges[j]
		if position[a.From] != position[b.From] {
			return position[a.From] < position[b.From]
		}
		return position[a.To] < position[b.To]
	})
	for _, name := range out.Order {
		m, _ := ws.Declarations.Member(name)
		gm := GraphMember{
			Name:              m.Name,
			Path:              m.Path,
			DevOnly:           m.DevOnly,
			Library:           m.Library,
			Dependencies:      []string{},
			Dependents:        []string{},
			RuntimeDependents: runtime[name],
		}
		if m.Versioned() {
			r := m.Releasable
			gm.Releasable = &r
		}
		if gm.Targets, err = TargetNames(ws, m); err != nil {
			return Graph{}, err
		}
		v, _, found, err := MemberVersion(ws, m)
		if err != nil {
			return Graph{}, err
		}
		if found {
			gm.Version = &v
		}
		for _, e := range out.Edges {
			if e.From == name {
				gm.Dependencies = append(gm.Dependencies, e.To)
			}
			if e.To == name {
				gm.Dependents = append(gm.Dependents, e.From)
			}
		}
		sort.Strings(gm.Dependencies)
		sort.Strings(gm.Dependents)
		gm.Leaf = len(gm.Dependents) == 0
		out.Members = append(out.Members, gm)
	}
	return out, nil
}

// member is the graph's member of that name.
func (g Graph) member(name string) (GraphMember, bool) {
	for _, m := range g.Members {
		if m.Name == name {
			return m, true
		}
	}
	return GraphMember{}, false
}

// edgeStyles are the DOT attributes of an edge of each scope.
var edgeStyles = map[string]string{
	workspace.ScopeRuntime:  "",
	workspace.ScopeDev:      " [style=dashed, color=gray]",
	workspace.ScopePeer:     " [style=dotted, color=blue]",
	workspace.ScopeExplicit: " [color=black, penwidth=2]",
}

// Render is the graph as Graphviz DOT or as an indented text tree.
func (g Graph) Render(format string) (string, error) {
	switch format {
	case FormatDot:
		return g.dot(), nil
	case FormatTree:
		return g.tree(), nil
	}
	return "", fmt.Errorf("%q is not a rendering of the graph; the renderings are %s and %s", format, FormatDot, FormatTree)
}

// dot styles dev-only members gray and leaves green, and each edge by its
// scope.
func (g Graph) dot() string {
	lines := []string{
		"digraph dependencies {",
		"    rankdir=TB;",
		`    node [shape=box, fontname="Helvetica", fontsize=10];`,
	}
	names := append([]string(nil), g.Order...)
	sort.Strings(names)
	for _, name := range names {
		m, _ := g.member(name)
		switch {
		case m.DevOnly:
			lines = append(lines, fmt.Sprintf("    %q [style=filled, fillcolor=lightgray];", name))
		case m.Leaf:
			lines = append(lines, fmt.Sprintf("    %q [style=filled, fillcolor=lightgreen];", name))
		}
	}
	for _, e := range g.Edges {
		lines = append(lines, fmt.Sprintf("    %q -> %q%s;", e.From, e.To, edgeStyles[e.Scope]))
	}
	return strings.Join(append(lines, "}"), "\n")
}

// tree lists each member by name with its dependencies indented below it,
// labelled [dev], [lib], and [leaf].
func (g Graph) tree() string {
	var lines []string
	names := append([]string(nil), g.Order...)
	sort.Strings(names)
	for _, name := range names {
		lines = append(lines, g.label(name))
		m, _ := g.member(name)
		for _, dep := range m.Dependencies {
			g.subtree(dep, 1, map[string]bool{name: true}, &lines)
		}
	}
	return strings.Join(lines, "\n")
}

func (g Graph) subtree(name string, indent int, visited map[string]bool, lines *[]string) {
	*lines = append(*lines, strings.Repeat("  ", indent)+g.label(name))
	if visited[name] {
		return
	}
	next := map[string]bool{name: true}
	for k := range visited {
		next[k] = true
	}
	m, _ := g.member(name)
	for _, dep := range m.Dependencies {
		g.subtree(dep, indent+1, next, lines)
	}
}

func (g Graph) label(name string) string {
	m, _ := g.member(name)
	var labels []string
	if m.DevOnly {
		labels = append(labels, "[dev]")
	}
	if m.Library {
		labels = append(labels, "[lib]")
	}
	if m.Leaf {
		labels = append(labels, "[leaf]")
	}
	if len(labels) == 0 {
		return name
	}
	return name + " " + strings.Join(labels, " ")
}
