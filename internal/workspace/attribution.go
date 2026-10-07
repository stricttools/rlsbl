package workspace

import (
	"github.com/stricttools/rlsbl/internal/git"
)

// CommitOwnerNames are the names of the members owning any file the commit
// sha changed. A commit touching only tool-owned paths has none.
func (w *Workspace) CommitOwnerNames(repo git.Repo, sha string) (map[string]bool, error) {
	files, err := repo.CommitFiles(sha)
	if err != nil {
		return nil, err
	}
	return w.OwnerNames(files), nil
}

// FilterCommits keeps the commits, in their order, that change a file the
// scope claims. A commit whose files git cannot list is an error: which
// scope it belongs to cannot be guessed.
func FilterCommits(repo git.Repo, commits []string, scope Scope) ([]string, error) {
	var kept []string
	for _, sha := range commits {
		files, err := repo.CommitFiles(sha)
		if err != nil {
			return nil, err
		}
		if scope.ClaimsAny(files) {
			kept = append(kept, sha)
		}
	}
	return kept, nil
}
