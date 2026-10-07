package releaseops

import (
	"fmt"
	"path/filepath"
	"sort"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
)

// A revert is computed whole before anything is written: every file the
// reverted commits touched gets the content it has once each of them is
// undone, newest first, the way `git revert` would merge it, through git's
// own three-way merge. A file that would conflict refuses the undo before
// anything is destroyed, so a revert can never stop mid-conflict with the
// tag and the GitHub Release already gone. The result is written and
// committed as one commit through safegit, like every other commit rlsbl
// makes in the working tree.

// fileState is one file's content, or its absence.
type fileState struct {
	content string
	exists  bool
}

func (f fileState) same(g fileState) bool {
	return f.exists == g.exists && (!f.exists || f.content == g.content)
}

// revertPlan is the files the reverted commits touched, with the content
// each has once they are reverted.
type revertPlan struct {
	// commits are the reverted commits, newest first.
	commits []git.CommitSubject
	files   map[string]fileState
}

// paths are the files the revert writes or removes, sorted.
func (p revertPlan) paths() []string {
	out := make([]string, 0, len(p.files))
	for path := range p.files {
		out = append(out, path)
	}
	sort.Strings(out)
	return out
}

func fileAt(repo git.Repo, rev, path string) (fileState, error) {
	content, found, err := repo.FileAt(rev, path)
	if err != nil {
		return fileState{}, err
	}
	return fileState{content: content, exists: found}, nil
}

// planRevert computes the revert of commits (newest first) on top of HEAD.
func planRevert(repo git.Repo, commits []git.CommitSubject) (revertPlan, error) {
	plan := revertPlan{commits: commits, files: map[string]fileState{}}
	for _, c := range commits {
		paths, err := repo.CommitFiles(c.SHA)
		if err != nil {
			return revertPlan{}, err
		}
		for _, path := range paths {
			ours, seen := plan.files[path]
			if !seen {
				if ours, err = fileAt(repo, "HEAD", path); err != nil {
					return revertPlan{}, err
				}
			}
			after, err := fileAt(repo, c.SHA, path)
			if err != nil {
				return revertPlan{}, err
			}
			before, err := fileAt(repo, c.SHA+"^", path)
			if err != nil {
				return revertPlan{}, err
			}
			result, err := revertFile(repo, c, path, ours, after, before)
			if err != nil {
				return revertPlan{}, err
			}
			plan.files[path] = result
		}
	}
	return plan, nil
}

// revertFile undoes one commit's change to path (after is what the commit
// left, before what it found) on top of ours, the file as it stands.
func revertFile(repo git.Repo, c git.CommitSubject, path string, ours, after, before fileState) (fileState, error) {
	if ours.same(after) {
		return before, nil
	}
	if !ours.exists || !after.exists || !before.exists {
		return fileState{}, revertConflict(c, path, "the commit created or deleted it, and later work changed it since")
	}
	merged, conflicts, err := repo.ThreeWayMerge(ours.content, after.content, before.content)
	if err != nil {
		return fileState{}, err
	}
	if conflicts > 0 {
		return fileState{}, revertConflict(c, path, fmt.Sprintf("later work changed the same lines (%d conflicting hunks)", conflicts))
	}
	return fileState{content: merged, exists: true}, nil
}

func revertConflict(c git.CommitSubject, path, why string) error {
	return fmt.Errorf("reverting %s (%q) conflicts in %s: %s. Undo refused: nothing was destroyed, and nothing was written. Revert that later work's change to %s yourself (or move it aside), commit, and run the undo again", c.SHA[:12], c.Subject, path, why, path)
}

// write writes the planned contents and removes the files the revert
// deletes, through the effects handle.
func (p revertPlan) write(e *strictcli.Effects, s Selection) error {
	for _, path := range p.paths() {
		f := p.files[path]
		abs := s.abs(path)
		if !f.exists {
			if _, err := e.Remove(abs); err != nil {
				return fmt.Errorf("removing %s: %w", path, err)
			}
			continue
		}
		if _, err := e.Mkdir(filepath.Dir(abs)); err != nil {
			return fmt.Errorf("creating the directory of %s: %w", path, err)
		}
		// Through a renamed temporary file, so a read-only file (a released
		// changelog file, an archive) is replaced like any other.
		tmp := abs + ".rlsbl-writing"
		if _, err := e.Write(tmp, f.content); err != nil {
			return fmt.Errorf("writing %s: %w", path, err)
		}
		if _, err := e.Rename(tmp, abs); err != nil {
			return fmt.Errorf("writing %s: %w", path, err)
		}
	}
	return nil
}
