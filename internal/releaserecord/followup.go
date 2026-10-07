package releaserecord

import (
	"fmt"
	"strings"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/github"
)

// Two closed identities of the lifecycle-and-license record imply an
// obligation outside this repository that nothing else verifies:
//
//   - a closed repository-url identity leaves the old repository in place;
//     until it is archived it keeps taking issues and pull requests and
//     looks like the place to send a fix;
//   - a closed go-module-path identity leaves the old module path published;
//     until it serves a deprecation notice, every consumer resolving it gets
//     a module with no sign that it moved.
//
// Both evaluations ask the network through a probe the caller passes, and an
// unanswered probe is a problem, never a pass: "could not ask" is not
// evidence that the old repository is archived or the old module
// deprecated. Nothing here performs the fix; each problem names it.

// FollowupVerdict is the outcome of one evaluation.
type FollowupVerdict struct {
	Problems []string
	Notes    []string
	// SkipReason is set when the record holds nothing to evaluate.
	SkipReason string
}

// OK reports whether the evaluation found no problem.
func (v FollowupVerdict) OK() bool { return len(v.Problems) == 0 }

// closedIdentities lists the record's closed identities of facet, in record
// order.
func closedIdentities(identities []lifecycle.Identity, facet lifecycle.Facet) []lifecycle.Identity {
	var out []lifecycle.Identity
	for _, id := range identities {
		if id.Facet == facet && !id.Pending() && !id.Open() {
			out = append(out, id)
		}
	}
	return out
}

// successor is the value the identity changed to: the identity of the same
// subject and facet starting the day it ended, and false when the record
// holds none.
func successor(identities []lifecycle.Identity, old lifecycle.Identity) (string, bool) {
	for _, id := range identities {
		if id.Subject == old.Subject && id.Facet == old.Facet && !id.Pending() && id.From.Equal(old.Until) {
			return id.Value, true
		}
	}
	return "", false
}

// githubRepository reads a repository-url value naming a github.com
// repository, and false for any other host, a local path, and an SSH alias:
// those are reported as unprobeable rather than assumed to be GitHub.
func githubRepository(url string) (github.Repository, bool) {
	trimmed := strings.TrimSpace(url)
	var rest string
	switch {
	case strings.HasPrefix(trimmed, "https://github.com/"):
		rest = strings.TrimPrefix(trimmed, "https://github.com/")
	case strings.HasPrefix(trimmed, "git@github.com:"):
		rest = strings.TrimPrefix(trimmed, "git@github.com:")
	case strings.HasPrefix(trimmed, "ssh://git@github.com/"):
		rest = strings.TrimPrefix(trimmed, "ssh://git@github.com/")
	default:
		return github.Repository{}, false
	}
	repo, err := github.ParseRepository(strings.TrimSuffix(strings.TrimSuffix(rest, "/"), ".git"))
	if err != nil {
		return github.Repository{}, false
	}
	return repo, true
}

// ArchivedProbe answers whether a GitHub repository is archived; an error is
// an unanswered probe.
type ArchivedProbe func(repo github.Repository) (archived bool, err error)

// EvaluateOldRepositoryArchived checks that every repository a closed
// repository-url identity names is archived.
func EvaluateOldRepositoryArchived(identities []lifecycle.Identity, probe ArchivedProbe) FollowupVerdict {
	closed := closedIdentities(identities, lifecycle.FacetRepositoryURL)
	if len(closed) == 0 {
		return FollowupVerdict{SkipReason: "the lifecycle-and-license record holds no closed repository-url identity"}
	}
	var v FollowupVerdict
	seen := map[string]bool{}
	archived := 0
	for _, id := range closed {
		repo, ok := githubRepository(id.Value)
		if !ok {
			v.Notes = append(v.Notes, fmt.Sprintf("%s: not a github.com repository, so whether it is archived cannot be asked", id.Value))
			continue
		}
		if seen[repo.String()] {
			continue
		}
		seen[repo.String()] = true
		isArchived, err := probe(repo)
		switch {
		case err != nil:
			v.Problems = append(v.Problems, fmt.Sprintf("%s: could not determine whether the repository %q left on %s is archived (%v). An unanswered probe is not an answer; fix the connection or the credential and re-run.", repo, id.Subject, formatDay(id.Until), err))
		case isArchived:
			archived++
		default:
			v.Problems = append(v.Problems, fmt.Sprintf("%s stopped being the repository of %q on %s but is still active, so it keeps collecting issues, pull requests, and clones for code that lives elsewhere. Archive it: `gh repo archive %s`.", repo, id.Subject, formatDay(id.Until), repo))
		}
	}
	if len(v.Problems) == 0 && archived > 0 {
		v.Notes = append([]string{fmt.Sprintf("%d earlier repositories archived", archived)}, v.Notes...)
	}
	if len(v.Problems) == 0 && archived == 0 && len(v.Notes) == 0 {
		return FollowupVerdict{SkipReason: "no closed repository-url identity names a repository to ask about"}
	}
	return v
}

// DeprecationStatus is what the module proxy serves for a module path.
type DeprecationStatus string

// The answers.
const (
	// Deprecated: the highest published version's go.mod carries a
	// deprecation notice.
	Deprecated DeprecationStatus = "deprecated"
	// NotDeprecated: it carries none.
	NotDeprecated DeprecationStatus = "not-deprecated"
	// NeverPublished: the proxy has never served the path.
	NeverPublished DeprecationStatus = "never-published"
)

// DeprecationAnswer is a probe's answer about one module path.
type DeprecationAnswer struct {
	Status DeprecationStatus
	// Version is the highest published version, read when it is.
	Version string
}

// DeprecationProbe answers whether a module path serves a deprecation
// notice; an error is an unanswered probe.
type DeprecationProbe func(module string) (DeprecationAnswer, error)

// EvaluateGoDeprecationPublished checks that every module path a closed
// go-module-path identity names serves a deprecation notice.
func EvaluateGoDeprecationPublished(identities []lifecycle.Identity, probe DeprecationProbe) FollowupVerdict {
	closed := closedIdentities(identities, lifecycle.FacetGoModulePath)
	if len(closed) == 0 {
		return FollowupVerdict{SkipReason: "the lifecycle-and-license record holds no closed go-module-path identity"}
	}
	var v FollowupVerdict
	seen := map[string]bool{}
	deprecated := 0
	for _, id := range closed {
		if seen[id.Value] {
			continue
		}
		seen[id.Value] = true
		moved := "a new path"
		if next, ok := successor(identities, id); ok {
			moved = next
		}
		answer, err := probe(id.Value)
		if err != nil {
			v.Problems = append(v.Problems, fmt.Sprintf("%s: could not determine whether the superseded module path is deprecated (%v). An unanswered probe is not an answer; fix the connection to the module proxy and re-run.", id.Value, err))
			continue
		}
		switch answer.Status {
		case Deprecated:
			deprecated++
		case NeverPublished:
			v.Notes = append(v.Notes, fmt.Sprintf("%s: the module proxy has never served this path, so nothing published needs deprecating", id.Value))
		case NotDeprecated:
			v.Problems = append(v.Problems, fmt.Sprintf("%s moved to %s on %s, but the module proxy still serves %s of the old path with no deprecation notice, so `go get` on it says nothing. In the old repository add a `// Deprecated: moved to %s` comment above its module directive and a retract of its published versions, and release it.", id.Value, moved, formatDay(id.Until), answer.Version, moved))
		default:
			v.Problems = append(v.Problems, fmt.Sprintf("%s: the probe answered %q, which is none of %s, %s, and %s", id.Value, answer.Status, Deprecated, NotDeprecated, NeverPublished))
		}
	}
	if len(v.Problems) == 0 && deprecated > 0 {
		v.Notes = append([]string{fmt.Sprintf("%d superseded module paths deprecated", deprecated)}, v.Notes...)
	}
	return v
}

func formatDay(t time.Time) string { return t.Format(time.DateOnly) }
