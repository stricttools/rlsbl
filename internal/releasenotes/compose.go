// Package releasenotes decides what one released version's GitHub Release
// carries and writes it there. The body is the version's deprecate and yank
// notices (the archive's release_notices), each followed by a blank line,
// then the version's notes (its changelog section, composed from its
// released changelog file and its archive's prose), then the rlsbl-ci-sha
// marker naming the release commit the archive records. The title is the
// tag, and the pre-release flag is set exactly when the version carries a
// notice, the way deprecate and yank mark it.
//
// The release, the commands on past releases, and the history rewrites all
// compose through Read and write through Create, Rewrite, EnsureMarker, or
// Publish. A Release is created once and edited in place afterwards; nothing
// here deletes a Release.
package releasenotes

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// blockSeparator separates one block of a body from the next: a notice from
// the notice below it, and the last notice from the notes.
const blockSeparator = "\n\n"

// notesDepth is the heading depth of the changelog section the notes are
// taken from: a releasable's own CHANGELOG.md, where versions are "##".
const notesDepth = 2

// ComposeBody is the layout of every Release body: notices top to bottom,
// then rest. An empty rest leaves the notices standing alone. Deprecate and
// yank put their notice on top of the body GitHub holds with this layout, so
// a body composed from the record afterwards is the same document.
func ComposeBody(notices []string, rest string) string {
	blocks := append([]string(nil), notices...)
	if rest != "" {
		blocks = append(blocks, rest)
	}
	return strings.Join(blocks, blockSeparator)
}

// Document is the whole of what one version's GitHub Release carries.
type Document struct {
	// Tag is the tag the Release is attached to: the tag the version
	// shipped under.
	Tag     string
	Version semver.Version
	// Notes is the version's changelog section without its heading; empty
	// notes make the body name the version instead.
	Notes string
	// Notices are the version's deprecate and yank notices, top to bottom.
	Notices []string
	// ReleaseCommit is the commit the version shipped from, which the marker
	// names, and empty when the record names none (a version recorded
	// unrecoverable). A marker is never invented for a commit nothing names.
	ReleaseCommit string
}

// Title is the Release title: the tag, as the release has always written it.
func (d Document) Title() string { return d.Tag }

// Prerelease reports whether the Release is marked pre-release: when the
// version carries a deprecate or yank notice, as those commands mark it.
// rlsbl releases no pre-release versions, so nothing else marks one.
func (d Document) Prerelease() bool { return len(d.Notices) > 0 }

// Marker is the marker line naming the release commit. A document naming no
// release commit has none, and asking for it is an error.
func (d Document) Marker() (string, error) {
	if d.ReleaseCommit == "" {
		return "", fmt.Errorf("the Release of %s names no release commit: the record holds none for %s, so its body carries no rlsbl-ci-sha marker", d.Tag, d.Version)
	}
	marker, err := github.CISHAMarker(d.ReleaseCommit)
	if err != nil {
		return "", fmt.Errorf("the Release of %s: %w", d.Tag, err)
	}
	return marker, nil
}

// Body is the Release body: the notices, the notes, a blank line, and the
// marker, or the notices and the notes alone for a document naming no
// release commit.
func (d Document) Body() (string, error) {
	notes := strings.TrimRight(d.Notes, "\n")
	if strings.TrimSpace(notes) == "" {
		notes = "Release " + d.Version.String()
	}
	if d.ReleaseCommit == "" {
		return ComposeBody(d.Notices, notes+"\n"), nil
	}
	marker, err := d.Marker()
	if err != nil {
		return "", err
	}
	return ComposeBody(d.Notices, notes+"\n\n"+marker+"\n"), nil
}

// KeepingMarkerOf is d with the release commit existing's marker names, for
// a document whose record names none: a body GitHub holds keeps the marker
// it carries when the record cannot say otherwise. A document naming a
// release commit is returned as it is, since the record outranks whatever a
// body says.
func (d Document) KeepingMarkerOf(existing string) Document {
	if d.ReleaseCommit != "" {
		return d
	}
	if sha, ok := github.CISHAFromBody(existing); ok {
		d.ReleaseCommit = sha
	}
	return d
}

// WithMarker is existing with marker on it, and changed false when existing
// already carries that marker, so nothing needs writing. A body carrying a
// different marker has it replaced, never a second one appended.
func WithMarker(existing, marker string) (body string, changed bool) {
	if strings.Contains(existing, marker) {
		return existing, false
	}
	stripped := strings.TrimRight(github.StripCISHAMarker(existing), "\n")
	if stripped == "" {
		return marker + "\n", true
	}
	return stripped + "\n\n" + marker + "\n", true
}

// Read composes the Document of released version v of releasable from the
// repository rooted at root (absolute), whose tags follow scheme: the notes
// from v's released changelog file and its archive's prose, the notices and
// the release commit from its archive, and the tag from the archive's
// shipped_as or else the scheme. The archive is read directly rather than
// through the release record's guarded entry: a repair asks here exactly
// when the tag and the release commit disagree. A version with no archive,
// one whose archive states no fate, and one never released are refused,
// each naming why.
func Read(root, releasable string, scheme workspace.TagScheme, v semver.Version) (Document, error) {
	doc, err := ReadArchived(root, releasable, scheme, v)
	if err != nil {
		return Document{}, err
	}
	section, err := changelog.VersionSection(root, releasable, v, notesDepth)
	if err != nil {
		return Document{}, err
	}
	_, body, _ := strings.Cut(section, "\n")
	doc.Notes = strings.TrimSpace(body)
	return doc, nil
}

// ReadArchived is Read without the notes: the tag, the release commit, and
// the notices of released version v from its archive alone, for a version
// released before its releasable kept a changelog file, whose notes the
// record does not hold. It refuses what Read refuses about the archive.
func ReadArchived(root, releasable string, scheme workspace.TagScheme, v semver.Version) (Document, error) {
	dir := releaserecord.ArchiveDir(releasable)
	rel := releaserecord.ArchivePath(dir, v)
	if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(rel))); errors.Is(err, fs.ErrNotExist) {
		return Document{}, fmt.Errorf("%s of %s has no release archive at %s: a GitHub Release is composed from the version's archive (its release commit and its notices), so there is nothing to compose it from. Backfill the archives, previewing first: `rlsbl release backfill --dry-run`, then `rlsbl release backfill --approve-consequential`", v, releasable, rel)
	} else if err != nil {
		return Document{}, fmt.Errorf("reading %s: %w", rel, err)
	}
	a, err := releaserecord.ReadArchive(root, dir, v)
	if err != nil {
		return Document{}, err
	}
	doc := Document{Version: v, Tag: scheme.Render(v), Notices: append([]string(nil), a.ReleaseNotices...)}
	if a.ShippedAs != "" {
		doc.Tag = a.ShippedAs
	}
	switch a.Fate {
	case releaserecord.FateUnstated:
		return Document{}, fmt.Errorf("%s states no fate (recorded, unrecoverable, or never released), so whether %s shipped, and from which commit, cannot be told. Backfill it, previewing first: `rlsbl release backfill --dry-run`, then `rlsbl release backfill --approve-consequential`", rel, v)
	case releaserecord.FateNeverReleased:
		return Document{}, fmt.Errorf("%s records that no release was ever published under %s (never_released = true), so there is no GitHub Release to compose for it", rel, v)
	case releaserecord.FateRecorded:
		doc.ReleaseCommit = a.ReleaseCommit.Commit
	}
	return doc, nil
}
