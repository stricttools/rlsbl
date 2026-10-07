package monorepo

import (
	"github.com/stricttools/rlsbl/internal/workspace"
)

// Dependency is one member's dependency on another member, with the
// version the depended-on member has now and what its constraint says
// about that version.
type Dependency struct {
	Member     string `json:"member"`
	Dependency string `json:"dependency"`
	Form       string `json:"form"`
	// Constraint is the constraint or path the manifest writes, and empty
	// for an explicit dependency.
	Constraint string `json:"constraint"`
	// Current is the depended-on member's version, and null when it has
	// no target.
	Current *string `json:"current"`
	// Status is ok, outdated, or versioned (a constraint the evaluation does
	// not read) for a versioned dependency, and the form otherwise.
	Status string `json:"status"`
}

// Outdated lists every dependency between members, member by member in
// declaration order, each judged against the version the depended-on member
// has now.
func Outdated(ws *workspace.Workspace) ([]Dependency, error) {
	graph := workspace.NewGraph(ws)
	if len(graph.ScanErrors) > 0 {
		return nil, scanErrors(graph)
	}
	versions := map[string]*string{}
	for _, m := range ws.Members() {
		v, _, found, err := MemberVersion(ws, m)
		if err != nil {
			return nil, err
		}
		if found {
			versions[m.Name] = &v
		}
	}
	out := []Dependency{}
	for _, m := range ws.Members() {
		for _, dep := range graph.Dependencies(m.Name) {
			row := Dependency{
				Member:     m.Name,
				Dependency: dep.Name,
				Form:       dep.Form,
				Constraint: dep.Constraint,
				Current:    versions[dep.Name],
				Status:     dep.Form,
			}
			if dep.Form == workspace.FormVersioned {
				current := ""
				if row.Current != nil {
					current = *row.Current
				}
				row.Status = string(workspace.EvaluateConstraint(dep.Constraint, current))
			}
			out = append(out, row)
		}
	}
	return out, nil
}
