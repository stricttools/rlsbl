// Package publishrules is rlsbl's side of the lifecycle-and-license rules at
// release time: the repository's visibility on GitHub, the refusals of what
// a releasable's release would publish (the proprietary-refuses-public-output
// and private-repository-publishing rules, which the lifecycle library
// decides), the deploy command's license precondition, the publish workflow
// features scaffold may render, the packed-artifact contents check, and the
// scan of published text for confidential names.
//
// The rules themselves live in the lifecycle library; this package supplies
// what the library needs from rlsbl's world (GitHub's answer, the
// declarations, the committed workflows, the packed artifacts) and turns its
// refusals into errors naming where each refused thing is declared. Every
// question takes the date it is asked about: nothing here reads the clock.
package publishrules

import (
	"fmt"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/github"
)

// VisibilitySource answers the repository's visibility on GitHub. A source
// is asked only when a rule's answer depends on it, and at most once.
type VisibilitySource interface {
	// Visibility is GitHub's answer. When it cannot be had, the error says
	// why, and the visibility is lifecycle.VisibilityUnknown.
	Visibility() (lifecycle.Visibility, error)
}

// gitHubVisibility asks GitHub once and keeps the answer.
type gitHubVisibility struct {
	client github.Client
	repo   github.Repository
	asked  bool
	answer lifecycle.Visibility
	err    error
}

// GitHubVisibility is the visibility GitHub reports for repo, asked through
// client the first time it is needed. GitHub's internal visibility is
// private: an internal repository is not public, and GitHub reports it
// private as well.
func GitHubVisibility(client github.Client, repo github.Repository) VisibilitySource {
	return &gitHubVisibility{client: client, repo: repo}
}

func (g *gitHubVisibility) Visibility() (lifecycle.Visibility, error) {
	if !g.asked {
		g.asked = true
		g.answer, g.err = ask(g.client, g.repo)
	}
	return g.answer, g.err
}

func ask(client github.Client, repo github.Repository) (lifecycle.Visibility, error) {
	info, err := client.Info(repo)
	if err != nil {
		return lifecycle.VisibilityUnknown, fmt.Errorf("asking GitHub for the visibility of %s: %w", repo, err)
	}
	switch info.Visibility {
	case github.Public:
		return lifecycle.VisibilityPublic, nil
	case github.Private, github.Internal:
		return lifecycle.VisibilityPrivate, nil
	}
	return lifecycle.VisibilityUnknown, fmt.Errorf("GitHub reports the visibility of %s as %q", repo, info.Visibility)
}

// knownVisibility is a visibility the caller already holds.
type knownVisibility lifecycle.Visibility

// KnownVisibility is a source answering v, for a caller that already asked
// GitHub (and for tests). lifecycle.VisibilityUnknown is refused as an
// answer: an unknown visibility comes with the reason it is unknown.
func KnownVisibility(v lifecycle.Visibility) (VisibilitySource, error) {
	switch v {
	case lifecycle.VisibilityPublic, lifecycle.VisibilityPrivate:
		return knownVisibility(v), nil
	}
	return nil, fmt.Errorf("%q is not a visibility GitHub answers; a visibility that could not be had comes from GitHubVisibility, with the reason", v)
}

func (k knownVisibility) Visibility() (lifecycle.Visibility, error) {
	return lifecycle.Visibility(k), nil
}

// CheckVisibility evaluates the proprietary-requires-private rule: a
// confidential repository must be private on GitHub, and a private one must
// be confidential. It always asks the source, and a visibility that cannot
// be had is refused, carrying the reason. repository-visibility and release
// validation call it.
func CheckVisibility(record *lifecycle.Record, source VisibilitySource, on time.Time) error {
	v, askErr := source.Visibility()
	if err := record.VisibilityAllowed(v, on); err != nil {
		return withReason(err, askErr)
	}
	return nil
}

// withReason adds why the visibility is unknown to a refusal that rests on
// it.
func withReason(refusal, askErr error) error {
	if askErr == nil {
		return refusal
	}
	return fmt.Errorf("%w (%v)", refusal, askErr)
}
