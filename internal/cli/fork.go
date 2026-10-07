package cli

import (
	"fmt"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/release"
)

// forkHistory is the upstream history of the repository rooted at root
// when it declares itself a fork (.strictmetadata/upstream/upstream.toml),
// and the zero Fork otherwise: everything reachable from the upstream
// branch's ref and from the inherited tags kept under refs/tags-of/, which
// every unreleased range leaves out. A fork whose upstream branch ref is
// missing is refused, naming the fetches that restore it.
func forkHistory(e *strictcli.Effects, root string) (release.Fork, error) {
	up, found, diags, err := strictspec.LoadUpstream(root)
	if err != nil || !found {
		return release.Fork{}, err
	}
	if len(diags) > 0 {
		var lines []string
		for _, d := range diags {
			lines = append(lines, d.Code+": "+d.Message)
		}
		return release.Fork{}, fmt.Errorf("%s is not a valid upstream declaration:\n  %s", strictspec.UpstreamFile, strings.Join(lines, "\n  "))
	}
	slug := up.Host + "/" + up.Owner + "/" + up.Repo
	url := "https://" + slug
	branchRef := "refs/upstream/" + slug + "/" + up.Branch
	keptPrefix := "refs/tags-of/" + slug + "/"
	repo, err := git.Open(e, root)
	if err != nil {
		return release.Fork{}, err
	}
	if _, found, err := repo.ResolveCommit(branchRef); err != nil {
		return release.Fork{}, err
	} else if !found {
		return release.Fork{}, fmt.Errorf("this repository is a fork of %s (branch %s, declared in %s), but the ref holding upstream's history is missing here: %s\n"+
			"  Changelog coverage leaves out every commit reachable from that ref and from the %s* refs, since upstream's commits are not this repository's to describe; without it every inherited commit would be asked for an entry, so rlsbl refuses instead.\n"+
			"  Fetch upstream's branch, and the inherited tags this repository keeps on origin, then run again:\n"+
			"    git fetch --no-tags %s +refs/heads/%s:%s\n"+
			"    git fetch --no-tags origin '%s*:%s*'",
			url, up.Branch, strictspec.UpstreamFile, branchRef, keptPrefix, url, up.Branch, branchRef, keptPrefix, keptPrefix)
	}
	kept, err := repo.RefNames(keptPrefix)
	if err != nil {
		return release.Fork{}, err
	}
	return release.Fork{Upstream: url, Exclude: append([]string{branchRef}, kept...)}, nil
}
