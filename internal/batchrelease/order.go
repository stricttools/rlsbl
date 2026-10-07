// Package batchrelease is `rlsbl monorepo release`: run releases several
// releasables of a workspace as one batch, init writes the batch release
// file naming them, and order reports the order a batch releases them in.
//
// Every command reads the declarations of the repository holding the
// working directory and refuses a standalone layout, whose one releasable
// is released by `rlsbl release run`.
package batchrelease

import (
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// requireWorkspace refuses the declarations of a standalone project.
// command names the monorepo release command for the refusal.
func requireWorkspace(ws *workspace.Workspace, command string) error {
	if ws.IsWorkspace() {
		return nil
	}
	return fmt.Errorf("`rlsbl monorepo release %s` works on a workspace, and %s declares repository_layout = %q: a standalone project has one releasable, released with `rlsbl release init` and `rlsbl release run --watch`", command, declarations.ReleasablesFile, declarations.LayoutStandalone)
}

// MemberOrder is every member of the workspace, each after the members it
// depends on (its manifests' dependencies on other members and its
// depends_on); among members ready at once, by name. A manifest that cannot
// be read and a cycle are refused: the order would leave out a dependency
// nobody could read.
func MemberOrder(ws *workspace.Workspace) ([]string, *workspace.Graph, error) {
	g := workspace.NewGraph(ws)
	if len(g.ScanErrors) > 0 {
		lines := []string{"the members' dependency graph is incomplete, because a manifest could not be read, so no release order can be derived from it; fix each manifest and run this again:"}
		for _, e := range g.ScanErrors {
			rel, err := filepath.Rel(ws.Root, e.Path)
			if err != nil {
				rel = e.Path
			}
			lines = append(lines, fmt.Sprintf("  - %s: %s (%v)", e.Member, filepath.ToSlash(rel), e.Err))
		}
		return nil, nil, errors.New(strings.Join(lines, "\n"))
	}
	order, err := g.TopologicalOrder()
	if err != nil {
		return nil, nil, err
	}
	return order, g, nil
}

// ReleasableOrder is the order a batch release releases the named
// releasables in: by the latest position any of a releasable's members takes
// in MemberOrder, so a releasable whose members depend on another
// releasable's members (a server on the clients its depends_on names) comes
// after it; ties by name. A name the declarations do not hold is refused,
// naming the declared releasables.
func ReleasableOrder(ws *workspace.Workspace, names []string) ([]string, error) {
	members, _, err := MemberOrder(ws)
	if err != nil {
		return nil, err
	}
	position := map[string]int{}
	for i, m := range members {
		position[m] = i
	}
	var unknown []string
	latest := map[string]int{}
	for _, name := range names {
		if _, ok := ws.Declarations.Releasable(name); !ok {
			unknown = append(unknown, name)
			continue
		}
		latest[name] = -1
		for _, m := range ws.MembersOf(name) {
			if p := position[m.Name]; p > latest[name] {
				latest[name] = p
			}
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return nil, fmt.Errorf("%s does not declare %s %s; the declared releasables are %s", declarations.ReleasablesFile, plural(len(unknown), "the releasable", "the releasables"), strings.Join(unknown, ", "), strings.Join(declaredNames(ws), ", "))
	}
	out := append([]string(nil), names...)
	sort.SliceStable(out, func(i, j int) bool {
		if latest[out[i]] != latest[out[j]] {
			return latest[out[i]] < latest[out[j]]
		}
		return out[i] < out[j]
	})
	return out, nil
}

// declaredNames are the declared releasables' names, in declaration order.
func declaredNames(ws *workspace.Workspace) []string {
	var names []string
	for _, r := range ws.Releasables() {
		names = append(names, r.Name)
	}
	return names
}

func plural(n int, one, many string) string {
	if n == 1 {
		return one
	}
	return many
}

// OrderReport is what `rlsbl monorepo release order` reports.
type OrderReport struct {
	// Members are every member, each after the members it depends on.
	Members []string `json:"members"`
	// Independent is true when no member depends on another.
	Independent bool `json:"independent"`
	// Releasables are every releasable, in the order a batch release
	// releases them.
	Releasables []string `json:"releasables"`
}

// Order reports the release order of the workspace holding the absolute
// directory dir.
func Order(dir string) (OrderReport, error) {
	ws, err := workspace.Discover(dir)
	if err != nil {
		return OrderReport{}, err
	}
	if err := requireWorkspace(ws, "order"); err != nil {
		return OrderReport{}, err
	}
	members, g, err := MemberOrder(ws)
	if err != nil {
		return OrderReport{}, err
	}
	releasables, err := ReleasableOrder(ws, declaredNames(ws))
	if err != nil {
		return OrderReport{}, err
	}
	independent := true
	for _, m := range members {
		if g.DependencyCount(m) > 0 {
			independent = false
		}
	}
	if releasables == nil {
		releasables = []string{}
	}
	return OrderReport{Members: members, Independent: independent, Releasables: releasables}, nil
}

// Render is the report as text.
func (r OrderReport) Render() string {
	var b strings.Builder
	if r.Independent {
		b.WriteString("Every member is independent (no member depends on another):\n")
		for _, m := range r.Members {
			fmt.Fprintf(&b, "  %s\n", m)
		}
	} else {
		b.WriteString("Members, each after the members it depends on:\n")
		for i, m := range r.Members {
			fmt.Fprintf(&b, "  %d. %s\n", i+1, m)
		}
	}
	b.WriteString("\nReleasables, in the order a batch release releases them:\n")
	if len(r.Releasables) == 0 {
		b.WriteString("  (none declared)\n")
	}
	for i, name := range r.Releasables {
		fmt.Fprintf(&b, "  %d. %s\n", i+1, name)
	}
	return strings.TrimRight(b.String(), "\n")
}
