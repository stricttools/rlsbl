package releaserecord

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

// A repository's tag namespace is writable by anything, and the backfill and
// reconcile walk it deciding, per tag, whether the repository accounts for
// it. This is the one answer both consult; they differ only in what they do
// with a tag nothing explains (the backfill refuses, reconcile trips).

// ExplanationSource is why a tag is accounted for.
type ExplanationSource string

// The explanations, in the order they take precedence.
const (
	// SourceArchivedVersion: the tag is one of the refs a version the
	// archives record owns, as the caller's mapping says.
	SourceArchivedVersion ExplanationSource = "archived-version"
	// SourceShippedAs: an archive records the tag as the one its version
	// shipped under.
	SourceShippedAs ExplanationSource = "shipped-as"
	// SourceUnversionedTag: the lifecycle-and-license record lists the tag
	// among its unversioned tags, outside the version model on purpose.
	SourceUnversionedTag ExplanationSource = "unversioned-tag"
	// SourceRetiredIdentity: the tag was created while a closed identity of
	// the lifecycle-and-license record owned it. A dead identity's tags are
	// accounted for by its record, never scrubbed and never refused.
	SourceRetiredIdentity ExplanationSource = "retired-identity"
)

// Explanation is why one tag is accounted for.
type Explanation struct {
	Tag    string
	Source ExplanationSource
	// Version is the version an archive-backed tag belongs to.
	Version semver.Version
	// Reason is the unversioned tag's reason.
	Reason string
	// Identity is the closed identity that owned the tag.
	Identity lifecycle.Identity
}

// Describe is one line naming the explanation, for a plan or an error.
func (x Explanation) Describe() string {
	switch x.Source {
	case SourceArchivedVersion:
		return "the archived version " + x.Version.String()
	case SourceShippedAs:
		return fmt.Sprintf("the archived version %s, which records shipped_as = %q", x.Version, x.Tag)
	case SourceUnversionedTag:
		return "listed among the unversioned tags of the lifecycle-and-license record: " + x.Reason
	}
	return fmt.Sprintf("created while the %s identity %q of %q owned it (until %s)", x.Identity.Facet, x.Identity.Value, x.Identity.Subject, x.Identity.Until.Format(time.DateOnly))
}

// Explanations is what accounts for the tags of one repository.
type Explanations struct {
	byTag  map[string]Explanation
	record *lifecycle.Record
}

// ArchiveDirs lists every directory holding archives in the repository at
// root, repository-relative: each releasable's and each retired subject's,
// in path order. Only directories holding an archive are listed.
func ArchiveDirs(root string) ([]string, error) {
	var dirs []string
	for _, parent := range []struct{ base, suffix string }{
		{declarations.ReleasesRoot, ""},
		{declarations.RetiredHistoriesRoot, "/releases"},
	} {
		entries, err := os.ReadDir(filepath.Join(root, filepath.FromSlash(parent.base)))
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("listing %s: %w", parent.base, err)
		}
		for _, entry := range entries {
			if !entry.IsDir() {
				continue
			}
			dir := parent.base + "/" + entry.Name() + parent.suffix
			versions, err := ArchivedVersions(root, dir)
			if err != nil {
				return nil, err
			}
			if len(versions) > 0 {
				dirs = append(dirs, dir)
			}
		}
	}
	sort.Strings(dirs)
	return dirs, nil
}

// BuildExplanations assembles what accounts for the tags of the repository
// at root. versionTags is the caller's own tag-to-version mapping of every
// tag the archived versions own (reconcile and the backfill derive it from
// the one authority for a version's refs); every archive in archiveDirs is
// read for its shipped_as, strictly, since an archive rlsbl cannot read
// cannot be said to account for nothing; record supplies the unversioned
// tags and the identities.
func BuildExplanations(root string, versionTags map[string]semver.Version, archiveDirs []string, record *lifecycle.Record) (*Explanations, error) {
	x := &Explanations{byTag: map[string]Explanation{}, record: record}
	for _, u := range record.UnversionedTags() {
		x.byTag[u.Tag] = Explanation{Tag: u.Tag, Source: SourceUnversionedTag, Reason: u.Reason}
	}
	for _, dir := range archiveDirs {
		versions, err := ArchivedVersions(root, dir)
		if err != nil {
			return nil, err
		}
		for _, v := range versions {
			a, err := ReadArchive(root, dir, v)
			if err != nil {
				return nil, err
			}
			if a.ShippedAs != "" {
				x.byTag[a.ShippedAs] = Explanation{Tag: a.ShippedAs, Source: SourceShippedAs, Version: v}
			}
		}
	}
	for tag, v := range versionTags {
		x.byTag[tag] = Explanation{Tag: tag, Source: SourceArchivedVersion, Version: v}
	}
	return x, nil
}

// Explain is the explanation of tag, created at created (its tagger date, or
// for a lightweight tag its commit's committer date), and false when nothing
// accounts for it. Only a closed identity explains a tag: a tag created
// while an open identity owns its pattern is a live release's tag, which an
// archive accounts for or nothing does. Identities of two subjects owning
// one tag are the lifecycle library's refusal.
func (x *Explanations) Explain(tag string, created time.Time) (Explanation, bool, error) {
	if found, ok := x.byTag[tag]; ok {
		return found, true, nil
	}
	owner, ok, err := x.record.TagOwner(tag, created)
	if err != nil || !ok || owner.Open() {
		return Explanation{}, false, err
	}
	return Explanation{Tag: tag, Source: SourceRetiredIdentity, Identity: owner}, true, nil
}

// UnversionedTags lists the tags the lifecycle-and-license record puts
// outside the version model and no archive claims, in order.
func (x *Explanations) UnversionedTags() []string {
	var tags []string
	for tag, found := range x.byTag {
		if found.Source == SourceUnversionedTag {
			tags = append(tags, tag)
		}
	}
	sort.Strings(tags)
	return tags
}
