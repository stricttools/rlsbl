package monorepo

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// ImpactRequest names what changed: members or repository-relative paths
// (Subjects), or every file changed by the commits since a revision (Since).
// Depth bounds the transitive dependents (any distance when negative).
type ImpactRequest struct {
	Subjects []string
	Since    string
	Depth    int
}

// Impact is what a change reaches through the dependency graph.
type Impact struct {
	// Changed are the members the change touches, sorted.
	Changed []string `json:"changed"`
	// DirectDependents depend on a changed member, sorted.
	DirectDependents []string `json:"direct_dependents"`
	// TransitiveDependents depend on one directly or not, within the depth,
	// sorted: the members to test and to consider releasing.
	TransitiveDependents []string `json:"transitive_dependents"`
}

// ReadImpact maps the request to the members it touches and walks their
// dependents. A subject is a member's name or a path in the repository;
// one that is neither, one that is both with two different answers, and a
// path that belongs to no member (rlsbl's own records) are refused.
func ReadImpact(repo git.Repo, ws *workspace.Workspace, req ImpactRequest) (Impact, error) {
	switch {
	case req.Since != "" && len(req.Subjects) > 0:
		return Impact{}, errors.New("name the change either with members and paths or with --since, not both")
	case req.Since == "" && len(req.Subjects) == 0:
		return Impact{}, errors.New("name what changed: a member, a path relative to the repository root, or --since <revision> for the files the commits since it changed")
	}
	g := workspace.NewGraph(ws)
	if len(g.ScanErrors) > 0 {
		return Impact{}, scanErrors(g)
	}
	changed := map[string]bool{}
	if req.Since != "" {
		files, err := filesChangedSince(repo, req.Since)
		if err != nil {
			return Impact{}, err
		}
		for name := range ws.OwnerNames(files) {
			changed[name] = true
		}
	}
	for _, s := range req.Subjects {
		name, err := subjectMember(ws, s)
		if err != nil {
			return Impact{}, err
		}
		changed[name] = true
	}
	direct, transitive := map[string]bool{}, map[string]bool{}
	for name := range changed {
		for _, d := range g.Dependents(name) {
			direct[d] = true
		}
		names, err := g.TransitiveDependents(name, req.Depth, "")
		if err != nil {
			return Impact{}, err
		}
		for _, d := range names {
			transitive[d] = true
		}
	}
	return Impact{Changed: sortedSet(changed), DirectDependents: sortedSet(direct), TransitiveDependents: sortedSet(transitive)}, nil
}

// subjectMember is the member a subject names: the member of that name, or
// the member owning that path.
func subjectMember(ws *workspace.Workspace, subject string) (string, error) {
	byName, named := ws.Declarations.Member(subject)
	byPath, pathFound, err := pathOwner(ws, subject)
	if err != nil {
		return "", err
	}
	switch {
	case named && pathFound && byName.Name != byPath:
		return "", fmt.Errorf("%q names the member %q, and as a path it lies in the member %q; name the member, or spell the path so it names nothing else (\"./%s\")", subject, byName.Name, byPath, subject)
	case named:
		return byName.Name, nil
	case pathFound:
		return byPath, nil
	}
	return "", fmt.Errorf("%q is neither a member's name nor a path in the repository; the members are %s", subject, strings.Join(memberNames(ws), ", "))
}

// pathOwner is the member owning the repository-relative path p, which must
// exist; found is false when nothing exists at p. A path belonging to no
// member is refused.
func pathOwner(ws *workspace.Workspace, p string) (owner string, found bool, err error) {
	canonical, ok := declarations.CanonicalPath(p)
	if !ok {
		return "", false, nil
	}
	if _, err := os.Lstat(filepath.Join(ws.Root, filepath.FromSlash(canonical))); errors.Is(err, fs.ErrNotExist) {
		return "", false, nil
	} else if err != nil {
		return "", false, err
	}
	m, ok := ws.OwnerOf(canonical)
	if !ok {
		return "", false, fmt.Errorf("%s is one of rlsbl's own records (%s) and belongs to no member", canonical, workspace.ToolOwnedRule(canonical))
	}
	return m.Name, true, nil
}

// filesChangedSince are the files the commits reachable from HEAD and not
// from since change.
func filesChangedSince(repo git.Repo, since string) ([]string, error) {
	sha, found, err := repo.ResolveCommit(since)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, fmt.Errorf("--since %s names no commit in this repository", since)
	}
	commits, err := repo.Commits([]string{"HEAD"}, []string{sha})
	if err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	var files []string
	for _, c := range commits {
		list, err := repo.CommitFiles(c)
		if err != nil {
			return nil, err
		}
		for _, f := range list {
			if !seen[f] {
				seen[f] = true
				files = append(files, f)
			}
		}
	}
	return files, nil
}

func sortedSet(set map[string]bool) []string {
	out := []string{}
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
