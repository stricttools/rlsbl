package releasenotes

import (
	"fmt"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/publishrules"
)

// Check refuses a document whose Release body carries a confidential name
// in a public repository (the scanner decides). Every writer of a Release
// body below runs it on the body it writes before writing; a command that
// writes elsewhere first (an archive notice) runs it before that, too. A
// missing scanner is an error, so no body is ever written unscanned.
func Check(scanner *publishrules.Scanner, doc Document) error {
	body, err := doc.Body()
	if err != nil {
		return err
	}
	return checkBody(scanner, doc.Tag, body)
}

func checkBody(scanner *publishrules.Scanner, tag, body string) error {
	if scanner == nil {
		return fmt.Errorf("the GitHub Release body of %s is written only after the confidential-name scan, and no scanner was given", tag)
	}
	return scanner.ScanTexts([]publishrules.Text{{Name: "the GitHub Release body of " + tag, Content: body}})
}

// Create creates the Release doc describes, refusing through gh's
// --verify-tag when the tag is not on GitHub. movesLatest states whether it
// may take the repository's "Latest" badge: true for a release, and for a
// repair only where RepairTakesLatest says so.
func Create(gh github.Client, repo github.Repository, scanner *publishrules.Scanner, doc Document, movesLatest bool, extra ...strictcli.EffectOption) error {
	body, err := doc.Body()
	if err != nil {
		return err
	}
	if err := checkBody(scanner, doc.Tag, body); err != nil {
		return err
	}
	return gh.CreateRelease(repo, github.NewRelease{
		Tag:         doc.Tag,
		Title:       doc.Title(),
		Notes:       body,
		Prerelease:  doc.Prerelease(),
		MovesLatest: movesLatest,
	}, extra...)
}

// Rewrite rewrites the existing Release of doc's tag in place to the
// document doc describes: its body, its title, and its pre-release flag,
// stated in both directions. A document naming no release commit keeps the
// marker the Release's body carries (KeepingMarkerOf), which is read first;
// a Release that does not exist is an error. Nothing is deleted, so a
// failure leaves the old Release as it was.
func Rewrite(gh github.Client, repo github.Repository, scanner *publishrules.Scanner, doc Document, extra ...strictcli.EffectOption) error {
	if doc.ReleaseCommit == "" {
		existing, err := gh.ReleaseBody(repo, doc.Tag)
		if err != nil {
			return fmt.Errorf("reading the GitHub Release of %s: %w", doc.Tag, err)
		}
		doc = doc.KeepingMarkerOf(existing)
	}
	body, err := doc.Body()
	if err != nil {
		return err
	}
	if err := checkBody(scanner, doc.Tag, body); err != nil {
		return err
	}
	return gh.RewriteRelease(repo, doc.Tag, github.ReleaseDocument{Notes: body, Title: doc.Title(), Prerelease: doc.Prerelease()}, extra...)
}

// EnsureMarker puts doc's marker onto the existing Release of doc's tag,
// replacing a different marker and leaving everything else of the body as
// it is, and reports whether it wrote. A release resuming onto a Release
// that already exists asks this, so the publish workflow always reads the
// commit CI verified.
func EnsureMarker(gh github.Client, repo github.Repository, scanner *publishrules.Scanner, doc Document, extra ...strictcli.EffectOption) (bool, error) {
	marker, err := doc.Marker()
	if err != nil {
		return false, err
	}
	existing, err := gh.ReleaseBody(repo, doc.Tag)
	if err != nil {
		return false, fmt.Errorf("reading the GitHub Release of %s: %w", doc.Tag, err)
	}
	body, changed := WithMarker(existing, marker)
	if !changed {
		return false, nil
	}
	if err := checkBody(scanner, doc.Tag, body); err != nil {
		return false, err
	}
	if err := gh.EditReleaseNotes(repo, doc.Tag, body, extra...); err != nil {
		return false, err
	}
	return true, nil
}

// Publish creates the Release of doc's tag when GitHub has none and
// otherwise rewrites the existing one in place, reporting whether it
// created. movesLatest is Create's.
func Publish(gh github.Client, repo github.Repository, scanner *publishrules.Scanner, doc Document, movesLatest bool, extra ...strictcli.EffectOption) (created bool, err error) {
	exists, err := gh.ReleaseExists(repo, doc.Tag)
	if err != nil {
		return false, err
	}
	if exists {
		return false, Rewrite(gh, repo, scanner, doc, extra...)
	}
	return true, Create(gh, repo, scanner, doc, movesLatest, extra...)
}

// RepairTakesLatest decides whether a repair creating doc's Release takes
// the repository's "Latest" badge. A release keeps GitHub's default (the
// newest release shows as Latest) and a repair re-creating an old Release
// must not move the badge onto it; but a repair is also how the newest
// release gets its Release when the release's own Release step failed, and
// that one should show as Latest. So a repair takes the badge when the
// repository has no Latest Release, or when newer says doc's tag is more
// recent than the Latest's. A pre-release never takes it, as GitHub never
// marks one Latest. Every question that cannot be answered is an error.
func RepairTakesLatest(gh github.Client, repo github.Repository, doc Document, newer func(latestTag string) (bool, error)) (bool, error) {
	if doc.Prerelease() {
		return false, nil
	}
	latest, found, err := gh.LatestReleaseTag(repo)
	if err != nil {
		return false, err
	}
	if !found {
		return true, nil
	}
	return newer(latest)
}

// TagNewerInHistory reports whether tag's commit is a strict descendant of
// than's: recency in one repository's history, which is what a workspace
// asks, since the Latest Release may belong to another releasable whose
// version number says nothing about which release came last. A tag that
// does not exist locally, and an ancestry git cannot decide, are errors.
func TagNewerInHistory(repo git.Repo, tag, than string) (bool, error) {
	mine, err := localTagCommit(repo, tag)
	if err != nil {
		return false, err
	}
	theirs, err := localTagCommit(repo, than)
	if err != nil {
		return false, err
	}
	if mine == theirs {
		return false, nil
	}
	verdict, err := repo.Ancestry(theirs, mine)
	if err != nil {
		return false, err
	}
	switch verdict {
	case git.IsAncestor:
		return true, nil
	case git.NotAncestor:
		return false, nil
	}
	return false, fmt.Errorf("git cannot tell whether %s (%s) is an ancestor of %s (%s): the history is shallow or missing objects. Deepen it with `git fetch --unshallow` and run again", than, theirs, tag, mine)
}

func localTagCommit(repo git.Repo, tag string) (string, error) {
	sha, found, err := repo.TagCommit(tag)
	if err != nil {
		return "", err
	}
	if !found {
		return "", fmt.Errorf("the tag %s does not exist in this repository, so where it sits in the history cannot be read. Fetch the tags with `git fetch origin --tags` and run again", tag)
	}
	return sha, nil
}
