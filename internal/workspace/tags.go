package workspace

import (
	"fmt"
	"strings"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

// TagScheme is one releasable's tag namespace and the one rule deciding
// what is in it. Its pattern is the tag format with the name filled in and
// the version left as {version} ("cmd/portal/v{version}",
// "portal@v{version}", "v{version}"). A tag belongs to the scheme when, and
// only when, it is the pattern rendered at the version it carries:
// "kernel/v*" also lists "kernel/vulkan/v0.1.0", but no version renders
// "kernel/v{version}" as that tag, so it is not kernel's. A glob only lists
// (ListGlob); ownership is always Owns.
type TagScheme struct {
	prefix, suffix string
}

// NewTagScheme is the scheme of a pattern holding one {version} and no
// other placeholder.
func NewTagScheme(pattern string) (TagScheme, error) {
	if n := strings.Count(pattern, "{version}"); n != 1 {
		return TagScheme{}, fmt.Errorf("a tag scheme has one {version} placeholder; %q has %d", pattern, n)
	}
	prefix, suffix, _ := strings.Cut(pattern, "{version}")
	if strings.ContainsAny(prefix+suffix, "{}") {
		return TagScheme{}, fmt.Errorf("the tag scheme %q holds a placeholder other than {version}", pattern)
	}
	return TagScheme{prefix: prefix, suffix: suffix}, nil
}

// SchemeOf is a releasable's tag scheme: its tag format with {name} filled
// in.
func SchemeOf(r declarations.Releasable) (TagScheme, error) {
	return NewTagScheme(strings.ReplaceAll(r.TagFormat, "{name}", r.Name))
}

// SchemeFromGlob is the scheme a listing glob was made from. Every glob
// rlsbl makes is a pattern with {version} replaced by '*', so a glob holding
// one '*' and no other glob character names one scheme; anything else is
// refused rather than read as a guess.
func SchemeFromGlob(glob string) (TagScheme, error) {
	if strings.Count(glob, "*") != 1 || strings.ContainsAny(glob, "?[") {
		return TagScheme{}, fmt.Errorf("the tag glob %q does not name one tag scheme: it must hold one '*' (the version) and no other glob character", glob)
	}
	return NewTagScheme(strings.Replace(glob, "*", "{version}", 1))
}

// Pattern is the scheme's pattern.
func (s TagScheme) Pattern() string { return s.prefix + "{version}" + s.suffix }

// Render is the tag the scheme gives version.
func (s TagScheme) Render(version semver.Version) string {
	return s.prefix + version.String() + s.suffix
}

// ListGlob is a git tag listing pattern matching a superset of the scheme's
// tags.
func (s TagScheme) ListGlob() string { return s.prefix + "*" + s.suffix }

// VersionOf is the version tag carries under the scheme, and false when tag
// is not the scheme's: not the pattern around a MAJOR.MINOR.PATCH version.
func (s TagScheme) VersionOf(tag string) (semver.Version, bool) {
	if !strings.HasPrefix(tag, s.prefix) || !strings.HasSuffix(tag, s.suffix) || len(tag) < len(s.prefix)+len(s.suffix) {
		return semver.Version{}, false
	}
	middle := tag[len(s.prefix) : len(tag)-len(s.suffix)]
	v, err := semver.Parse(middle)
	if err != nil {
		return semver.Version{}, false
	}
	return v, true
}

// Owns reports whether tag is the scheme's rendering of some version.
func (s TagScheme) Owns(tag string) bool {
	_, ok := s.VersionOf(tag)
	return ok
}

// versionERE is a MAJOR.MINOR.PATCH version as semver.Parse reads it: three
// decimal components, none with a leading zero.
const versionERE = `(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)`

// Regexp is a POSIX extended regular expression (and a Go regular
// expression) matching the tags Owns accepts, anchored at both ends, for
// the places that judge a tag where this package cannot run (a workflow's
// shell step). It accepts a component too long for 64 bits, which VersionOf
// refuses.
func (s TagScheme) Regexp() string {
	return "^" + quoteERE(s.prefix) + versionERE + quoteERE(s.suffix) + "$"
}

// quoteERE escapes every character an extended regular expression gives a
// meaning.
func quoteERE(text string) string {
	var b strings.Builder
	for _, r := range text {
		if strings.ContainsRune(`\.+*?()|[]{}^$`, r) {
			b.WriteByte('\\')
		}
		b.WriteRune(r)
	}
	return b.String()
}

// IdentityTagOwner evaluates the lifecycle record's identity-owns-its-tags
// rule through the tag matcher: an identity owns tag when its period covers
// the date of created and one of its tag patterns, read as the scheme its
// glob was made from (SchemeFromGlob), owns tag. A glob only lists, so `v*`
// never owns `video-proc@v0.1.0`. Pending identities own nothing; a tag
// pattern that names no scheme is refused, and so are identities of two
// subjects owning one tag. When several identities of one subject own it,
// the first in record order is returned.
func IdentityTagOwner(rec *lifecycle.Record, tag string, created time.Time) (lifecycle.Identity, bool, error) {
	var found []lifecycle.Identity
	for _, id := range rec.Identities() {
		if id.Pending() || !id.Contains(created) {
			continue
		}
		for _, p := range id.TagPatterns {
			scheme, err := SchemeFromGlob(p)
			if err != nil {
				return lifecycle.Identity{}, false, fmt.Errorf("the %s identity %q of %q in %s: %w", id.Facet, id.Value, id.Subject, lifecycle.RecordFile, err)
			}
			if scheme.Owns(tag) {
				found = append(found, id)
				break
			}
		}
	}
	if len(found) == 0 {
		return lifecycle.Identity{}, false, nil
	}
	for _, id := range found[1:] {
		if id.Subject != found[0].Subject {
			return lifecycle.Identity{}, false, &lifecycle.Refusal{
				Rule:   lifecycle.RuleIdentityOwnsItsTags,
				Detail: fmt.Sprintf("tag %q created on %s is owned by the tag patterns of %q (%s) and of %q (%s), so its owner is ambiguous", tag, created.Format(time.DateOnly), found[0].Subject, found[0].Facet, id.Subject, id.Facet),
				Fix:    "Narrow the tag patterns of one of the identities so each tag has one owner.",
			}
		}
	}
	return found[0], true, nil
}

// TagStyle is which of the three shapes a version tag has.
type TagStyle string

// The version tag shapes.
const (
	// TagStyleStandalone is v1.2.3.
	TagStyleStandalone TagStyle = "standalone"
	// TagStyleMonorepo is name@v1.2.3.
	TagStyleMonorepo TagStyle = "monorepo"
	// TagStylePath is some/path/v1.2.3, the Go module proxy's.
	TagStylePath TagStyle = "path"
)

// ParseVersionTag reads tag as a version tag of one of the three shapes,
// matched against the whole tag, and is false for any other tag: a tag that
// is not a version ("latest"), a partial or pre-release version ("v1.2",
// "v1.2.3-rc.1"), or no separator before the "v".
func ParseVersionTag(tag string) (semver.Version, TagStyle, bool) {
	if rest, ok := strings.CutPrefix(tag, "v"); ok {
		if v, err := semver.Parse(rest); err == nil {
			return v, TagStyleStandalone, true
		}
	}
	for _, shape := range []struct {
		separator string
		style     TagStyle
	}{{"@v", TagStyleMonorepo}, {"/v", TagStylePath}} {
		i := strings.LastIndex(tag, shape.separator)
		if i <= 0 {
			continue
		}
		if v, err := semver.Parse(tag[i+len(shape.separator):]); err == nil {
			return v, shape.style, true
		}
	}
	return semver.Version{}, "", false
}

// TagOwner is the releasable whose scheme owns tag, and false when none
// does. Two releasables owning one tag is an error naming both: the tag's
// releasable cannot be guessed.
func (w *Workspace) TagOwner(tag string) (declarations.Releasable, bool, error) {
	var owners []declarations.Releasable
	for _, r := range w.Releasables() {
		scheme, err := SchemeOf(r)
		if err != nil {
			return declarations.Releasable{}, false, err
		}
		if scheme.Owns(tag) {
			owners = append(owners, r)
		}
	}
	switch len(owners) {
	case 0:
		return declarations.Releasable{}, false, nil
	case 1:
		return owners[0], true, nil
	default:
		names := make([]string, len(owners))
		for i, r := range owners {
			names[i] = r.Name
		}
		return declarations.Releasable{}, false, fmt.Errorf("the tag %s is rendered by the tag formats of the releasables %s alike; give them tag formats that render different tags", tag, strings.Join(names, ", "))
	}
}
