package release

import (
	"path/filepath"
	"sort"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// WriteScope are the repository-relative paths a release of the releasable
// writes, as far as they can be known before it runs: the releasable's state
// (its changelog, release, validation-cache, and run-state paths), its
// generated CHANGELOG.md (and a workspace's roll-up), the scaffold state,
// each member's selfdoc.json, and the version files and lockfiles of every
// target of its members. What a producer generates (docs, the schema dump)
// cannot be named before it runs; Advance refuses the same way for every
// path the release's commits write.
func WriteScope(w *workspace.Workspace, releasable string) ([]string, error) {
	scope := map[string]bool{}
	for _, d := range workspace.StateDirs(releasable) {
		scope[d] = true
	}
	scope[changelog.Home(w.Declarations, releasable)] = true
	if w.IsWorkspace() {
		scope[changelog.RollUpPath] = true
	}
	scope[declarations.ScaffoldStateFile] = true
	if err := addMemberScope(w, w.MembersOf(releasable), scope); err != nil {
		return nil, err
	}
	return sortedPaths(scope), nil
}

// WorkspaceWriteScope are the paths a batch release of a workspace writes:
// every releasable's scope, the batch release files and run state, and the
// root's CHANGELOG.md and selfdoc.json.
func WorkspaceWriteScope(w *workspace.Workspace) ([]string, error) {
	scope := map[string]bool{
		declarations.ChangelogRoot:     true,
		declarations.ReleasesRoot:      true,
		declarations.BatchReleasesDir:  true,
		declarations.ScaffoldStateFile: true,
		changelog.RollUpPath:           true,
		selfdocFile:                    true,
	}
	for _, r := range w.Releasables() {
		for _, d := range workspace.StateDirs(r.Name) {
			scope[d] = true
		}
	}
	if err := addMemberScope(w, w.Members(), scope); err != nil {
		return nil, err
	}
	return sortedPaths(scope), nil
}

// addMemberScope adds each member's selfdoc.json and its targets' version
// files and lockfiles.
func addMemberScope(w *workspace.Workspace, members []declarations.Member, scope map[string]bool) error {
	for _, m := range members {
		scope[declarations.Join(m.Path, selfdocFile)] = true
		ts, err := targets.MemberTargets(w.Root, m)
		if err != nil {
			return err
		}
		for _, t := range ts {
			target, err := targets.Get(t.Name)
			if err != nil {
				return err
			}
			dir := m.TargetDir(t)
			for _, f := range target.Facts().VersionFiles {
				scope[declarations.Join(dir, f)] = true
			}
			for _, s := range lockfileSpecs {
				scope[declarations.Join(dir, s.name)] = true
			}
		}
	}
	return nil
}

func sortedPaths(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for p := range set {
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}

// EnterOptions declare how a release takes the repository.
type EnterOptions struct {
	DryRun bool
	// What names the release in a refusal ("The release").
	What string
	// Rerun is what a refusal tells the operator to run once the cause is
	// dealt with ("run `rlsbl release run` again").
	Rerun string
	// OnWait is told that another rlsbl process holds the lock, before the
	// release waits for it.
	OnWait func(path string)
}

// Session is a release's hold on the repository: the lock, and the release
// checkout it runs in.
type Session struct {
	// Checkout is the release checkout; nil under --dry-run, where the
	// release previews the working tree as it stands.
	Checkout *Checkout
	// Root is where the release reads and writes committed files: the
	// release checkout, or the working tree under --dry-run.
	Root string
	// LiveRoot is the working tree, where run state, the environment file,
	// and the dev overlays live.
	LiveRoot string
	// Blocking are the uncommitted changes that would refuse the release; a
	// preview reports them instead of refusing.
	Blocking []git.Change
	// Ignored are the uncommitted changes the release leaves alone.
	Ignored []git.Change
	lock    *runstate.Lock
}

// Enter takes the repository for a release writing scope (WriteScope or
// WorkspaceWriteScope): it looks at the working tree once, refuses when a
// path in scope has uncommitted changes (naming every one), takes the
// release lock, and prepares the release checkout of the release branch's
// tip. Every other uncommitted change is listed in Ignored and left alone.
// Under --dry-run nothing refuses and no checkout is entered: Blocking
// holds what would refuse, and the release previews the working tree.
// Close gives the lock back.
func Enter(e *strictcli.Effects, live git.Repo, scope []string, o EnterOptions) (*Session, error) {
	liveRoot := resolvePath(live.Dir())
	s := &Session{Root: liveRoot, LiveRoot: liveRoot}
	if o.DryRun {
		changes, err := LiveChanges(live)
		if err != nil {
			return nil, err
		}
		s.Blocking, s.Ignored = PartitionChanges(changes, scope)
		return s, nil
	}
	lock, err := runstate.Acquire(e, liveRoot, runstate.AcquireOptions{Wait: runstate.WaitForHolder, OnWait: o.OnWait})
	if err != nil {
		return nil, err
	}
	s.lock = lock
	fail := func(err error) (*Session, error) {
		if releaseErr := lock.Release(); releaseErr != nil {
			return nil, releaseErr
		}
		return nil, err
	}
	changes, err := LiveChanges(live)
	if err != nil {
		return fail(err)
	}
	s.Blocking, s.Ignored = PartitionChanges(changes, scope)
	if len(s.Blocking) > 0 {
		return fail(&LiveTreeConflictError{Message: ConflictMessage(s.Blocking, o.What, o.Rerun)})
	}
	branch, err := LiveBranch(live)
	if err != nil {
		return fail(err)
	}
	sha, found, err := live.ResolveCommit("refs/heads/" + branch)
	if err != nil {
		return fail(err)
	}
	if !found {
		return fail(&BranchMovedError{Message: "the branch " + branch + " has no commit to release"})
	}
	co, err := EnterCheckout(e, live, branch, sha)
	if err != nil {
		return fail(err)
	}
	s.Checkout = co
	s.Root = co.Path
	return s, nil
}

// ToLive is the working tree's counterpart of a path under the session's
// root.
func (s *Session) ToLive(path string) string {
	if s.Checkout == nil {
		return path
	}
	return s.Checkout.ToLive(path)
}

// LivePath is the absolute working-tree path of a repository-relative path.
func (s *Session) LivePath(rel string) string {
	return filepath.Join(s.LiveRoot, filepath.FromSlash(rel))
}

// Close gives the lock back; a session under --dry-run holds none.
func (s *Session) Close() error {
	if s.lock == nil {
		return nil
	}
	err := s.lock.Release()
	s.lock = nil
	return err
}
