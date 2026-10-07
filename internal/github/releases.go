package github

import (
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// ReleaseListLimit is how many Releases one listing asks gh for. `gh release
// list` reports no total and offers no pagination, so a listing that comes
// back holding this many entries may be truncated and is refused.
const ReleaseListLimit = 1000

// releaseNotFound is what gh prints when the Release a view names does not
// exist.
const releaseNotFound = "release not found"

// ReleaseExists reports whether the repository has a GitHub Release for tag.
// gh's own "release not found" is the one answer read as absence; any other
// failure is an error.
func (c Client) ReleaseExists(repo Repository, tag string) (bool, error) {
	args := []string{"release", "view", tag, "--repo", repo.String(), "--json", "tagName", "--jq", ".tagName"}
	res, err := c.read(readTimeout, args...)
	if err != nil {
		return false, err
	}
	if res.code == 0 {
		return true, nil
	}
	if strings.Contains(strings.ToLower(res.stderr+res.stdout), releaseNotFound) {
		return false, nil
	}
	return false, failed(args, res)
}

// ReleaseBody is the body of the repository's Release for tag. A missing
// Release is an error.
func (c Client) ReleaseBody(repo Repository, tag string) (string, error) {
	return c.output(readTimeout, "release", "view", tag, "--repo", repo.String(), "--json", "body", "--jq", ".body")
}

// LatestReleaseTag is the tag of the repository's "Latest" Release; ok is
// false when the repository has none. Only gh's own "release not found" is
// read as none.
func (c Client) LatestReleaseTag(repo Repository) (tag string, ok bool, err error) {
	args := []string{"release", "view", "--repo", repo.String(), "--json", "tagName", "--jq", ".tagName"}
	res, err := c.read(readTimeout, args...)
	if err != nil {
		return "", false, err
	}
	if res.code != 0 {
		if strings.Contains(strings.ToLower(res.stderr+res.stdout), releaseNotFound) {
			return "", false, nil
		}
		return "", false, failed(args, res)
	}
	tag = strings.TrimSpace(res.stdout)
	if tag == "" {
		return "", false, fmt.Errorf("gh named no tag for the Latest release of %s", repo)
	}
	return tag, true, nil
}

// ReleaseTags is every tag carrying a GitHub Release in the repository, in
// one listing. A listing that comes back at ReleaseListLimit entries is
// refused: it may be truncated, and every unlisted Release would be judged
// absent.
func (c Client) ReleaseTags(repo Repository) ([]string, error) {
	out, err := c.output(listTimeout, "release", "list", "--repo", repo.String(), "--limit", strconv.Itoa(ReleaseListLimit), "--json", "tagName", "--jq", ".[].tagName")
	if err != nil {
		return nil, fmt.Errorf("the GitHub Releases of %s could not be listed: %w", repo, err)
	}
	var tags []string
	for _, line := range lines(out) {
		tags = append(tags, strings.TrimSpace(line))
	}
	if len(tags) >= ReleaseListLimit {
		return nil, fmt.Errorf("the GitHub Release listing of %s came back at its %d-entry limit, so it may be truncated: `gh release list` reports no total and offers no pagination, and every unlisted Release would be judged absent. Raise github.ReleaseListLimit above the repository's Release count", repo, ReleaseListLimit)
	}
	return tags, nil
}

// NewRelease is one Release to create.
type NewRelease struct {
	Tag   string
	Title string
	// Notes is the whole body, marker and notices included.
	Notes      string
	Prerelease bool
	// MovesLatest states whether this Release may take the repository's
	// "Latest" badge: true for a release, false for a repair re-creating an
	// old Release, so the badge never moves onto a version that is not the
	// newest.
	MovesLatest bool
}

// CreateRelease creates a Release with --verify-tag, so gh refuses instead
// of creating a missing tag at the default branch's head. The notes reach gh
// on its standard input.
func (c Client) CreateRelease(repo Repository, r NewRelease, extra ...strictcli.EffectOption) error {
	if r.Tag == "" || r.Title == "" {
		return errors.New("a Release to create needs a tag and a title")
	}
	args := []string{"release", "create", r.Tag, "--repo", repo.String(), "--title", r.Title, "--notes-file", "-", "--verify-tag"}
	if r.Prerelease {
		args = append(args, "--prerelease")
	}
	if !r.MovesLatest {
		args = append(args, "--latest=false")
	}
	return c.write([]byte(r.Notes), extra, args...)
}

// EditReleaseNotes replaces the notes of the Release for tag, leaving its
// title and flags as they are.
func (c Client) EditReleaseNotes(repo Repository, tag, notes string, extra ...strictcli.EffectOption) error {
	return c.write([]byte(notes), extra, "release", "edit", tag, "--repo", repo.String(), "--notes-file", "-")
}

// ReleaseDocument is the whole of a Release document an edit states.
type ReleaseDocument struct {
	Notes string
	// Title, when not empty, replaces the title; empty keeps it.
	Title      string
	Prerelease bool
}

// RewriteRelease rewrites the Release for tag: its notes, its title when one
// is given, and its pre-release flag, stated in both directions so the flag
// GitHub ends up carrying is decided here rather than inherited.
func (c Client) RewriteRelease(repo Repository, tag string, doc ReleaseDocument, extra ...strictcli.EffectOption) error {
	args := []string{"release", "edit", tag, "--repo", repo.String(), "--notes-file", "-"}
	if doc.Title != "" {
		args = append(args, "--title", doc.Title)
	}
	if doc.Prerelease {
		args = append(args, "--prerelease")
	} else {
		args = append(args, "--prerelease=false")
	}
	return c.write([]byte(doc.Notes), extra, args...)
}

// DeleteRelease deletes the Release for tag. The tag itself is left alone.
func (c Client) DeleteRelease(repo Repository, tag string, extra ...strictcli.EffectOption) error {
	return c.write(nil, extra, "release", "delete", tag, "--repo", repo.String(), "--yes")
}

// The released-commit marker: the publish workflow's only precise statement
// of which commit CI must be green on, on a line of its own so a repair
// replaces it rather than appending a second one.
var ciSHAMarker = regexp.MustCompile(`(?m)^<!-- rlsbl-ci-sha: ([0-9a-f]{40}) -->\n?`)

var fullSHA = regexp.MustCompile(`^[0-9a-f]{40}$`)

// CISHAMarker is the marker line naming the commit a release's CI verified.
// Anything but a full 40-digit lowercase commit id is refused.
func CISHAMarker(sha string) (string, error) {
	sha = strings.TrimSpace(sha)
	if !fullSHA.MatchString(sha) {
		return "", fmt.Errorf("the released-commit marker needs a full 40-digit commit id, not %q", sha)
	}
	return "<!-- rlsbl-ci-sha: " + sha + " -->", nil
}

// StripCISHAMarker is body with every released-commit marker line removed.
func StripCISHAMarker(body string) string {
	return ciSHAMarker.ReplaceAllString(body, "")
}

// CISHAFromBody is the commit the first released-commit marker in body
// names; ok is false when body carries none.
func CISHAFromBody(body string) (sha string, ok bool) {
	m := ciSHAMarker.FindStringSubmatch(body)
	if m == nil {
		return "", false
	}
	return m[1], true
}
