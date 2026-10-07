package release

import (
	"path/filepath"
	"slices"

	"github.com/stricttools/rlsbl/internal/git"
)

// RemoveCheckout removes the repository's release checkout when it exists,
// and says so through say. A history rewrite calls it: the checkout's
// detached HEAD pins the history as it was before the rewrite, so the
// rewritten-away commits stay reachable until it is gone. The next release
// creates it afresh. A directory at the checkout's path that is not a
// registered worktree of the repository is left alone.
func RemoveCheckout(live git.Repo, say func(string)) error {
	checkout, err := CheckoutDir(live)
	if err != nil {
		return err
	}
	worktrees, err := live.Worktrees()
	if err != nil {
		return err
	}
	if !slices.Contains(worktrees, resolvePath(checkout)) {
		return nil
	}
	if err := live.RemoveWorktree(checkout); err != nil {
		return err
	}
	say("Removed the release checkout at " + filepath.ToSlash(checkout) + ": it pinned the history as it was before the rewrite. The next release creates it afresh.")
	return nil
}
