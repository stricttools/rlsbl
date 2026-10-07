package historyrewrite

import (
	"fmt"
	"slices"
	"sort"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// releasableRefs answers which refs each version of one releasable owns,
// through targets.ExpectedRefsOf, the one authority the release's tag step,
// the checks, undo, and these commands share.
type releasableRefs struct {
	// root is the repository root, absolute.
	root       string
	releasable declarations.Releasable
	scheme     workspace.TagScheme
	members    []targets.RefMember
	// membersErr is why the members' targets could not be read: their
	// companion tags cannot be derived, and a caller decides what that means.
	membersErr error
	// aliases maps a tag to the boundary aliases the transition record
	// attributes to it.
	aliases map[string][]string
	// goTarget is whether a member carries the go target, whose tags are the
	// published artifact itself.
	goTarget bool
}

// newReleasableRefs reads what the refs of releasable r derive from: its tag
// scheme, its members' targets, and the boundary aliases the transition
// record's events attribute to its tags.
func newReleasableRefs(ws *workspace.Workspace, events []releaserecord.Event, r declarations.Releasable) (*releasableRefs, error) {
	scheme, err := workspace.SchemeOf(r)
	if err != nil {
		return nil, err
	}
	x := &releasableRefs{root: ws.Root, releasable: r, scheme: scheme, aliases: map[string][]string{}}
	for _, m := range ws.MembersOf(r.Name) {
		declared, err := targets.MemberTargets(ws.Root, m)
		if err != nil {
			x.membersErr = fmt.Errorf("the targets of the member %q (path %q) cannot be read: %w", m.Name, m.Path, err)
			x.members = nil
			break
		}
		for _, t := range declared {
			if t.Name == declarations.TargetGo {
				x.goTarget = true
			}
		}
		x.members = append(x.members, targets.RefMember{Path: m.Path, Targets: declared, Publishes: r.PublishMode != declarations.PublishNone})
	}
	for _, e := range events {
		alias, ok := e.(*releaserecord.BoundaryAliasEvent)
		if !ok || (alias.Releasable != "" && alias.Releasable != r.Name) {
			continue
		}
		for _, a := range alias.Aliases {
			x.aliases[a.AliasedTag] = append(x.aliases[a.AliasedTag], a.AliasTag)
		}
	}
	return x, nil
}

// archiveDir is the releasable's archive directory.
func (x *releasableRefs) archiveDir() string { return releaserecord.ArchiveDir(x.releasable.Name) }

// expected derives the refs version v owns. archive is v's archive, nil for
// a version no archive records yet: its shipped_as names the primary, and
// the paths it recorded trees for bound the companions. withMembers false
// derives them without the members, for a caller that reports the members'
// error itself.
func (x *releasableRefs) expected(v semver.Version, archive *releaserecord.Archive, withMembers bool) (targets.ExpectedRefs, error) {
	schemeTag := x.scheme.Render(v)
	in := targets.RefInputs{SchemeTag: schemeTag}
	if withMembers {
		if x.membersErr != nil {
			return targets.ExpectedRefs{}, x.membersErr
		}
		in.Members = x.members
	}
	primary := schemeTag
	if archive != nil {
		in.ShippedAs = archive.ShippedAs
		if archive.ShippedAs != "" {
			primary = archive.ShippedAs
		}
		if archive.Fate == releaserecord.FateRecorded {
			in.RecordedPaths = []string{}
			for p := range archive.ReleaseCommit.Trees {
				in.RecordedPaths = append(in.RecordedPaths, p)
			}
			sort.Strings(in.RecordedPaths)
		}
	}
	in.Aliases = x.aliases[primary]
	return targets.ExpectedRefsOf(v, in)
}

// isCompanion reports whether tag is one of the companion tags version v of
// the releasable owns: a member's ecosystem tag, which carries no GitHub
// Release.
func (x *releasableRefs) isCompanion(tag string, v semver.Version) (bool, error) {
	var archive *releaserecord.Archive
	versions, err := releaserecord.ArchivedVersions(x.root, x.archiveDir())
	if err != nil {
		return false, err
	}
	if slices.ContainsFunc(versions, func(a semver.Version) bool { return semver.Compare(a, v) == 0 }) {
		a, err := releaserecord.ReadArchive(x.root, x.archiveDir(), v)
		if err != nil {
			return false, err
		}
		archive = &a
	}
	refs, err := x.expected(v, archive, true)
	if err != nil {
		return false, err
	}
	return slices.Contains(refs.Companions, tag), nil
}
