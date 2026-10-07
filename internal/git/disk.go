package git

import (
	"os"
	"path/filepath"
	"strings"
)

// IsRepositoryRoot reports whether dir is the root of a git working tree,
// answered the way git's own discovery answers it and without running git:
// a .git directory holding HEAD, or a .git file naming its git directory
// (gitdir: ...), as a linked worktree or a submodule has. A .git entry that
// is neither is not a repository, and git walks past it.
func IsRepositoryRoot(dir string) bool {
	marker := filepath.Join(dir, ".git")
	info, err := os.Stat(marker)
	if err != nil {
		return false
	}
	if info.IsDir() {
		head, err := os.Stat(filepath.Join(marker, "HEAD"))
		return err == nil && !head.IsDir()
	}
	data, err := os.ReadFile(marker)
	if err != nil {
		return false
	}
	return strings.HasPrefix(string(data), "gitdir:")
}
