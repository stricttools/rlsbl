package cli

import (
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/upstream"
)

const upstreamGroupHelp = "Manage a fork's relation to its upstream, which the fork declares in .strictmetadata/upstream/upstream.toml (host, " +
	"owner, repo, and branch; a repository without that file is not a fork, and no upstream is ever inferred from a git remote). Changelog " +
	"coverage in a fork leaves out every commit reachable from the upstream branch, fetched into refs/upstream/<host>/<owner>/<repo>/<branch>, " +
	"and from the inherited tags kept under refs/tags-of/<host>/<owner>/<repo>/."

const adoptTagsHelp = "Move the tags this fork inherited from its upstream out of refs/tags. A tag is inherited when the upstream " +
	"(https://<host>/<owner>/<repo>, asked with git ls-remote, never through a package registry) has a tag of the same name at the same object. " +
	"Each inherited tag moves, keeping its object, to refs/tags-of/<host>/<owner>/<repo>/<tag>: the ref is written here, one push creates it on " +
	"origin and a second push deletes the tag from origin's refs/tags, each guarded by the object observed, and then the tag is deleted from " +
	"refs/tags here. Every push carries one ref, because GitHub fires no events for a push deleting four or more tags, and tags move one after " +
	"another, so a run that stops part-way leaves every earlier tag finished. Tags the upstream does not have are left alone. A tag carrying an " +
	"inherited tag's name at a different object (here, on origin, or against a kept ref), and an inherited tag whose object is only on origin, " +
	"refuse the whole run, named, before anything is written. A run after an interrupted one finishes it, and otherwise has nothing to do. " +
	"Use --dry-run to print the plan and write nothing."

func registerUpstream(r *commandSet) {
	r.group([]string{"upstream"}, upstreamGroupHelp)
	r.add(command{
		path:   []string{"upstream", "adopt-tags"},
		help:   adoptTagsHelp,
		effect: mutating,
		// Only a person may decide that tags this repository carries are
		// the upstream's releases rather than its own: the command deletes
		// them from refs/tags here and on origin.
		consequential: true,
		run: func(ctx *strictcli.Context, _ map[string]any) (any, error) {
			_, root, err := workingRepository()
			if err != nil {
				return nil, err
			}
			return nil, upstream.RunAdoptTags(ctx, root)
		},
	})
}
