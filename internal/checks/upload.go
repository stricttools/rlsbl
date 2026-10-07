package checks

import (
	"fmt"
	"path/filepath"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/targets"
)

// The upload family: what a publishing member's upload would carry, listed
// without the network (the npm tarball through `npm pack --dry-run`, the Go
// module zip from the tracked files). A Python upload has to be built,
// which needs the network, so the pypi CI workflow builds and checks it on
// the candidate commit instead, and these checks answer nothing for it.
func uploadChecks() []check {
	return []check{
		errorCheck("upload-private-paths", checkUploadPrivatePaths),
	}
}

// observedHandle is the run's effects handle with every program it starts
// declared an observe: a check reports and changes nothing in the
// repository, which is what lets the read-only check command start a test
// run, a dry-run sync, or a dry-run pack.
type observedHandle struct {
	*strictcli.Effects
}

func (h observedHandle) Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error) {
	return h.Effects.Run(argv, append(append([]strictcli.EffectOption(nil), opts...), strictcli.Observe())...)
}

// upload is what one target's upload of a publishing member would carry.
type upload struct {
	member declarations.Member
	// dir is the target's directory, repository-relative.
	dir     string
	listing targets.UploadListing
}

// uploadsOf lists the uploads of the members that publish, one per target
// directory whose upload can be listed offline. An upload that cannot be
// listed is returned as a problem.
func uploadsOf(c *Context, members []declarations.Member) (uploads []upload, problems []string) {
	seen := map[string]bool{}
	for _, m := range members {
		if c.Workspace().PublishModeOf(m) == declarations.PublishNone {
			continue
		}
		for _, t := range targetsOf(c, m) {
			key := t.target.Name() + "\x00" + t.dir
			if !t.target.Facts().ListsUploadOffline || seen[key] {
				continue
			}
			seen[key] = true
			listing, found, err := targets.ListUpload(observedHandle{c.Effects()}, c.Repo(), t.target, t.dir, c.in.Scratch)
			if err != nil {
				problems = append(problems, fmt.Sprintf("%s: the %s upload in %s cannot be listed: %v", m.Name, t.target.Name(), dirLabel(c, t.dir), err))
				continue
			}
			if !found {
				continue
			}
			rel, err := filepath.Rel(c.Root(), t.dir)
			if err != nil {
				panic(unanswered(err.Error()))
			}
			uploads = append(uploads, upload{member: m, dir: filepath.ToSlash(rel), listing: listing})
		}
	}
	return uploads, problems
}

func checkUploadPrivatePaths(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	uploads, problems := uploadsOf(c, c.Members())
	if len(uploads) == 0 && len(problems) == 0 {
		return r.Skipped("no publishing member has an npm package or Go module zip to list")
	}
	for _, u := range uploads {
		for _, p := range targets.PrivatePathsIn(u.listing.Files) {
			problems = append(problems, fmt.Sprintf("%s: %s would ship %s, a private path (%s): %s", u.member.Name, u.listing.Label, declarations.Join(u.dir, p.Rel), p.Rule, u.listing.PrivatePathFix(p.Rel, p.Rule, p.Directory)))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d private path(s) an upload would carry; a registry keeps every upload permanently", len(problems)), fmt.Sprintf("no npm package or Go module zip of the %d upload(s) carries a private path", len(uploads)))
}
