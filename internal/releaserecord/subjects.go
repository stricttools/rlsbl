package releaserecord

import (
	"fmt"
	"regexp"

	"github.com/stricttools/rlsbl/internal/semver"
)

// VersionBumpSubjects are the subjects a release's version-bump commit of v
// carries, tagged tag: the tag itself, which a standalone project's release
// writes, and "<releasable>: release v<version>", which a workspace
// releasable's release writes. Both spellings are in the histories the
// record describes, so `release undo` recognizes either when it locates the
// commit it reverts, and the release writes one of them.
func VersionBumpSubjects(releasable, tag string, v semver.Version) []string {
	return []string{tag, fmt.Sprintf("%s: release v%s", releasable, v)}
}

// cleanExclusionsSubject is the subject of the batch-limit clean-up commit.
var cleanExclusionsSubject = regexp.MustCompile(`^chore: clean [0-9]+ stale batch exclusion\(s\) from config\.json$`)

// IsFinalizationSubject reports whether subject is one a release gave a
// commit it made after its version-bump commit while it recorded v, in the
// histories written before the release tagged the commit CI verified: the
// changelog and release-file finalizations, the regenerated notes, the
// batch-limit clean-up, and the workspace snapshot. In those histories such
// commits can sit between the version-bump commit and the release commit, so
// `release undo` reverts them with it; a release tagging the commit CI
// verified makes them above its release commit, where undo repairs what they
// recorded instead.
func IsFinalizationSubject(subject string, v semver.Version) bool {
	switch subject {
	case fmt.Sprintf("chore: finalize changelog for %s", v),
		fmt.Sprintf("chore: finalize release file for %s", v),
		fmt.Sprintf("chore: regenerate %s.md from archived release metadata", v),
		"snapshot":
		return true
	}
	return cleanExclusionsSubject.MatchString(subject)
}
