package release

import (
	"fmt"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// Fork is a fork's upstream history, which every unreleased range leaves
// out: the URL of the repository this one is a fork of, and the revisions
// whose history is the upstream's. The zero value is a repository that is
// no fork.
type Fork struct {
	Upstream string
	Exclude  []string
}

// selection is what a working directory selects: the repository, the
// member whose territory holds the directory, and that member's releasable
// with its changelog, when it is versioned under one.
type selection struct {
	repo   git.Repo
	ws     *workspace.Workspace
	member declarations.Member
	// releasable is nil for a member versioned under no releasable, which
	// has no release record and no changelog.
	releasable *declarations.Releasable
	subject    changelog.Subject
}

// selectAt reads the repository holding the absolute directory dir and what
// dir selects in it.
func selectAt(e *strictcli.Effects, dir string, fork Fork) (selection, error) {
	ws, err := workspace.Discover(dir)
	if err != nil {
		return selection{}, err
	}
	repo, err := git.Open(e, ws.Root)
	if err != nil {
		return selection{}, err
	}
	member, err := ws.MemberAtDirectory(dir)
	if err != nil {
		return selection{}, err
	}
	s := selection{repo: repo, ws: ws, member: member}
	r, ok := ws.ReleasableOf(member)
	if !ok {
		return s, nil
	}
	s.releasable = &r
	if s.subject, err = changelog.NewSubject(repo, ws, r.Name, fork.Upstream, fork.Exclude); err != nil {
		return selection{}, err
	}
	return s, nil
}

// record is the release record of the selected releasable.
func (s selection) record() *releaserecord.Record { return s.subject.Record }

// requireReleasable refuses a selection whose member is versioned under no
// releasable, for a command whose answer is about a releasable's releases.
func (s selection) requireReleasable(command string) error {
	if s.releasable != nil {
		return nil
	}
	return fmt.Errorf("the member %q (path %q) is versioned under no releasable, so it has no release record and no changelog for `rlsbl %s` to read: run it from the directory of a member that is versioned under one", s.member.Name, s.member.Path, command)
}

// Coverage counts how a releasable's unreleased commits stand against its
// changelog: Total are the commits that need an entry, Covered those of
// them some entry names, and Exempted the commits that need none.
type Coverage struct {
	Covered  int `json:"covered"`
	Total    int `json:"total"`
	Exempted int `json:"exempted"`
}

// unreleased is the selected releasable's unreleased work: the range from
// its nearest release, the commits of it in the releasable's scope (newest
// first), which of them are exempt from changelog coverage, and which its
// unreleased entries name.
type unreleased struct {
	nearest *releaserecord.Entry
	scoped  []string
	exempt  map[string]bool
	covered map[string]bool
}

// readUnreleased reads the selected releasable's unreleased work, scoping
// the range before exempting, the order changelog coverage uses, so another
// releasable's commits never count among this one's exempted.
func (s selection) readUnreleased() (unreleased, error) {
	head, err := s.repo.Head()
	if err != nil {
		return unreleased{}, err
	}
	nearest, err := s.record().Nearest(head)
	if err != nil {
		return unreleased{}, err
	}
	rng, err := s.subject.UnreleasedRange()
	if err != nil {
		return unreleased{}, err
	}
	scoped, err := workspace.FilterCommits(s.repo, rng.Commits, s.subject.Scope())
	if err != nil {
		return unreleased{}, err
	}
	u := unreleased{nearest: nearest, scoped: scoped, exempt: map[string]bool{}}
	for _, sha := range scoped {
		why, err := changelog.ExemptionOf(s.repo, sha)
		if err != nil {
			return unreleased{}, err
		}
		if why != "" {
			u.exempt[sha] = true
		}
	}
	f, err := changelog.ReadUnreleased(s.ws.Root, s.subject.Dir())
	if err != nil {
		return unreleased{}, err
	}
	if u.covered, err = s.subject.CoveredCommits(f); err != nil {
		return unreleased{}, err
	}
	return u, nil
}

// coverage counts the unreleased work.
func (u unreleased) coverage() Coverage {
	var c Coverage
	for _, sha := range u.scoped {
		switch {
		case u.exempt[sha]:
			c.Exempted++
		case u.covered[sha]:
			c.Covered++
			c.Total++
		default:
			c.Total++
		}
	}
	return c
}

// LatestFields are the latest release's fields of a payload: its version,
// whether this checkout contains it (null when there is no release, or its
// commit is unrecoverable), its fate, the never-released versions archived
// above it, and its display.
type LatestFields struct {
	LatestRelease           *string  `json:"latest_release"`
	LatestReleaseInCheckout *bool    `json:"latest_release_in_checkout"`
	LatestReleaseState      *string  `json:"latest_release_state"`
	NeverReleasedVersions   []string `json:"never_released_versions"`
	LatestReleaseLabel      string   `json:"latest_release_label"`
}

func latestOf(f releaserecord.LatestFact) LatestFields {
	out := LatestFields{NeverReleasedVersions: []string{}, LatestReleaseLabel: f.Label()}
	for _, v := range f.NeverReleasedAbove {
		out.NeverReleasedVersions = append(out.NeverReleasedVersions, v.String())
	}
	if !f.Released {
		return out
	}
	version, state := f.Version.String(), f.State()
	out.LatestRelease, out.LatestReleaseState = &version, &state
	if !f.Unrecoverable {
		in := f.InCheckout
		out.LatestReleaseInCheckout = &in
	}
	return out
}
