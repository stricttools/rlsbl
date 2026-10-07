package changelog

import (
	"fmt"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
)

// Pushed is the commits a push sends, as the pre-push hook sees it.
type Pushed struct {
	// Commits are the commits the push sends that the remote's old commits
	// do not reach, newest first per ref, each once.
	Commits []string
	// Rewritten are the refs whose old remote commit does not resolve here:
	// a history rewrite replaced it, so the range from it cannot be
	// computed. Their commits are not in Commits, and the caller says so.
	Rewritten []git.PushedRef
}

// PushedCommits are the commits refs push. An updated ref sends what its
// new commit reaches and the remote's old commit does not; a new ref sends
// what no remote-tracking ref reaches; a deletion sends nothing. exclude are
// revisions whose history is not the repository's own (a fork's upstream
// branch and inherited tags), left out of every range.
func PushedCommits(repo git.Repo, refs []git.PushedRef, exclude []string) (Pushed, error) {
	var out Pushed
	seen := map[string]bool{}
	for _, ref := range refs {
		if git.IsNullObjectID(ref.LocalID) {
			continue
		}
		var stop []string
		if git.IsNullObjectID(ref.RemoteID) {
			stop = append([]string{"--remotes"}, exclude...)
		} else {
			_, found, err := repo.ResolveCommit(ref.RemoteID)
			if err != nil {
				return Pushed{}, err
			}
			if !found {
				out.Rewritten = append(out.Rewritten, ref)
				continue
			}
			stop = append([]string{ref.RemoteID}, exclude...)
		}
		commits, err := repo.Commits([]string{ref.LocalID}, stop)
		if err != nil {
			return Pushed{}, err
		}
		for _, c := range commits {
			if !seen[c] {
				seen[c] = true
				out.Commits = append(out.Commits, c)
			}
		}
	}
	return out, nil
}

// PushCoverage are the pushed commits needing an entry that no entry of
// files names, as 12-character ids in the order given. Exempt commits need
// none. Every changelog file counts, the released versions' included, so a
// commit a release already described is covered.
func PushCoverage(repo git.Repo, files []*File, pushed []string) ([]string, error) {
	needing, _, err := FilterExempt(repo, pushed)
	if err != nil {
		return nil, err
	}
	res := newResolver(repo)
	covered := map[string]bool{}
	for _, f := range files {
		for _, l := range f.Lines {
			for _, h := range l.Entry.Commits {
				full, err := res.resolve(h)
				if err != nil {
					return nil, err
				}
				if full != "" {
					covered[full] = true
				}
			}
		}
	}
	var missing []string
	for _, c := range needing {
		if !covered[c] {
			missing = append(missing, short(c))
		}
	}
	return missing, nil
}

// TrackedRecords are the repository-relative paths of a releasable's
// changelog records that git must track: its unreleased file and its
// CHANGELOG.md, and in a workspace the roll-up.
func TrackedRecords(d *declarations.Releasables, releasable string) []string {
	paths := []string{Dir(releasable) + "/" + UnreleasedName, Home(d, releasable)}
	if d.IsWorkspace() {
		paths = append(paths, RollUpPath)
	}
	return paths
}

// IgnoredPaths are the paths of paths (repository-relative) that the
// repository's ignore rules match, asked of git check-ignore through r,
// in the order given.
func IgnoredPaths(r git.Runner, root string, paths []string) ([]string, error) {
	var ignored []string
	for _, p := range paths {
		c, err := r.Run([]interface{}{"git", "check-ignore", "-q", "--", p}, strictcli.Cwd(root), strictcli.Check(false))
		if err != nil {
			return nil, fmt.Errorf("git check-ignore %s in %s: %w", p, root, err)
		}
		switch c.ExitCode() {
		case 0:
			ignored = append(ignored, p)
		case 1:
		default:
			return nil, fmt.Errorf("git check-ignore %s in %s exited %d: %s", p, root, c.ExitCode(), strings.TrimSpace(c.Stderr()))
		}
	}
	return ignored, nil
}

// IgnoredRecordsFinding words ignored changelog records, or is empty when
// there are none.
func IgnoredRecordsFinding(root string, ignored []string) string {
	if len(ignored) == 0 {
		return ""
	}
	return fmt.Sprintf("rlsbl's changelog records are ignored by git in %s, so they would never be committed: %s. Change the ignore rule that matches them (`git check-ignore -v <path>` names it)", filepath.Clean(root), strings.Join(ignored, ", "))
}
