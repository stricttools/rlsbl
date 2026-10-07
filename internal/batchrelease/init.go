package batchrelease

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// InitCommitMessage is the subject of the commit `rlsbl monorepo release
// init` makes.
const InitCommitMessage = "release: scaffold the batch release file"

// InitResult is what `rlsbl monorepo release init` did.
type InitResult struct {
	// Path is the batch release file, repository-relative.
	Path string
	// Written is false when a batch release file nobody filled in was there
	// already, so nothing was written.
	Written bool
	// Releasables are the releasables given a table to fill in, and Idle
	// those written commented out, with no unreleased commit since their
	// latest release.
	Releasables []string
	Idle        []string
}

// Init is `rlsbl monorepo release init`: it writes the batch release file
// of the workspace whose root is liveRoot, one [releasables.<name>] table
// per releasable (the ones names lists, or every declared one when names is
// empty) with bump and description blank, context blank, every target of
// the releasable's members in include, and exclude empty, and commits it. A
// releasable with no unreleased commit since its latest release is written
// commented out. A batch release file nobody filled in is left as it is;
// one somebody filled in is refused, never overwritten. fork leaves a
// fork's upstream history out of the unreleased commits.
func Init(e *strictcli.Effects, liveRoot string, names []string, fork release.Fork) (InitResult, error) {
	ws, err := workspace.Load(liveRoot)
	if err != nil {
		return InitResult{}, err
	}
	if err := requireWorkspace(ws, "init"); err != nil {
		return InitResult{}, err
	}
	rel := releaserecord.BatchReleaseFilePath
	abs := filepath.Join(liveRoot, filepath.FromSlash(rel))
	existing, err := os.ReadFile(abs)
	switch {
	case err == nil:
		if releaserecord.IsPristineBatchReleaseFile(existing) {
			return InitResult{Path: rel}, nil
		}
		return InitResult{}, fmt.Errorf("%s exists and somebody filled it in, so it is not overwritten; edit it, or delete it (saferm delete --on-error abort --description \"a batch release file to scaffold again\" %s) and run `rlsbl monorepo release init` again", rel, abs)
	case !errors.Is(err, fs.ErrNotExist):
		return InitResult{}, err
	}
	selected, err := selectReleasables(ws, names)
	if err != nil {
		return InitResult{}, err
	}
	repo, err := git.Open(e, liveRoot)
	if err != nil {
		return InitResult{}, err
	}
	res := InitResult{Path: rel}
	var tables, idle []string
	for _, r := range selected {
		include, err := release.ReleasableTargets(ws, r)
		if err != nil {
			return InitResult{}, err
		}
		if len(include) == 0 {
			return InitResult{}, fmt.Errorf("no target (go, npm, or pypi) is declared or detected in any member of %s, so its table would include nothing; add a manifest (go.mod, package.json, pyproject.toml) or declare the members' targets in %s, or leave %s out by naming the other releasables with --releasables, then run `rlsbl monorepo release init` again", r.Name, declarations.ReleasablesFile, r.Name)
		}
		progress, err := release.ReadProgress(repo, ws, r.Name, fork)
		if err != nil {
			return InitResult{}, err
		}
		if progress.Coverage.Total == 0 && progress.Latest.Released {
			idle = append(idle, idleTable(r.Name, include, progress.Latest.Version.String()))
			res.Idle = append(res.Idle, r.Name)
			continue
		}
		tables = append(tables, scaffoldedTable(r.Name, include))
		res.Releasables = append(res.Releasables, r.Name)
	}
	if len(tables) == 0 {
		return InitResult{}, fmt.Errorf("no commit needing a changelog entry was made since the latest release of %s, so a batch release would release nothing; commit the next changes first, then run `rlsbl monorepo release init` again", strings.Join(res.Idle, ", "))
	}
	manifest := path.Join(declarations.BatchReleasesDir, "manifest.toml")
	manifestTracked, err := repo.Tracks(manifest)
	if err != nil {
		return InitResult{}, err
	}
	if err := declarations.EnsureOwnedDirectory(e, liveRoot, declarations.BatchReleasesDir); err != nil {
		return InitResult{}, err
	}
	if _, err := e.Write(abs, []byte(scaffoldedBatchFile(tables, idle)), strictcli.Mode(0o644)); err != nil {
		return InitResult{}, err
	}
	paths := []string{rel}
	if !manifestTracked {
		// The first write into the batch releases directory created its
		// ownership manifest, which the commit carries.
		paths = append(paths, manifest)
	}
	if _, err := repo.Commit(git.CommitRequest{Message: InitCommitMessage, Paths: paths, RequireChange: true}); err != nil {
		return InitResult{}, fmt.Errorf("%s was written and could not be committed: %w. Commit it with `safegit commit -m %q -- %s`", rel, err, InitCommitMessage, strings.Join(paths, " "))
	}
	res.Written = true
	return res, nil
}

// selectReleasables are the releasables names lists, in declaration order,
// or every declared one when names is empty. A name no releasable has is
// refused, naming the declared ones.
func selectReleasables(ws *workspace.Workspace, names []string) ([]declarations.Releasable, error) {
	if len(names) == 0 {
		return ws.Releasables(), nil
	}
	wanted := map[string]bool{}
	var unknown []string
	for _, n := range names {
		if _, ok := ws.Declarations.Releasable(n); !ok {
			unknown = append(unknown, n)
		}
		wanted[n] = true
	}
	if len(unknown) > 0 {
		return nil, fmt.Errorf("--releasables names %s, which %s does not declare; the declared releasables are %s (pass --releasables once per releasable)", strings.Join(unknown, ", "), declarations.ReleasablesFile, strings.Join(declaredNames(ws), ", "))
	}
	var out []declarations.Releasable
	for _, r := range ws.Releasables() {
		if wanted[r.Name] {
			out = append(out, r)
		}
	}
	return out, nil
}

func quotedList(items []string) string {
	quoted := make([]string, len(items))
	for i, t := range items {
		quoted[i] = tomledit.QuoteString(t)
	}
	return "[" + strings.Join(quoted, ", ") + "]"
}

// scaffoldedTable is a releasable's table with bump and description blank,
// which a batch release refuses until somebody fills them in.
func scaffoldedTable(releasable string, include []string) string {
	return strings.Join([]string{
		"[releasables." + tomledit.QuoteKey(releasable) + "]",
		"# How the release moves the version: patch, minor, major, or infra (moved as a patch, recorded as infra).",
		`bump = ""`,
		"# What this release is, in a sentence or two. Required.",
		`description = ""`,
		"# Why these changes were made. Optional.",
		`context = ""`,
		"# The targets released, and the ones left out; between them they name every target of the releasable's members.",
		"include = " + quotedList(include),
		"exclude = []",
	}, "\n")
}

// idleTable is a releasable's table commented out, for a releasable with no
// unreleased commit since its latest release.
func idleTable(releasable string, include []string, latest string) string {
	lines := []string{"# " + releasable + ": no commit needing a changelog entry since " + latest + "; uncomment the table to release it anyway."}
	for _, line := range strings.Split(scaffoldedTable(releasable, include), "\n") {
		if strings.HasPrefix(line, "# ") {
			continue
		}
		lines = append(lines, "# "+line)
	}
	return strings.Join(lines, "\n")
}

// scaffoldedBatchFile is a batch release file holding tables and, after
// them, the idle tables commented out.
func scaffoldedBatchFile(tables, idle []string) string {
	parts := []string{
		"# The next batch release of this workspace, one table per releasable. Fill in each table's bump and description, then run `" + runstate.BatchRunInvocation + "`.\nformat_version = 2",
	}
	parts = append(parts, tables...)
	parts = append(parts, idle...)
	return strings.Join(parts, "\n\n") + "\n"
}
