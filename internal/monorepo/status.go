package monorepo

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// Status is `monorepo status`'s report: one row per releasable, then one
// row per member.
type Status struct {
	Releasables []ReleasableStatus `json:"releasables"`
	Members     []MemberStatus     `json:"members"`
}

// ReleasableStatus is one releasable's row.
type ReleasableStatus struct {
	Name string `json:"name"`
	// Version is the releasable's version file's version, and null when the
	// file does not exist yet (the releasable was never released).
	Version *string `json:"version"`
	// Latest is the latest release's display, annotated when this checkout
	// does not contain it.
	Latest string `json:"latest"`
	// Coverage counts the commits since the nearest release this checkout
	// contains, in the releasable's scope, against its changelog.
	Coverage release.Coverage `json:"coverage"`
	Members  []string         `json:"members"`
}

// MemberStatus is one member's row.
type MemberStatus struct {
	Name    string   `json:"name"`
	Path    string   `json:"path"`
	Targets []string `json:"targets"`
	// Version is null for a member with no target. A member whose
	// releasable publishes nothing has its releasable's version file's
	// version, since nothing bumps its manifests; VersionSource says which.
	Version       *string `json:"version"`
	VersionSource string  `json:"version_source"`
	// Releasable is null for a member versioned under no releasable.
	Releasable   *string `json:"releasable"`
	Library      bool    `json:"library"`
	DevOnly      bool    `json:"dev_only"`
	Dependencies int     `json:"dependencies"`
	Dependents   int     `json:"dependents"`
}

// The sources of a member's version.
const (
	// VersionFromVersionFile is the releasable's version file.
	VersionFromVersionFile = "version file"
	// VersionFromNoTarget is a member with no target and so no version.
	VersionFromNoTarget = "none"
)

// ReadStatus reports on every releasable and member of the workspace.
func ReadStatus(repo git.Repo, ws *workspace.Workspace, fork release.Fork) (Status, error) {
	st := Status{Releasables: []ReleasableStatus{}, Members: []MemberStatus{}}
	for _, r := range ws.Releasables() {
		row := ReleasableStatus{Name: r.Name, Members: []string{}}
		for _, m := range ws.MembersOf(r.Name) {
			row.Members = append(row.Members, m.Name)
		}
		v, found, err := releasableVersion(ws, r.Name)
		if err != nil {
			return Status{}, err
		}
		if found {
			row.Version = &v
		}
		p, err := release.ReadProgress(repo, ws, r.Name, fork)
		if err != nil {
			return Status{}, err
		}
		row.Latest, row.Coverage = p.Latest.Label(), p.Coverage
		st.Releasables = append(st.Releasables, row)
	}
	graph := workspace.NewGraph(ws)
	for _, m := range ws.Members() {
		names, err := TargetNames(ws, m)
		if err != nil {
			return Status{}, err
		}
		row := MemberStatus{
			Name:         m.Name,
			Path:         m.Path,
			Targets:      names,
			Library:      m.Library,
			DevOnly:      m.DevOnly,
			Dependencies: graph.DependencyCount(m.Name),
			Dependents:   graph.DependentCount(m.Name),
		}
		if m.Versioned() {
			r := m.Releasable
			row.Releasable = &r
		}
		if m.Versioned() && ws.PublishModeOf(m) == declarations.PublishNone {
			v, found, err := releasableVersion(ws, m.Releasable)
			if err != nil {
				return Status{}, err
			}
			row.VersionSource = VersionFromVersionFile
			if found {
				row.Version = &v
			}
		} else {
			v, target, found, err := MemberVersion(ws, m)
			if err != nil {
				return Status{}, err
			}
			row.VersionSource = VersionFromNoTarget
			if found {
				row.Version, row.VersionSource = &v, target
			}
		}
		st.Members = append(st.Members, row)
	}
	if len(graph.ScanErrors) > 0 {
		return Status{}, scanErrors(graph)
	}
	return st, nil
}

// releasableVersion is the releasable's version file's version; found is
// false while the file does not exist.
func releasableVersion(ws *workspace.Workspace, releasable string) (string, bool, error) {
	_, err := os.Stat(filepath.Join(ws.Root, filepath.FromSlash(declarations.VersionFile(releasable))))
	if errors.Is(err, fs.ErrNotExist) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	v, err := ws.ReadReleasableVersion(releasable)
	if err != nil {
		return "", false, err
	}
	return v.String(), true, nil
}

// scanErrors is the refusal of a graph some manifest of which could not be
// read: its counts and edges would say "no dependency" where nobody could
// tell.
func scanErrors(g *workspace.Graph) error {
	var errs []error
	for _, e := range g.ScanErrors {
		errs = append(errs, e)
	}
	return errors.Join(append([]error{errors.New("the members' dependency graph is incomplete, because a manifest could not be read; fix it and run this again")}, errs...)...)
}
