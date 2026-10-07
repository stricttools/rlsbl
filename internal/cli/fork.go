package cli

import (
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/upstream"
)

// forkHistory is the upstream history of the repository rooted at root
// when it declares itself a fork, and the zero Fork otherwise: the URL of
// the repository forked and the revisions every unreleased range leaves
// out (upstream.HistoryExclusions, which refuses a fork whose upstream
// branch ref is missing, naming the fetches that restore it).
func forkHistory(e *strictcli.Effects, root string) (release.Fork, error) {
	url, err := upstream.URLOf(root)
	if err != nil || url == "" {
		return release.Fork{}, err
	}
	repo, err := git.Open(e, root)
	if err != nil {
		return release.Fork{}, err
	}
	exclude, err := upstream.HistoryExclusions(repo)
	if err != nil {
		return release.Fork{}, err
	}
	return release.Fork{Upstream: url, Exclude: exclude}, nil
}
