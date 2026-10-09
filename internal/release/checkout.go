package release

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
)

// The release checkout is the detached checkout of the committed commit a
// release runs in. Everything a release does before its first push (the
// pre-release pipeline, the hooks, the tests and checks, the version writes,
// the build, and the release commit) happens there, never in the working
// tree the operator or another session edits. It is one git worktree of the
// repository, kept under the repository's git common directory and reused
// from release to release: each release resets it to the commit it starts
// from and removes its untracked files, keeping ignored ones, so dependency
// environments and build caches stay warm.
//
// The checkout's commits reach the release branch through Advance:
// the branch moves only by compare-and-swap from the commit this release
// last left it at, and only the files the release's own commits change are
// written into the working tree, and only while every one of them is clean
// there. Every other file of the working tree is never read and never
// written, so another session's uncommitted work neither blocks a release
// nor leaks into it.

// CheckoutRelative is the release checkout's location relative to the
// repository's git common directory. A history rewrite removes the checkout
// there, since its detached HEAD keeps the old history reachable.
const CheckoutRelative = "rlsbl/release-checkout"

// ReleaseBinRelative is the release's own directory for binaries, relative
// to the git common directory.
const ReleaseBinRelative = "rlsbl/release-bin"

// ReleaseBinEnv names the release's directory for binaries to its hooks: a
// hook builds into it a tool the release must run in its unreleased form,
// without installing that build for every session on the machine.
const ReleaseBinEnv = "RLSBL_RELEASE_BIN"

// BranchMovedError is the refusal of an advance whose branch is not where
// this release last left it.
type BranchMovedError struct{ Message string }

func (e *BranchMovedError) Error() string { return e.Message }

// LiveTreeConflictError is the refusal of an advance, or of a release's
// start, whose written paths have uncommitted changes in the working tree.
type LiveTreeConflictError struct{ Message string }

func (e *LiveTreeConflictError) Error() string { return e.Message }

// Checkout is one release's checkout and the working tree it releases.
type Checkout struct {
	// LiveRoot is the working tree's root, symbolic links resolved.
	LiveRoot string
	// Path is the checkout's root, symbolic links resolved.
	Path string
	// Branch is the release branch the checkout's commits advance.
	Branch string
	// Tip is the commit the branch is expected at: the commit the release
	// started from, then each commit Advance moved it to.
	Tip string
	// ReleaseBin is the release's directory for binaries.
	ReleaseBin string
	// env is what every process of the release gets.
	env  map[string]string
	e    *strictcli.Effects
	live git.Repo
	repo git.Repo
	// last is the most recent advance, for UnwindLastAdvance; nil once
	// unwound or before any advance.
	last *advance
}

// advance is one move of the branch and the paths it wrote.
type advance struct {
	old, new string
	paths    []string
}

// Repo is the checkout's repository.
func (c *Checkout) Repo() git.Repo { return c.repo }

// LiveRepo is the working tree's repository.
func (c *Checkout) LiveRepo() git.Repo { return c.live }

// BranchRef is the release branch's full ref. Inside the checkout HEAD is
// detached and carries only the release's own commits, so a question about
// what reached the branch names the ref every worktree shares.
func (c *Checkout) BranchRef() string { return "refs/heads/" + c.Branch }

// Environment is what every process of the release is started with: GOWORK
// (off when the checkout commits no go.work, so a Go command never finds the
// working tree's uncommitted go.work in a parent directory, and empty when it
// does, so a Go command in the checkout finds the checkout's own go.work and
// one in another directory, such as a test's throwaway module, finds none),
// RLSBL_RELEASE_BIN, and PATH with the release's directory for binaries
// first, so a tool a hook built there is the one later steps run.
func (c *Checkout) Environment() map[string]string {
	out := make(map[string]string, len(c.env))
	for k, v := range c.env {
		out[k] = v
	}
	return out
}

// Owns reports whether the absolute path lies inside the checkout.
func (c *Checkout) Owns(path string) bool {
	return within(resolvePath(path), c.Path)
}

// ToLive is the working tree's counterpart of a path inside the checkout;
// any other path comes back unchanged. Run state, the environment file,
// and the dev overlays belong to the working tree, not to the committed
// commit, so their readers go through here.
func (c *Checkout) ToLive(path string) string {
	resolved := resolvePath(path)
	if !within(resolved, c.Path) {
		return path
	}
	rel, _ := filepath.Rel(c.Path, resolved)
	return filepath.Join(c.LiveRoot, rel)
}

// ToCheckout is the checkout's counterpart of a path inside the working
// tree; a path outside it is refused.
func (c *Checkout) ToCheckout(path string) (string, error) {
	resolved := resolvePath(path)
	if !within(resolved, c.LiveRoot) {
		return "", fmt.Errorf("%s is outside the repository at %s; the release checkout holds nothing for it", path, c.LiveRoot)
	}
	rel, _ := filepath.Rel(c.LiveRoot, resolved)
	return filepath.Join(c.Path, rel), nil
}

// resolvePath is path with symbolic links resolved where it exists, and the
// cleaned path where it does not.
func resolvePath(path string) string {
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		return resolved
	}
	return filepath.Clean(path)
}

// within reports whether path is dir or lies under it.
func within(path, dir string) bool {
	return path == dir || strings.HasPrefix(path, dir+string(filepath.Separator))
}

// CheckoutDir is where the release checkout of the repository lives.
func CheckoutDir(live git.Repo) (string, error) {
	common, err := live.CommonDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(resolvePath(common), filepath.FromSlash(CheckoutRelative)), nil
}

// isOwnCheckout reports whether path is a registered worktree of the
// repository: the release checkout rlsbl created.
func isOwnCheckout(live git.Repo, path string) (bool, error) {
	worktrees, err := live.Worktrees()
	if err != nil {
		return false, err
	}
	resolved := resolvePath(path)
	for _, w := range worktrees {
		if resolvePath(w) == resolved {
			return true, nil
		}
	}
	return false, nil
}

// PrepareCheckout puts the release checkout at commit sha, clean, and
// returns its path. The first release creates it; later ones reset it and
// remove its untracked files, keeping ignored ones. Submodules are
// initialized when the commit declares any. A directory at the checkout's
// path that is not this repository's checkout is refused, naming it: rlsbl
// deletes nothing it did not create.
func PrepareCheckout(e *strictcli.Effects, live git.Repo, sha string) (string, error) {
	path, err := CheckoutDir(live)
	if err != nil {
		return "", err
	}
	_, statErr := os.Lstat(path)
	switch {
	case statErr == nil:
		own, err := isOwnCheckout(live, path)
		if err != nil {
			return "", err
		}
		if !own {
			return "", fmt.Errorf("%s exists but is not this repository's release checkout (a git worktree of %s); rlsbl creates the release checkout there and will not replace a directory it did not create. Delete %s (saferm delete --on-error abort --description \"not the release checkout\" %s) and run the release again", path, live.Dir(), path, path)
		}
		co, err := git.Open(e, path)
		if err != nil {
			return "", err
		}
		if err := co.ResetDetached(sha); err != nil {
			return "", err
		}
	case errors.Is(statErr, os.ErrNotExist):
		if _, err := e.Mkdir(filepath.Dir(path)); err != nil {
			return "", err
		}
		if err := live.AddDetachedWorktree(path, sha); err != nil {
			return "", err
		}
	default:
		return "", statErr
	}
	path = resolvePath(path)
	co, err := git.Open(e, path)
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(filepath.Join(path, ".gitmodules")); err == nil {
		if err := co.UpdateSubmodules(); err != nil {
			return "", err
		}
	}
	leftover, err := co.Changes()
	if err != nil {
		return "", err
	}
	if len(leftover) > 0 {
		return "", fmt.Errorf("the release checkout at %s is not clean after being reset to %s:\n%s\nDelete %s (saferm delete --on-error abort --description \"an unclean release checkout\" %s) and run the release again; the next release creates it afresh", path, short(sha), renderChanges(leftover), path, path)
	}
	return path, nil
}

// short is a commit id's first twelve characters.
func short(sha string) string {
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}

// prepareReleaseBin creates the release's own directory for binaries, empty,
// beside the release checkout, and returns its path.
func prepareReleaseBin(e *strictcli.Effects, live git.Repo) (string, error) {
	common, err := live.CommonDir()
	if err != nil {
		return "", err
	}
	path := filepath.Join(resolvePath(common), filepath.FromSlash(ReleaseBinRelative))
	if _, err := e.Remove(path); err != nil {
		return "", err
	}
	if _, err := e.Mkdir(path); err != nil {
		return "", err
	}
	return path, nil
}

// releaseEnvironment is what every process of a release in the checkout co
// gets (see Checkout.Environment).
func releaseEnvironment(co git.Repo, releaseBin string) (map[string]string, error) {
	gowork := "off"
	tracked, err := co.Tracks("go.work")
	if err != nil {
		return nil, err
	}
	if tracked {
		gowork = ""
	}
	searchPath := releaseBin
	if p := os.Getenv("PATH"); p != "" {
		searchPath += string(os.PathListSeparator) + p
	}
	return map[string]string{"GOWORK": gowork, ReleaseBinEnv: releaseBin, "PATH": searchPath}, nil
}

// EnterCheckout prepares the release checkout at sha, the tip of branch,
// and the release's directory for binaries, and returns the checkout. The
// checkout is left as it is when the release ends, to be reset by the next.
func EnterCheckout(e *strictcli.Effects, live git.Repo, branch, sha string) (*Checkout, error) {
	liveRoot := resolvePath(live.Dir())
	path, err := PrepareCheckout(e, live, sha)
	if err != nil {
		return nil, err
	}
	bin, err := prepareReleaseBin(e, live)
	if err != nil {
		return nil, err
	}
	co, err := git.Open(e, path)
	if err != nil {
		return nil, err
	}
	env, err := releaseEnvironment(co, bin)
	if err != nil {
		return nil, err
	}
	return &Checkout{LiveRoot: liveRoot, Path: path, Branch: branch, Tip: sha, ReleaseBin: bin, env: env, e: e, live: live, repo: co}, nil
}

// LiveBranch is the branch the working tree is on; a detached HEAD is
// refused.
func LiveBranch(live git.Repo) (string, error) {
	branch, attached, err := live.HeadBranch()
	if err != nil {
		return "", err
	}
	if !attached || branch == "" {
		return "", errors.New("HEAD is detached: a release runs from a named release branch; check out the release branch and run the release again")
	}
	return branch, nil
}

// isRunState reports whether the repository-relative path is rlsbl's own
// run state, which sits in the working tree on purpose and is nobody's
// uncommitted work.
func isRunState(path string) bool {
	return path == declarations.ReleaseStateDir || strings.HasPrefix(path, declarations.ReleaseStateDir+"/")
}

// LiveChanges are the working tree's uncommitted changes, rlsbl's own run
// state left out.
func LiveChanges(live git.Repo) ([]git.Change, error) {
	all, err := live.Changes()
	if err != nil {
		return nil, err
	}
	var out []git.Change
	for _, c := range all {
		if !isRunState(c.Path) {
			out = append(out, c)
		}
	}
	return out, nil
}

// underScope reports whether path is scope or lies under the directory
// scope.
func underScope(path, scope string) bool {
	scope = strings.TrimRight(scope, "/")
	return path == scope || strings.HasPrefix(path, scope+"/")
}

// PartitionChanges splits changes against the paths a release writes (a
// directory covers everything under it): a change inside them would be
// overwritten by the release, so it blocks; every other change belongs to
// somebody else and is left alone.
func PartitionChanges(changes []git.Change, scope []string) (blocking, ignored []git.Change) {
	for _, c := range changes {
		hit := false
		for _, s := range scope {
			if underScope(c.Path, s) {
				hit = true
				break
			}
		}
		if hit {
			blocking = append(blocking, c)
		} else {
			ignored = append(ignored, c)
		}
	}
	return blocking, ignored
}

func renderChanges(changes []git.Change) string {
	lines := make([]string, len(changes))
	for i, c := range changes {
		lines[i] = "  " + c.Status + " " + c.Path
	}
	return strings.Join(lines, "\n")
}

// ConflictMessage is the refusal of uncommitted changes to paths a release
// writes. what names the release ("The release"); rerun is what to do once
// they are committed ("run the release again").
func ConflictMessage(blocking []git.Change, what, rerun string) string {
	return fmt.Sprintf("%s writes these paths, and they have uncommitted changes in the working tree:\n%s\nNothing was written to the branch or to the working tree. Commit those changes (or take back your own edits to those files), then %s.", what, renderChanges(blocking), rerun)
}

// IgnoredReport says which uncommitted changes a release leaves alone, or
// is empty when there are none.
func IgnoredReport(ignored []git.Change) string {
	if len(ignored) == 0 {
		return ""
	}
	return "Uncommitted changes the release does not touch (they stay in the working tree and are not part of the release):\n" + renderChanges(ignored)
}

// refuseMoved is the refusal of an advance whose branch moved.
func (c *Checkout) refuseMoved(expected, current, rerun string) error {
	shown := "nothing"
	if current != "" {
		shown = short(current)
	}
	lines := []string{fmt.Sprintf("the release was stopped: %s moved while the release was running. This release left it at %s; it is now at %s.", c.Branch, short(expected), shown)}
	if current != "" {
		appeared, err := c.live.Commits([]string{current}, []string{expected})
		if err == nil && len(appeared) > 0 {
			lines = append(lines, "", "Commits that appeared on the branch:")
			for _, sha := range appeared {
				subject, _ := c.live.CommitSubject(sha)
				lines = append(lines, fmt.Sprintf("  %s  %s", short(sha), subject))
			}
		}
	}
	lines = append(lines, "", fmt.Sprintf("Nothing was written to %s or to the working tree: the branch only ever advances from where this release left it. To include those commits, record them with `rlsbl changelog add` and %s; to exclude them, move them off %s first.", c.Branch, rerun, c.Branch))
	return &BranchMovedError{Message: strings.Join(lines, "\n")}
}

// branchTip is where the release branch is now, empty when it is gone.
func (c *Checkout) branchTip() (string, error) {
	sha, found, err := c.live.ResolveCommit(c.BranchRef())
	if err != nil || !found {
		return "", err
	}
	return sha, nil
}

// Advance moves the release branch to the checkout's commit rev and writes
// the files that commit changes into the working tree. Nothing happens when
// the branch is already there. It refuses, writing nothing, when the branch
// is not where this release left it (*BranchMovedError), when the working
// tree is not on the release branch, and when a path the advance writes has
// uncommitted changes there (*LiveTreeConflictError). what names the
// release in a refusal; rerun is what to do once the cause is dealt with.
func (c *Checkout) Advance(rev, what, rerun string) error {
	next, found, err := c.repo.ResolveCommit(rev)
	if err != nil {
		return err
	}
	if !found {
		return fmt.Errorf("%s names no commit in the release checkout at %s", rev, c.Path)
	}
	old := c.Tip
	if next == old {
		return nil
	}
	current, err := c.branchTip()
	if err != nil {
		return err
	}
	if current != old {
		return c.refuseMoved(old, current, rerun)
	}
	branch, attached, err := c.live.HeadBranch()
	if err != nil {
		return err
	}
	if !attached || branch != c.Branch {
		head := "detached"
		if attached {
			head = "refs/heads/" + branch
		}
		return fmt.Errorf("the working tree at %s is no longer on %s (HEAD is %s), so the files of the release's commits cannot be written into it. Nothing was written. Check out %s again, then %s", c.LiveRoot, c.Branch, head, c.Branch, rerun)
	}
	paths, err := c.repo.ChangedBetween(old, next)
	if err != nil {
		return err
	}
	if blocking, err := c.editedAmong(paths); err != nil {
		return err
	} else if len(blocking) > 0 {
		return &LiveTreeConflictError{Message: ConflictMessage(blocking, what, rerun)}
	}
	if err := c.live.SwapRef(c.BranchRef(), next, old, "rlsbl release: advance "+c.Branch); err != nil {
		now, readErr := c.branchTip()
		if readErr != nil {
			return readErr
		}
		if now != old {
			return c.refuseMoved(old, now, rerun)
		}
		return err
	}
	if err := c.live.RestorePaths(next, paths); err != nil {
		return err
	}
	c.Tip = next
	c.last = &advance{old: old, new: next, paths: paths}
	return nil
}

// editedAmong are the working tree's uncommitted changes to any of paths.
func (c *Checkout) editedAmong(paths []string) ([]git.Change, error) {
	wanted := map[string]bool{}
	for _, p := range paths {
		wanted[p] = true
	}
	changes, err := LiveChanges(c.live)
	if err != nil {
		return nil, err
	}
	var out []git.Change
	for _, ch := range changes {
		if wanted[ch.Path] {
			out = append(out, ch)
		}
	}
	return out, nil
}

// TakeBack moves branch from commit from back to commit to, by
// compare-and-swap, the inverse of an advance: the branch returns only while
// it is still at from, and the files that differ between the two are
// restored to to's content only while every one of them still holds from's.
// ok is false, with the reason, when doing so would touch somebody else's
// work; nothing is written then.
func TakeBack(live git.Repo, branch, from, to string) (ok bool, reason string, err error) {
	ref := "refs/heads/" + branch
	current, found, err := live.ResolveCommit(ref)
	if err != nil {
		return false, "", err
	}
	if !found || current != from {
		shown := "nothing"
		if found {
			shown = short(current)
		}
		return false, fmt.Sprintf("%s is at %s, not at the release commit %s: something was committed on top of it", branch, shown, short(from)), nil
	}
	paths, err := live.ChangedBetween(to, from)
	if err != nil {
		return false, "", err
	}
	wanted := map[string]bool{}
	for _, p := range paths {
		wanted[p] = true
	}
	changes, err := LiveChanges(live)
	if err != nil {
		return false, "", err
	}
	var edited []git.Change
	for _, ch := range changes {
		if wanted[ch.Path] {
			edited = append(edited, ch)
		}
	}
	if len(edited) > 0 {
		return false, "files the release wrote were changed since:\n" + renderChanges(edited), nil
	}
	if err := live.SwapRef(ref, to, from, "rlsbl release: take back "+branch); err != nil {
		return false, branch + " moved while it was being taken back", nil
	}
	if err := live.RestorePaths(to, paths); err != nil {
		return false, "", err
	}
	return true, "", nil
}

// UnwindLastAdvance takes back the most recent advance (TakeBack). ok is
// true when there was nothing to take back or it is taken back, and false,
// with the reason, when doing so would touch somebody else's work.
func (c *Checkout) UnwindLastAdvance() (ok bool, reason string, err error) {
	if c.last == nil {
		return true, "", nil
	}
	ok, reason, err = TakeBack(c.live, c.Branch, c.last.new, c.last.old)
	if err != nil || !ok {
		return ok, reason, err
	}
	c.Tip = c.last.old
	c.last = nil
	return true, "", nil
}
