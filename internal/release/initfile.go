package release

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// InitCommitMessage is the subject of the commit `rlsbl release init`
// makes.
const InitCommitMessage = "release: scaffold unreleased.toml"

// InitResult is what `rlsbl release init` did.
type InitResult struct {
	// Path is the release file, repository-relative.
	Path string
	// Written is false when a release file nobody filled in was there
	// already, so nothing was written.
	Written bool
}

// Init is `rlsbl release init`: it writes the release file of the
// releasable the working directory dir selects, with bump and description
// left blank for the operator to fill in and every target of the
// releasable's members in include, and commits it. A release file nobody
// filled in yet is left as it is; one somebody filled in is refused, never
// overwritten. liveRoot is the root of the repository holding dir.
func Init(e *strictcli.Effects, liveRoot, dir string) (InitResult, error) {
	ws, err := workspace.Load(liveRoot)
	if err != nil {
		return InitResult{}, err
	}
	member, err := ws.MemberAtDirectory(dir)
	if err != nil {
		return InitResult{}, err
	}
	r, ok := ws.ReleasableOf(member)
	if !ok {
		return InitResult{}, &ValidationError{Message: fmt.Sprintf("the working directory lies in the member %q, which is versioned under no releasable, so it has no release file; run `rlsbl release init` from a member of a releasable, or `rlsbl monorepo release init` for a batch release", member.Name)}
	}
	rel := releaserecord.ReleaseFilePath(r.Name)
	abs := filepath.Join(liveRoot, filepath.FromSlash(rel))
	existing, err := os.ReadFile(abs)
	switch {
	case err == nil:
		if releaserecord.IsPristineReleaseFile(existing) {
			return InitResult{Path: rel}, nil
		}
		return InitResult{}, &ValidationError{Message: fmt.Sprintf("%s exists and somebody filled it in, so it is not overwritten; edit it, or delete it (saferm delete --on-error abort --description \"a release file to scaffold again\" %s) and run `rlsbl release init` again", rel, abs)}
	case !errors.Is(err, fs.ErrNotExist):
		return InitResult{}, err
	}
	include, err := releasableTargets(ws, r)
	if err != nil {
		return InitResult{}, err
	}
	if len(include) == 0 {
		return InitResult{}, &ValidationError{Message: fmt.Sprintf("no target (go, npm, or pypi) is declared or detected in any member of %s, so a release file would include nothing; add a manifest (go.mod, package.json, pyproject.toml) or declare the members' targets in %s, then run `rlsbl release init` again", r.Name, declarations.ReleasablesFile)}
	}
	repo, err := git.Open(e, liveRoot)
	if err != nil {
		return InitResult{}, err
	}
	manifest := path.Join(declarations.ReleasesRoot, "manifest.toml")
	manifestTracked, err := repo.Tracks(manifest)
	if err != nil {
		return InitResult{}, err
	}
	if err := declarations.EnsureOwnedDirectory(e, liveRoot, declarations.ReleasesRoot); err != nil {
		return InitResult{}, err
	}
	if _, err := e.Mkdir(filepath.Dir(abs)); err != nil {
		return InitResult{}, err
	}
	if _, err := e.Write(abs, []byte(scaffoldedReleaseFile(r.Name, include)), strictcli.Mode(0o644)); err != nil {
		return InitResult{}, err
	}
	paths := []string{rel}
	if !manifestTracked {
		// The first write into the releases directory created its ownership
		// manifest, which the commit carries.
		paths = append(paths, manifest)
	}
	if _, err := repo.Commit(git.CommitRequest{Message: InitCommitMessage, Paths: paths, RequireChange: true}); err != nil {
		return InitResult{}, fmt.Errorf("%s was written and could not be committed: %w. Commit it with `safegit commit -m %q -- %s`", rel, err, InitCommitMessage, strings.Join(paths, " "))
	}
	return InitResult{Path: rel, Written: true}, nil
}

// releasableTargets are the names of the targets of the releasable's
// members, each once, in the order the members and their targets come.
func releasableTargets(ws *workspace.Workspace, r declarations.Releasable) ([]string, error) {
	var names []string
	for _, m := range sortedMembers(ws.MembersOf(r.Name)) {
		ts, err := targets.MemberTargets(ws.Root, m)
		if err != nil {
			return nil, err
		}
		for _, t := range ts {
			if !containsString(names, t.Name) {
				names = append(names, t.Name)
			}
		}
	}
	return names, nil
}

func containsString(list []string, item string) bool {
	for _, l := range list {
		if l == item {
			return true
		}
	}
	return false
}

// scaffoldedReleaseFile is a release file with bump and description blank,
// which a release refuses until somebody fills them in.
func scaffoldedReleaseFile(releasable string, include []string) string {
	quoted := make([]string, len(include))
	for i, t := range include {
		quoted[i] = fmt.Sprintf("%q", t)
	}
	return strings.Join([]string{
		"# The next release of " + releasable + ". Fill in bump and description, then run `rlsbl release run`.",
		"format_version = 2",
		"# How the release moves the version: patch, minor, major, or infra (moved as a patch, recorded as infra).",
		`bump = ""`,
		"# What this release is, in a sentence or two. Required.",
		`description = ""`,
		"# Why these changes were made. Optional.",
		`context = ""`,
		"# The targets released, and the ones left out; between them they name every target of the releasable's members.",
		"include = [" + strings.Join(quoted, ", ") + "]",
		"exclude = []",
		"",
	}, "\n")
}
