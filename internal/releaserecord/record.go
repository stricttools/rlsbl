package releaserecord

import (
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The record answers two questions differently, because they are different
// questions:
//
//   - What bounds the unreleased range? The highest archived release whose
//     release commit this checkout contains (Nearest). A release that is not
//     in this history cannot bound a range computed from it.
//   - What is the latest release? The highest archived release, whether or
//     not the checkout contains it (Latest), with the discrepancy stated
//     rather than rewritten into a fact about the checkout.
//
// Reading walks the archives highest first and stops at the first answer, so
// the ordinary case (a checkout containing the latest release) opens one
// archive.
//
// The release backfill and reconcile read archives and tags without these
// guarded reads (ReadArchive, ArchivedVersions), so they run on the very
// repository the guards refuse.

// ReadErrorReason is which of the record's read errors a ReadError is.
type ReadErrorReason string

// The read errors. Each fires where the record is read for use, never while
// the archives are listed.
const (
	// ReasonDisagreement: the version's tag exists locally and points at a
	// commit other than the archive's release commit.
	ReasonDisagreement ReadErrorReason = "disagreement"
	// ReasonIndeterminable: git cannot say whether a release commit is in
	// this checkout's history (a missing object, a shallow history).
	ReasonIndeterminable ReadErrorReason = "indeterminable"
	// ReasonNoFate: an archive states none of the three fates.
	ReasonNoFate ReadErrorReason = "no-fate"
	// ReasonUnbackfilled: no archive at all, yet tags of the releasable's
	// scheme exist: a releasable that has released and was never
	// backfilled.
	ReasonUnbackfilled ReadErrorReason = "unbackfilled"
	// ReasonLatestNotInCheckout: a release is prepared on a history that
	// does not contain the latest release.
	ReasonLatestNotInCheckout ReadErrorReason = "latest-not-in-checkout"
)

// ReadError is one of the record's read errors, with the message naming
// what was found and how to clear it.
type ReadError struct {
	Reason  ReadErrorReason
	Message string
}

func (e *ReadError) Error() string { return e.Message }

// Record is one releasable's release record, read in its repository.
type Record struct {
	repo       git.Repo
	releasable string
	dir        string
	scheme     workspace.TagScheme
	upstream   string
}

// New is the release record of releasable in repo, whose tags follow scheme.
// upstream is the URL of the repository this one is a fork of, as its
// declared upstream names it, and empty for a repository that is no fork:
// the record names it when tags may be the upstream's.
func New(repo git.Repo, releasable string, scheme workspace.TagScheme, upstream string) *Record {
	return &Record{repo: repo, releasable: releasable, dir: ArchiveDir(releasable), scheme: scheme, upstream: upstream}
}

// Releasable is the releasable the record is of.
func (r *Record) Releasable() string { return r.releasable }

// Dir is the record's repository-relative archive directory.
func (r *Record) Dir() string { return r.dir }

// Scheme is the releasable's tag scheme.
func (r *Record) Scheme() workspace.TagScheme { return r.scheme }

func (r *Record) root() string { return r.repo.Dir() }

// Entry is one archived release read for use, in one of the three fates.
type Entry struct {
	Version semver.Version
	// Path is the archive's repository-relative path.
	Path string
	Fate Fate
	// ReleaseCommit is the commit a recorded release shipped from, and
	// empty in the other fates.
	ReleaseCommit string
	// ShippedAs is the tag the version shipped under when the archive
	// records one; never set on a never-released entry.
	ShippedAs string
}

// Tag is the tag the release carries: the one it shipped under when the
// archive records it, otherwise the scheme's.
func (e Entry) Tag(scheme workspace.TagScheme) string {
	if e.ShippedAs != "" {
		return e.ShippedAs
	}
	return scheme.Render(e.Version)
}

// sameCommit reports whether two commit ids name the same commit, allowing
// one to be abbreviated.
func sameCommit(a, b string) bool {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	return n > 0 && a[:n] == b[:n]
}

// Versions lists the record's archived versions, highest first.
func (r *Record) Versions() ([]semver.Version, error) {
	return ArchivedVersions(r.root(), r.dir)
}

// Fate is what the record says about v: FateAbsent when there is no archive
// of it, otherwise the fate the archive states. An archive stating none is
// the no-fate read error. Every question of the form "was this version
// released?" asks here.
func (r *Record) Fate(v semver.Version) (Fate, error) {
	found, err := exists(r.root(), ArchivePath(r.dir, v))
	if err != nil {
		return "", err
	}
	if !found {
		return FateAbsent, nil
	}
	a, err := ReadArchive(r.root(), r.dir, v)
	if err != nil {
		return "", err
	}
	if a.Fate == FateUnstated {
		return "", r.noFateError(a)
	}
	return a.Fate, nil
}

// Entry reads the archive of v for use. An archive stating no fate, and a
// recorded release whose local tag points elsewhere than its release commit,
// are read errors; a missing archive is an error, since callers reach here
// from the listing.
func (r *Record) Entry(v semver.Version) (Entry, error) {
	a, err := ReadArchive(r.root(), r.dir, v)
	if err != nil {
		return Entry{}, err
	}
	switch a.Fate {
	case FateUnstated:
		return Entry{}, r.noFateError(a)
	case FateNeverReleased:
		return Entry{Version: v, Path: a.Path, Fate: FateNeverReleased}, nil
	case FateUnrecoverable:
		return Entry{Version: v, Path: a.Path, Fate: FateUnrecoverable, ShippedAs: a.ShippedAs}, nil
	}
	entry := Entry{Version: v, Path: a.Path, Fate: FateRecorded, ReleaseCommit: a.ReleaseCommit.Commit, ShippedAs: a.ShippedAs}
	tag := entry.Tag(r.scheme)
	tagCommit, found, err := r.repo.TagCommit(tag)
	if err != nil {
		return Entry{}, err
	}
	if found && !sameCommit(tagCommit, entry.ReleaseCommit) {
		return Entry{}, &ReadError{Reason: ReasonDisagreement, Message: fmt.Sprintf(
			"the release record and the git tag disagree about %s:\n"+
				"  tag %q points at        %s\n"+
				"  the archive's release commit is %s\n"+
				"  archive: %s\n"+
				"  The archive is the record the release flow itself wrote, and rlsbl rewrites an\n"+
				"  archive only through its own record writers, so a disagreement means the tag\n"+
				"  moved: a history rewrite that did not re-point it, or a hand-made tag on the\n"+
				"  wrong commit. Re-point the tag at the release commit\n"+
				"    git tag -f %s %s\n"+
				"  or, if the rewrite was intended, repair the release metadata from the rewrite\n"+
				"  journal with `rlsbl release reconcile --mode plan` and then `--mode apply`.\n"+
				"  rlsbl will not guess which of the two is right.",
			v, tag, tagCommit, entry.ReleaseCommit, a.Path, tag, entry.ReleaseCommit)}
	}
	return entry, nil
}

// noFateError is the read error for an archive stating no fate, with what
// the version's tag says as evidence of what the backfill will find.
func (r *Record) noFateError(a Archive) error {
	tag := r.scheme.Render(a.Version)
	if a.ShippedAs != "" {
		tag = a.ShippedAs
	}
	derived := fmt.Sprintf("  Its tag %q does not exist locally, so the backfill will look for the\n"+
		"  version-bump commit instead, and record the version unrecoverable only if\n"+
		"  that also fails. If the tag merely was not fetched, fetch it first\n"+
		"  (git fetch origin --tags).", tag)
	if commit, found, err := r.repo.TagCommit(tag); err != nil {
		return err
	} else if found {
		derived = fmt.Sprintf("  Its tag %q points at %s, which is the commit the backfill will record.", tag, commit)
	}
	return &ReadError{Reason: ReasonNoFate, Message: fmt.Sprintf(
		"the release record entry for %s records no fate: %s\n"+
			"  An archive records one of three: the release commit the release flow wrote\n"+
			"  (release_commit and [released_trees]), unrecoverable = true (it shipped, from a\n"+
			"  commit nothing can name), or never_released = true (the version number exists,\n"+
			"  no release does). This one records none of them, so it was written before\n"+
			"  release commits were recorded and was never backfilled, and rlsbl cannot tell\n"+
			"  which commit %s shipped from, or whether it shipped at all.\n"+
			"%s\n"+
			"  Backfill it, previewing first:\n"+
			"    rlsbl release backfill --dry-run\n"+
			"    rlsbl release backfill --approve-consequential\n"+
			"  If %s was never released, declare that first by adding never_released = true\n"+
			"  to the archive (it is read-only: chmod 644, add the line, chmod 444); the\n"+
			"  backfill leaves a declared fate alone.",
		a.Version, a.Path, a.Version, derived, a.Version)}
}

// tagEvidence is how many of the scheme's tags the unbackfilled error names.
const tagEvidence = 3

// schemeTags lists the local tags the releasable's scheme owns, highest
// version first.
func (r *Record) schemeTags() ([]string, error) {
	tags, err := r.repo.TagCommits()
	if err != nil {
		return nil, err
	}
	type owned struct {
		tag string
		v   semver.Version
	}
	var found []owned
	for tag := range tags {
		if v, ok := r.scheme.VersionOf(tag); ok {
			found = append(found, owned{tag, v})
		}
	}
	sort.Slice(found, func(i, j int) bool { return semver.Compare(found[i].v, found[j].v) > 0 })
	out := make([]string, len(found))
	for i, o := range found {
		out[i] = o.tag
	}
	return out, nil
}

// requireBackfilled refuses an empty record in a repository whose tags say
// the releasable has released: answering from the empty record would report
// its whole history as unreleased. A releasable that never released carries
// no tag of its scheme, and its empty record is the answer.
func (r *Record) requireBackfilled(versions []semver.Version) error {
	if len(versions) > 0 {
		return nil
	}
	tags, err := r.schemeTags()
	if err != nil {
		return err
	}
	if len(tags) == 0 {
		return nil
	}
	evidence := tags
	more := ""
	if len(evidence) > tagEvidence {
		evidence = evidence[:tagEvidence]
		more = " (and others)"
	}
	fork := ""
	if r.upstream != "" {
		fork = fmt.Sprintf("  This repository is a fork of %s: tags it inherited from upstream are\n"+
			"  upstream's releases, not its own, and a backfill would record them as this\n"+
			"  repository's. Move them out of refs/tags first, previewing first:\n"+
			"    rlsbl upstream adopt-tags --dry-run\n"+
			"    rlsbl upstream adopt-tags --approve-consequential\n"+
			"  and backfill only if tags remain after that.\n", r.upstream)
	}
	return &ReadError{Reason: ReasonUnbackfilled, Message: fmt.Sprintf(
		"the release record of %q is empty, but this repository has its version tags: %s\n"+
			"  No release archive exists there, so the record holds nothing, and yet the tag\n"+
			"  namespace carries tags of the releasable's scheme (%q), which is what a\n"+
			"  releasable that has released and was never backfilled looks like.\n"+
			"  Matching tags: %s%s\n"+
			"  Answering from an empty record would report the whole history as unreleased\n"+
			"  and widen every range computed from it, so rlsbl refuses instead.\n"+
			"%s"+
			"  Backfill the archives, previewing first:\n"+
			"    rlsbl release backfill --dry-run\n"+
			"    rlsbl release backfill --approve-consequential\n"+
			"  (the preview lists every tag the repository cannot account for, and the apply\n"+
			"  refuses while one remains.) A releasable that never released carries no tag of\n"+
			"  its scheme and is unaffected.",
		r.releasable, r.dir, r.scheme.Pattern(), strings.Join(evidence, ", "), more, fork)}
}

// indeterminable is the read error for an ancestry git cannot decide.
func indeterminable(e Entry, head string) error {
	return &ReadError{Reason: ReasonIndeterminable, Message: fmt.Sprintf(
		"cannot determine whether the released commit of %s is in this checkout's history:\n"+
			"  git could not answer whether %s is an ancestor of %s. The commit's objects\n"+
			"  are missing, or the history is truncated so the walk stops before reaching it.\n"+
			"  archive: %s\n"+
			"  This is not a no, and rlsbl will not read it as one: an unanswerable ancestry\n"+
			"  would widen every range computed from it. Deepen the repository and re-run:\n"+
			"    git fetch --unshallow          (a shallow clone)\n"+
			"    git fetch origin %s   (one missing commit)",
		e.Version, e.ReleaseCommit, head, e.Path, e.ReleaseCommit)}
}

// Nearest is the highest recorded release whose release commit head
// contains, and nil when the record holds none this checkout contains (a
// releasable before its first release, or a checkout older than every
// release). Unrecoverable and never-released versions are passed over: the
// one has no commit to bound with, the other was never a release. This is
// what bounds every unreleased range.
func (r *Record) Nearest(head string) (*Entry, error) {
	versions, err := r.Versions()
	if err != nil {
		return nil, err
	}
	if err := r.requireBackfilled(versions); err != nil {
		return nil, err
	}
	for _, v := range versions {
		entry, err := r.Entry(v)
		if err != nil {
			return nil, err
		}
		if entry.Fate != FateRecorded {
			continue
		}
		verdict, err := r.repo.Ancestry(entry.ReleaseCommit, head)
		if err != nil {
			return nil, err
		}
		switch verdict {
		case git.IsAncestor:
			return &entry, nil
		case git.AncestryIndeterminable:
			return nil, indeterminable(entry, head)
		}
	}
	return nil, nil
}

// AtCommit is the release sha is, and nil when sha shipped no version. It
// costs one archive read: the nearest release in sha's own history is sha
// itself when, and only when, sha is a release commit.
func (r *Record) AtCommit(sha string) (*Entry, error) {
	entry, err := r.Nearest(sha)
	if err != nil || entry == nil || !sameCommit(entry.ReleaseCommit, sha) {
		return nil, err
	}
	return entry, nil
}

// LatestFact is the releasable's latest release and whether a checkout
// contains it.
type LatestFact struct {
	// Released is false when the record holds no release.
	Released bool
	Version  semver.Version
	// Unrecoverable is true when the latest release is unrecoverable, and
	// InCheckout then says nothing.
	Unrecoverable bool
	// InCheckout is whether the checkout contains the latest release's
	// commit.
	InCheckout bool
	// NeverReleasedAbove are the never-released versions archived above the
	// latest release, highest first.
	NeverReleasedAbove []semver.Version
}

// State is the fate of the latest release for a machine reader:
// "recorded", "unrecoverable", or "" when there is no release.
func (f LatestFact) State() string {
	switch {
	case !f.Released:
		return ""
	case f.Unrecoverable:
		return string(FateUnrecoverable)
	}
	return string(FateRecorded)
}

// Label is the display of the fact: the version, annotated when the checkout
// lacks it or its commit is unrecoverable, "(none)" when nothing was
// released, and naming the never-released versions archived above it, so
// the display does not look stale against the highest archive.
func (f LatestFact) Label() string {
	base := "(none)"
	var notes []string
	if f.Released {
		base = f.Version.String()
		switch {
		case f.Unrecoverable:
			notes = append(notes, "commit not recoverable")
		case !f.InCheckout:
			notes = append(notes, "not in this checkout's history")
		}
	}
	if len(f.NeverReleasedAbove) > 0 {
		names := make([]string, len(f.NeverReleasedAbove))
		for i, v := range f.NeverReleasedAbove {
			names[i] = v.String()
		}
		notes = append(notes, strings.Join(names, ", ")+" archived but never released")
	}
	if len(notes) == 0 {
		return base
	}
	return base + " (" + strings.Join(notes, "; ") + ")"
}

// Latest is the releasable's latest release: the highest archived version
// that was released, whether or not head contains it, with the
// never-released versions above it named. An empty record in a repository
// carrying the scheme's tags is the unbackfilled read error.
func (r *Record) Latest(head string) (LatestFact, error) {
	versions, err := r.Versions()
	if err != nil {
		return LatestFact{}, err
	}
	if err := r.requireBackfilled(versions); err != nil {
		return LatestFact{}, err
	}
	var phantoms []semver.Version
	for _, v := range versions {
		entry, err := r.Entry(v)
		if err != nil {
			return LatestFact{}, err
		}
		switch entry.Fate {
		case FateNeverReleased:
			phantoms = append(phantoms, v)
			continue
		case FateUnrecoverable:
			return LatestFact{Released: true, Version: v, Unrecoverable: true, NeverReleasedAbove: phantoms}, nil
		}
		verdict, err := r.repo.Ancestry(entry.ReleaseCommit, head)
		if err != nil {
			return LatestFact{}, err
		}
		if verdict == git.AncestryIndeterminable {
			return LatestFact{}, indeterminable(entry, head)
		}
		return LatestFact{Released: true, Version: v, InCheckout: verdict == git.IsAncestor, NeverReleasedAbove: phantoms}, nil
	}
	return LatestFact{NeverReleasedAbove: phantoms}, nil
}

// RequireCheckoutContainsLatest refuses to prepare a release on a history
// that lacks the latest release: the new release would revert it, and its
// changelog range would cover commits that already shipped. Nothing released
// and an unrecoverable latest release (no commit to require) pass. It
// returns the fact it judged, for the version decision.
func (r *Record) RequireCheckoutContainsLatest(head string) (LatestFact, error) {
	fact, err := r.Latest(head)
	if err != nil {
		return LatestFact{}, err
	}
	if !fact.Released || fact.Unrecoverable || fact.InCheckout {
		return fact, nil
	}
	entry, err := r.Entry(fact.Version)
	if err != nil {
		return LatestFact{}, err
	}
	return LatestFact{}, &ReadError{Reason: ReasonLatestNotInCheckout, Message: fmt.Sprintf(
		"this checkout does not contain the latest release, %s.\n"+
			"  Its released commit %s is not an ancestor of %s.\n"+
			"  archive: %s\n"+
			"  Releasing from here would ship a history the previous release is not in,\n"+
			"  reverting it, and the new version's changelog range would cover commits that\n"+
			"  already shipped. Bring the checkout up to date first (git pull, or check out\n"+
			"  the release branch), then re-run the release.",
		fact.Version, entry.ReleaseCommit, head, entry.Path)}
}

// LatestReleasedVersion is the highest version the archives in dir
// (repository-relative) record as released, and false when none is. It
// reads only the archives: no git, no network. An archive stating no fate is
// an error naming it, since whether it was released cannot be told.
func LatestReleasedVersion(root, dir string) (semver.Version, bool, error) {
	versions, err := ArchivedVersions(root, dir)
	if err != nil {
		return semver.Version{}, false, err
	}
	for _, v := range versions {
		a, err := ReadArchive(root, dir, v)
		if err != nil {
			return semver.Version{}, false, err
		}
		switch a.Fate {
		case FateNeverReleased:
			continue
		case FateUnstated:
			return semver.Version{}, false, fmt.Errorf("%s records no fate, so whether %s was released cannot be told; run `rlsbl release backfill --dry-run` to see how it would be recorded", a.Path, v)
		}
		return v, true, nil
	}
	return semver.Version{}, false, nil
}
