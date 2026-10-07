package release

import (
	"fmt"
	"strings"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// Progress is one releasable's standing, for a report over every releasable
// of a workspace: its latest release, and how its changelog covers the
// commits since the nearest release this checkout contains.
type Progress struct {
	Latest   releaserecord.LatestFact
	Coverage Coverage
}

// ReadProgress reads the standing of the named releasable, with the range,
// scope, and exemptions `rlsbl status` and `rlsbl unreleased` use. An
// undeclared releasable is refused, naming the declared ones.
func ReadProgress(repo git.Repo, ws *workspace.Workspace, releasable string, fork Fork) (Progress, error) {
	r, ok := ws.Declarations.Releasable(releasable)
	if !ok {
		var names []string
		for _, d := range ws.Releasables() {
			names = append(names, d.Name)
		}
		return Progress{}, fmt.Errorf("no releasable is named %q; the declared releasables are %s", releasable, strings.Join(names, ", "))
	}
	subject, err := changelog.NewSubject(repo, ws, r.Name, fork.Upstream, fork.Exclude)
	if err != nil {
		return Progress{}, err
	}
	s := selection{repo: repo, ws: ws, releasable: &r, subject: subject}
	head, err := repo.Head()
	if err != nil {
		return Progress{}, err
	}
	latest, err := s.record().Latest(head)
	if err != nil {
		return Progress{}, err
	}
	u, err := s.readUnreleased()
	if err != nil {
		return Progress{}, err
	}
	return Progress{Latest: latest, Coverage: u.coverage()}, nil
}
