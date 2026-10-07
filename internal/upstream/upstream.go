// Package upstream is a fork's relation to its upstream: the declaration,
// the refs holding the upstream's history, the revisions changelog coverage
// leaves out in a fork, and `rlsbl upstream adopt-tags`, which moves the
// tags a fork inherited out of refs/tags.
//
// A repository is a fork when, and only when, it declares its upstream in
// .strictmetadata/upstream/upstream.toml (strictspec's built-in upstream
// schema: host, owner, repo, and branch, all required). A missing file means
// the repository is not a fork; nothing is ever inferred from a git remote.
//
// Two ref namespaces hold the upstream's history, both under the upstream's
// <host>/<owner>/<repo>, so two upstreams never collide:
//
//   - refs/tags-of/<host>/<owner>/<repo>/<tag>: a tag the fork inherited,
//     moved out of refs/tags by adopt-tags with its object unchanged and
//     pushed to origin so it is not lost. A version tag in refs/tags claims
//     that this repository released that version; an inherited one is the
//     upstream's release.
//   - refs/upstream/<host>/<owner>/<repo>/<branch>: the declared branch as
//     fetched from the upstream. rlsbl never writes it: the operator fetches
//     it with the command MissingHistoryMessage prints.
package upstream

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/rlsbl/internal/git"
)

// The declaration and the ref namespaces.
const (
	// File is where a fork declares its upstream, relative to the
	// repository root.
	File = strictspec.UpstreamFile
	// TagsOfRoot is the namespace inherited tags move to.
	TagsOfRoot = "refs/tags-of"
	// BranchRoot is the namespace the declared upstream branch is fetched
	// into.
	BranchRoot = "refs/upstream"
	// Remote is the remote the moved tags are deleted from and their kept
	// refs pushed to.
	Remote = "origin"
)

// tagsPrefix is the namespace of a repository's own tags.
const tagsPrefix = "refs/tags/"

// declarationExample is the declaration every refusal about it shows.
const declarationExample = "    format_version = 1\n    host = \"github.com\"\n    owner = \"<owner>\"\n    repo = \"<repo>\"\n    branch = \"main\""

// Upstream is a fork's declared upstream.
type Upstream struct {
	Host   string
	Owner  string
	Repo   string
	Branch string
}

// Slug is <host>/<owner>/<repo>.
func (u Upstream) Slug() string { return u.Host + "/" + u.Owner + "/" + u.Repo }

// URL is the upstream repository's https URL, which git is asked at; no
// package registry is ever asked about it.
func (u Upstream) URL() string { return "https://" + u.Slug() }

// TagsOfPrefix is the ref prefix this upstream's inherited tags live under,
// with its trailing slash.
func (u Upstream) TagsOfPrefix() string { return TagsOfRoot + "/" + u.Slug() + "/" }

// KeptRef is the ref the inherited tag moves to.
func (u Upstream) KeptRef(tag string) string { return u.TagsOfPrefix() + tag }

// BranchRef is the ref holding the declared branch as fetched from the
// upstream.
func (u Upstream) BranchRef() string { return BranchRoot + "/" + u.Slug() + "/" + u.Branch }

// BranchFetchCommand is the fetch that writes (or refreshes) BranchRef.
// --no-tags, because git otherwise follows every upstream tag pointing into
// the fetched history into refs/tags, bringing back the inherited tags
// adopt-tags moved out.
func (u Upstream) BranchFetchCommand() string {
	return fmt.Sprintf("git fetch --no-tags %s +refs/heads/%s:%s", u.URL(), u.Branch, u.BranchRef())
}

// TagsOfFetchCommand is the fetch that restores this upstream's kept tags
// from origin.
func (u Upstream) TagsOfFetchCommand() string {
	spec := u.TagsOfPrefix() + "*"
	return fmt.Sprintf("git fetch --no-tags %s '%s:%s'", Remote, spec, spec)
}

// Load reads the upstream the repository rooted at root declares; found is
// false when it declares none (it is not a fork). A declaration strictspec
// refuses is an error naming every diagnostic and the shape it must have.
func Load(root string) (u Upstream, found bool, err error) {
	declared, found, diags, err := strictspec.LoadUpstream(root)
	if err != nil {
		return Upstream{}, false, fmt.Errorf("cannot read %s: %w", File, err)
	}
	if !found {
		return Upstream{}, false, nil
	}
	if len(diags) > 0 || declared == nil {
		lines := make([]string, 0, len(diags))
		for _, d := range diags {
			lines = append(lines, fmt.Sprintf("  %s at %s: %s", d.Code, d.Path, d.Message))
		}
		return Upstream{}, true, fmt.Errorf("%s is not a valid upstream declaration:\n%s\n  It must hold format_version = 1 and the four strings host, owner, repo, and branch, for example:\n%s", File, strings.Join(lines, "\n"), declarationExample)
	}
	return Upstream{Host: declared.Host, Owner: declared.Owner, Repo: declared.Repo, Branch: declared.Branch}, true, nil
}

// URLOf is the URL of the repository the repository rooted at root is a
// fork of, and empty when it declares no upstream: the form the release
// record and the changelog take it in.
func URLOf(root string) (string, error) {
	u, found, err := Load(root)
	if err != nil || !found {
		return "", err
	}
	return u.URL(), nil
}

// MissingHistoryMessage is the refusal of a fork whose upstream branch ref
// is missing here, with the fetches that restore it.
func MissingHistoryMessage(u Upstream) string {
	return fmt.Sprintf("this repository is a fork of %s (branch %s, declared in %s), but the ref holding the upstream's history is missing here: %s\n"+
		"  Changelog coverage leaves out every commit reachable from that ref and from the %s* refs (the upstream's commits are not this repository's to describe), "+
		"so without it every inherited commit would be asked for an entry, and rlsbl refuses instead.\n"+
		"  Fetch the upstream's branch, and the inherited tags this repository keeps on origin, then run the command again:\n"+
		"    %s\n"+
		"    %s",
		u.URL(), u.Branch, File, u.BranchRef(), u.TagsOfPrefix(), u.BranchFetchCommand(), u.TagsOfFetchCommand())
}

// HistoryExclusions are the revisions whose history is the upstream's, not
// the repository's own, for a commit listing to leave out (git's --not
// list): the declared branch's ref and every kept inherited tag. They are
// empty for a repository that declares no upstream. In a fork the branch ref
// is required: a fork missing it is refused, naming the fetches that
// restore it. Kept tags are read when present; a fork whose upstream has no
// tags, or that has not adopted them yet, has none.
func HistoryExclusions(repo git.Repo) ([]string, error) {
	u, found, err := Load(repo.Dir())
	if err != nil || !found {
		return nil, err
	}
	_, present, err := repo.ResolveCommit(u.BranchRef())
	if err != nil {
		return nil, err
	}
	if !present {
		return nil, errors.New(MissingHistoryMessage(u))
	}
	kept, err := repo.LocalRefs(u.TagsOfPrefix())
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(kept))
	for name := range kept {
		names = append(names, name)
	}
	sort.Strings(names)
	return append([]string{u.BranchRef()}, names...), nil
}
