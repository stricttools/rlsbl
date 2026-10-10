package releaseops

import (
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/confidential"

	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/releasenotes"
)

// EditRequest is a `release edit`.
type EditRequest struct {
	// Dir is the working directory, absolute.
	Dir string
	// Version is the version whose Release is rewritten.
	Version string
	// TermList is where the confidential-term list the rewritten Release
	// body is scanned against is.
	TermList confidential.Location
}

// Edit rewrites one released version's GitHub Release in place from the
// record: the notes its changelog holds, the notices its archive records
// on top, the rlsbl-ci-sha marker naming its release commit, and the
// pre-release flag the notices decide. A version the record holds no release of,
// and one without a GitHub Release, are refused before anything is written.
func Edit(ctx *strictcli.Context, req EditRequest) error {
	s, err := Select(ctx.Effects(), req.Dir)
	if err != nil {
		return err
	}
	v, err := ParseVersion(req.Version, "the version")
	if err != nil {
		return err
	}
	if _, err := s.releasedArchive(v); err != nil {
		return err
	}
	scanner, err := publishrules.LoadScanner(s.Repo, req.TermList, time.Now())
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
