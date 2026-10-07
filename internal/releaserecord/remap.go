package releaserecord

import (
	"fmt"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/semver"
)

// A history rewrite replaces the commits the archives' release commits name.
// Moving them is narrow on purpose: a release commit the rewrite's map names
// moves to the new commit, one it does not name stays as it is, and an
// unrecoverable or never-released archive has no commit to move.
//
// Each recorded tree is computed again at the new commit. A rewrite that
// only re-parents commits leaves every tree the same, and the version did
// ship that content from a commit with a new name. A rewrite that
// redacted something under a released path did not, and recording the
// archive at it would claim content the release never shipped, so by
// default a changed tree refuses the whole move. `release scrub` declares
// the other answer: it rewrites the changelog files inside every released
// tree, so every tree changes by construction, and it records the new trees
// and prints each changed path. Which answer applies is the caller's
// declared choice, never inferred from what is observed.
//
// Every archive is planned and verified before any is written, so a refusal
// on one leaves every archive as it was.

// ContentChange is what a caller declares a released tree that differs at
// the rewritten commit means.
type ContentChange string

// The two answers.
const (
	// ContentChangeRefuse refuses the move, naming the version and both
	// trees.
	ContentChangeRefuse ContentChange = "refuse"
	// ContentChangeRecord records the new tree and reports the change.
	ContentChangeRecord ContentChange = "record"
)

// TreeChange is one released path whose tree differs at the new commit.
type TreeChange struct {
	Path     string
	Recorded string
	Found    string
}

// CommitRemap is one archive's release commit, moved.
type CommitRemap struct {
	// Dir is the repository-relative archive directory.
	Dir       string
	Version   semver.Version
	Path      string
	OldCommit string
	NewCommit string
	// Trees is what the rewritten archive records.
	Trees map[string]string
	// Changed names the released paths whose tree differs at the new
	// commit; empty whenever the content was the same.
	Changed []TreeChange
}

// mapCommit is the rewritten commit of sha, and empty when the map does not
// name it. A stored commit may be abbreviated: it maps when it is a key, or
// the prefix of one key; the prefix of several is refused.
func mapCommit(sha string, commitMap map[string]string) (string, error) {
	if to, ok := commitMap[sha]; ok {
		return to, nil
	}
	var matches []string
	for from := range commitMap {
		if strings.HasPrefix(from, sha) {
			matches = append(matches, from)
		}
	}
	switch len(matches) {
	case 0:
		return "", nil
	case 1:
		return commitMap[matches[0]], nil
	}
	sort.Strings(matches)
	return "", fmt.Errorf("the release commit %s is the prefix of more than one commit in the rewrite map (%s), so which commit it moved to cannot be decided; record the full commit id in the archive and re-run", sha, strings.Join(matches, ", "))
}

// PlanRemap plans the move of every release commit the archives in dir
// (repository-relative) record through commitMap, verified and not written.
// repo reads the trees at the new commits.
func PlanRemap(repo git.Repo, dir string, commitMap map[string]string, onChange ContentChange) ([]CommitRemap, error) {
	if onChange != ContentChangeRefuse && onChange != ContentChangeRecord {
		return nil, fmt.Errorf("a content change is answered %q or %q, not %q", ContentChangeRefuse, ContentChangeRecord, onChange)
	}
	if len(commitMap) == 0 {
		return nil, nil
	}
	root := repo.Dir()
	versions, err := ArchivedVersions(root, dir)
	if err != nil {
		return nil, err
	}
	var planned []CommitRemap
	for _, v := range versions {
		a, err := ReadArchive(root, dir, v)
		if err != nil {
			return nil, err
		}
		if a.Fate != FateRecorded {
			// The commitless fates have nothing to move; an archive stating no
			// fate is the record's no-fate error, which reads report.
			continue
		}
		old := a.ReleaseCommit.Commit
		next, err := mapCommit(old, commitMap)
		if err != nil {
			return nil, err
		}
		if next == "" || next == old {
			continue
		}
		remap := CommitRemap{Dir: dir, Version: v, Path: a.Path, OldCommit: old, NewCommit: next, Trees: map[string]string{}}
		paths := make([]string, 0, len(a.ReleaseCommit.Trees))
		for p := range a.ReleaseCommit.Trees {
			paths = append(paths, p)
		}
		sort.Strings(paths)
		for _, p := range paths {
			recorded := a.ReleaseCommit.Trees[p]
			found, ok, err := repo.TreeAt(next, p)
			if err != nil {
				return nil, err
			}
			if !ok {
				return nil, fmt.Errorf("the released path %q of %s has no tree at the rewritten commit %s (%s); the rewritten commit must carry every released path before its release commit can be recorded", p, v, next, git.TreeRevSpec(next, p))
			}
			if found != recorded {
				if onChange == ContentChangeRefuse {
					return nil, fmt.Errorf("the release commit of %s cannot be moved through the rewrite: the content it recorded is not the content the rewritten commit carries.\n"+
						"  released path:        %s\n"+
						"  recorded tree:        %s\n"+
						"  tree at %s: %s\n"+
						"  release commit moved: %s -> %s\n"+
						"  archive:              %s\n"+
						"  The archive states what %s shipped. Recording it at a commit whose tree differs\n"+
						"  would claim content that was never released, which is what a rewrite that\n"+
						"  redacted a released file produces. Nothing was written. Decide what the record\n"+
						"  should say (the shipped artifact is unchanged on the registry; only the history\n"+
						"  moved), then record the version unrecoverable or rewrite with the new trees.",
						v, p, recorded, short(next), found, short(old), short(next), a.Path, v)
				}
				remap.Changed = append(remap.Changed, TreeChange{Path: p, Recorded: recorded, Found: found})
			}
			remap.Trees[p] = found
		}
		planned = append(planned, remap)
	}
	return planned, nil
}

func short(sha string) string {
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}

// RemapTarget is one archive directory a repair moves, with the releasable
// or retired subject it belongs to.
type RemapTarget struct {
	Dir string
	// Subject is the releasable or retired subject, named on the
	// release-commit-remap event.
	Subject string
}

// RemapTargets lists every archive directory of the repository at root that
// holds an archive: each releasable's, and each retired subject's, whose
// record of what it released names commits a rewrite moves too.
func RemapTargets(root string) ([]RemapTarget, error) {
	dirs, err := ArchiveDirs(root)
	if err != nil {
		return nil, err
	}
	targets := make([]RemapTarget, len(dirs))
	for i, dir := range dirs {
		subject := path.Base(dir)
		if path.Base(dir) == "releases" {
			subject = path.Base(path.Dir(dir))
		}
		targets[i] = RemapTarget{Dir: dir, Subject: subject}
	}
	return targets, nil
}

// RepairReleaseCommits moves every release commit the repository's archives
// record through commitMap and appends one release-commit-remap event per
// archive directory that moved, naming rewrite (what performed the rewrite,
// in the writer's words) and stamped at now. Every directory is planned and
// verified before the first write, so a refusal in one leaves every archive
// as it was. It returns the moves and the repository-relative paths a commit
// must carry: the rewritten archives and the transition record.
func RepairReleaseCommits(e *strictcli.Effects, repo git.Repo, commitMap map[string]string, rewrite string, onChange ContentChange, now time.Time) ([]CommitRemap, []string, error) {
	root := repo.Dir()
	targets, err := RemapTargets(root)
	if err != nil {
		return nil, nil, err
	}
	type plannedDir struct {
		target RemapTarget
		remaps []CommitRemap
	}
	var plans []plannedDir
	for _, t := range targets {
		remaps, err := PlanRemap(repo, t.Dir, commitMap, onChange)
		if err != nil {
			return nil, nil, err
		}
		if len(remaps) > 0 {
			plans = append(plans, plannedDir{t, remaps})
		}
	}
	var all []CommitRemap
	var touched []string
	var events []Event
	for _, p := range plans {
		ev := &ReleaseCommitRemapEvent{Releasable: p.target.Subject, Rewrite: rewrite}
		for _, r := range p.remaps {
			if err := WriteReleaseCommit(e, root, r.Dir, r.Version, ReleaseCommit{Commit: r.NewCommit, Trees: r.Trees}); err != nil {
				return nil, nil, err
			}
			touched = append(touched, r.Path)
			ev.Mappings = append(ev.Mappings, CommitMapping{OldSHA: r.OldCommit, NewSHA: r.NewCommit})
		}
		all = append(all, p.remaps...)
		events = append(events, ev)
	}
	if len(events) > 0 {
		if err := AppendEvents(e, root, events, now); err != nil {
			return nil, nil, err
		}
		touched = append(touched, declarations.TransitionsFile)
	}
	return all, touched, nil
}
