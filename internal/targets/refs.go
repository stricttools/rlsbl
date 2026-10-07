package targets

import (
	"slices"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

// ExpectedRefs is every git ref one released version owns, the one answer
// the release's tag step, the unpublished-refs check, undo, and reconcile
// share, so a ref the release creates is never one a check does not look
// for. The groups fail differently and are kept apart: a missing primary
// means the release never tagged, a missing companion means an ecosystem
// cannot resolve a module, and a missing alias means a recorded fact has no
// ref behind it.
type ExpectedRefs struct {
	Version semver.Version
	// Primary is the tag the release is named after: the archive's shipped
	// spelling when it records one, otherwise the releasable's tag format
	// rendered at the version.
	Primary string
	// Companions are the tags the members' ecosystems need (a Go module
	// below the root resolves at path/vX.Y.Z).
	Companions []string
	// Aliases are other spellings the repository's records attribute to
	// the version.
	Aliases []string
	// ShippedAs is the archive's shipped spelling, or empty. When set it is
	// also Primary; it is stated apart because it predates the release that
	// created the other refs, and undo leaves it where it stands.
	ShippedAs string
	// SchemeSpelling is the current tag format's spelling of a version that
	// shipped under another one, when no group names it. Nothing is owed
	// under it, so it is not in Tags, but a tag of that spelling is a name of
	// the version.
	SchemeSpelling string
}

// Tags are the refs owed, primary first, then companions, then aliases,
// each once.
func (r ExpectedRefs) Tags() []string {
	var out []string
	for _, tag := range append(append([]string{r.Primary}, r.Companions...), r.Aliases...) {
		if tag != "" && !slices.Contains(out, tag) {
			out = append(out, tag)
		}
	}
	return out
}

// Spellings are every tag name the version is addressable under: Tags, then
// SchemeSpelling.
func (r ExpectedRefs) Spellings() []string {
	tags := r.Tags()
	if r.SchemeSpelling != "" && !slices.Contains(tags, r.SchemeSpelling) {
		tags = append(tags, r.SchemeSpelling)
	}
	return tags
}

// RefMember is one member of the releasable being released, as the
// companion tags see it.
type RefMember struct {
	// Path is repository-relative, "." for the root member.
	Path string
	// Targets are the member's targets, declared or detected.
	Targets []declarations.Target
	// Publishes is false for a member whose releasable publishes nothing
	// (publish mode none): there is no proxy to satisfy for it.
	Publishes bool
}

// RefInputs are what a version's refs derive from besides the version.
type RefInputs struct {
	// SchemeTag is the releasable's tag format rendered at the version.
	SchemeTag string
	// Members are the releasable's members.
	Members []RefMember
	// RecordedPaths are the member paths a released version's archive
	// recorded tree hashes for: such a version owes the companions of those
	// members only, so a member added after it is never owed a tag at a
	// commit that predates it. Nil for a version with no archive yet (the
	// release being made), which owes every member's.
	RecordedPaths []string
	// ShippedAs is the spelling the version's archive records it shipped
	// under, or empty.
	ShippedAs string
	// Aliases are the other spellings the repository's records attribute to
	// the version.
	Aliases []string
}

// ExpectedRefsOf derives the refs version owns.
func ExpectedRefsOf(version semver.Version, in RefInputs) (ExpectedRefs, error) {
	primary := in.SchemeTag
	if in.ShippedAs != "" {
		primary = in.ShippedAs
	}
	var companions []string
	for _, m := range in.Members {
		if !m.Publishes || (in.RecordedPaths != nil && !slices.Contains(in.RecordedPaths, m.Path)) {
			continue
		}
		for _, t := range m.Targets {
			target, err := Get(t.Name)
			if err != nil {
				return ExpectedRefs{}, err
			}
			for _, tag := range target.CompanionTags(m.Path, version) {
				if tag != primary && !slices.Contains(companions, tag) {
					companions = append(companions, tag)
				}
			}
		}
	}
	var aliases []string
	for _, tag := range append(append([]string(nil), in.Aliases...), in.ShippedAs) {
		if tag != "" && !slices.Contains(aliases, tag) {
			aliases = append(aliases, tag)
		}
	}
	refs := ExpectedRefs{Version: version, Primary: primary, Companions: companions, Aliases: aliases, ShippedAs: in.ShippedAs}
	if !slices.Contains(refs.Tags(), in.SchemeTag) {
		refs.SchemeSpelling = in.SchemeTag
	}
	return refs, nil
}
