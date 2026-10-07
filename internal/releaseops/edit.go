package releaseops

import (
	"fmt"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/releasenotes"
	"github.com/stricttools/rlsbl/internal/semver"
)

// EditRequest is a `release edit`.
type EditRequest struct {
	// Dir is the working directory, absolute.
	Dir string
	// Version is the version whose Release is rewritten, and empty for the
	// releasable's latest release.
	Version string
	// IndexPath is the machine-local confidential-name index the rewritten
	// Release body is scanned against.
	IndexPath string
}

// Edit rewrites one released version's GitHub Release in place from the
// record: the notes its changelog holds, the notices its archive records
// on top, the rlsbl-ci-sha marker naming its release commit, and the
// pre-release flag the notices decide. The version defaults to the latest
// release the archives record. A version the record holds no release of,
// and one without a GitHub Release, are refused before anything is written.
func Edit(ctx *strictcli.Context, req EditRequest) error {
	s, err := Select(ctx.Effects(), req.Dir)
	if err != nil {
		return err
	}
	var v semver.Version
	if req.Version == "" {
		latest, found, err := s.latestReleased()
		if err != nil {
			return err
		}
		if !found {
			return fmt.Errorf("the release archives of %s in %s record no release, so there is no Release to edit", s.Releasable.Name, s.Record.Dir())
		}
		v = latest
	} else if v, err = ParseVersion(req.Version, "the version"); err != nil {
		return err
	}
	if _, err := s.releasedArchive(v); err != nil {
		return err
	}
	scanner, err := publishrules.LoadScanner(s.Root(), req.IndexPath, time.Now())
	if err != nil {
		return err
	}
	doc, err := releasenotes.Read(s.Root(), s.Releasable.Name, s.Scheme, v)
	if err != nil {
		return err
	}
	gh, slug, err := openGitHub(ctx.Effects(), s)
	if err != nil {
		return err
	}
	if err := requireRelease(gh, slug, doc.Tag); err != nil {
		return err
	}
	if err := releasenotes.Rewrite(gh, slug, scanner, doc); err != nil {
		return err
	}
	if ctx.DryRun() {
		ctx.Out("Would rewrite the GitHub Release of " + doc.Tag + " from the record")
		return nil
	}
	ctx.Out("Rewrote the GitHub Release of " + doc.Tag + " from the record")
	return nil
}
