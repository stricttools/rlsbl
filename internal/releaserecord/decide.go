package releaserecord

import (
	"fmt"
	"path"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

// Decision is what the next release ships, decided from the record.
type Decision struct {
	// FirstRelease is true only when the record holds no released version:
	// the current version then ships as it is and Bump is empty.
	FirstRelease bool
	Version      semver.Version
	Bump         semver.Bump
	Tag          string
}

// BehindLatestText is the explanation every refusal of a version below the
// latest release shares. namedBy says where the version was read, completing
// "<namedBy> <version>": "the version files say" or "the in-progress release
// state names". The release and `release abandon` both print it, so the two
// cannot explain the state differently.
func BehindLatestText(v, latest semver.Version, namedBy string) string {
	return fmt.Sprintf("%s %s, which is behind the latest release, %s. The version files must name at least %s: rlsbl never releases a version below the latest release, and never records one as never released. Nothing was changed.", namedBy, v, latest, latest)
}

// DecideVersion decides what the next release ships from current, the
// version the version files name, the declared bump, and latest, the
// record's latest-release fact (which the release has already judged).
// `release run`, batch planning, and `rewrite project-name` all ask here, so
// they cannot disagree.
//
// The current version's fate decides, with its local tag as corroboration:
//
//   - released (recorded or unrecoverable): bump from it; a released version
//     whose tag is gone is refused, and restoring the tag at the commit it
//     shipped from clears that;
//   - never released: bump from it, the number an abandoned attempt claimed;
//   - no archive, tag present: bump from it;
//   - no archive, no tag: a first release when nothing was ever released
//     (the current version ships as it is); once anything was released it
//     is refused, as behind the latest release when it is below it, and
//     otherwise as the state an abandoned attempt leaves, which `release
//     abandon` records.
//
// A bumped version the record holds as never released or retracted is
// refused too (BumpedVersion).
func (r *Record) DecideVersion(current semver.Version, bump semver.Bump, latest LatestFact) (Decision, error) {
	if _, err := semver.ParseBump(string(bump)); err != nil {
		return Decision{}, err
	}
	fate, err := r.Fate(current)
	if err != nil {
		return Decision{}, err
	}
	currentTag := r.scheme.Render(current)
	_, tagged, err := r.repo.TagCommit(currentTag)
	if err != nil {
		return Decision{}, err
	}
	if fate.Released() && !tagged {
		return Decision{}, r.destroyedTagError(current, currentTag, fate)
	}
	if fate == FateAbsent && !tagged {
		if latest.Released && semver.Compare(current, latest.Version) < 0 {
			return Decision{}, fmt.Errorf("%s\n  Set the version files to %s, then re-run the release, which bumps from %s.", BehindLatestText(current, latest.Version, "the version files say"), latest.Version, latest.Version)
		}
		if latest.Released {
			return Decision{}, fmt.Errorf("the version files say %s, but the latest release is %s, and the release record holds nothing for %s: no archive %s, no tag %q.\n"+
				"  That is the state an abandoned release attempt leaves behind: the version files were\n"+
				"  bumped, and the attempt never reached the step that records a release. rlsbl will not\n"+
				"  release %s as it is, and will not guess a version to bump from. Nothing was changed.\n"+
				"  Record %s as never released, then re-run the release, which bumps from %s:\n"+
				"    rlsbl release abandon --approve-consequential",
				current, latest.Version, current, ArchivePath(r.dir, current), currentTag, current, current, current)
		}
		// Nothing was released, yet a finalized changelog file of the
		// version still contradicts a first release.
		if err := r.refuseFinalizedChangelog(current, currentTag); err != nil {
			return Decision{}, err
		}
		return Decision{FirstRelease: true, Version: current, Tag: currentTag}, nil
	}
	return r.BumpedVersion(current, bump)
}

// BumpedVersion is the release that applies bump to current, refusing a
// result the record holds as never released or retracted and naming the
// version files' value that bumps past it. `rewrite project-name` asks it directly for an
// unrecoverable current version, which the release refuses until its tag is
// restored: the rename records the version that release then ships.
func (r *Record) BumpedVersion(current semver.Version, bump semver.Bump) (Decision, error) {
	next, err := current.Bump(bump)
	if err != nil {
		return Decision{}, err
	}
	// taken says why the record holds v's number from every release: never
	// released (an abandoned attempt claimed it) or retracted; empty when
	// it does not.
	taken := func(v semver.Version) (string, error) {
		f, err := r.Fate(v)
		if err != nil || f == FateAbsent {
			return "", err
		}
		if f == FateNeverReleased {
			return "never released", nil
		}
		if f == FateUnstated {
			return "", nil
		}
		a, err := ReadArchive(r.root(), r.dir, v)
		if err != nil {
			return "", err
		}
		if a.Retracted() {
			return "retracted (by " + a.RetractedBy + ")", nil
		}
		return "", nil
	}
	why, err := taken(next)
	if err != nil {
		return Decision{}, err
	}
	if why != "" {
		base, candidate := next, next
		for {
			c, err := candidate.Bump(bump)
			if err != nil {
				return Decision{}, err
			}
			base, candidate = candidate, c
			again, err := taken(candidate)
			if err != nil {
				return Decision{}, err
			}
			if again == "" {
				break
			}
		}
		return Decision{}, fmt.Errorf("the %s bump from %s gives %s, which the release record holds as %s: %s\n"+
			"  A never-released number was claimed by an abandoned attempt, and a retracted one is\n"+
			"  retracted on the Go module proxy for good; rlsbl releases under neither. Nothing was\n"+
			"  changed.\n"+
			"  Bump past it: set the version files to %s, which the record holds, and the %s bump\n"+
			"  then gives %s.",
			bump, current, next, why, ArchivePath(r.dir, next), base, bump, candidate)
	}
	return Decision{Version: next, Bump: bump, Tag: r.scheme.Render(next)}, nil
}

// destroyedTagError refuses a release of a version the record says shipped
// whose tag is gone: the tag was deleted (an interrupted or undone release),
// or the tag format changed since the version shipped. Without it the
// release would read the version as a first release.
func (r *Record) destroyedTagError(v semver.Version, tag string, fate Fate) error {
	record := ArchivePath(r.dir, v)
	commit := "<release-commit>"
	note := ""
	switch fate {
	case FateRecorded:
		a, err := ReadArchive(r.root(), r.dir, v)
		if err != nil {
			return err
		}
		commit = a.ReleaseCommit.Commit
	case FateUnrecoverable:
		note = fmt.Sprintf(" The archive is marked unrecoverable: the release record cannot name the commit %s shipped from, so the release cannot tell where the history it ships from begins. Only you know that commit.", v)
	}
	return destroyedTag(v, tag, "release archive", record, commit, note)
}

func destroyedTag(v semver.Version, tag, recordName, record, commit, note string) error {
	return fmt.Errorf("version %s was released before: its %s %s exists, but no tag %q is present. "+
		"This happens when the tag was deleted (by an interrupted or undone release, say), or when "+
		"the tag format changed since the version was released, so it was tagged under an old name "+
		"and the current tag %q does not exist. Either way this looks like a first release when it "+
		"is not.%s\n"+
		"Recover by either:\n"+
		"  (1) restoring the tag %q at the commit %s was released from (git tag %s %s), then\n"+
		"      re-running the release; or\n"+
		"  (2) if the tag format changed, checking tag_format in %s: if %s was released under a\n"+
		"      different tag, create the current tag %q at that release commit.",
		v, recordName, record, tag, tag, note, tag, v, tag, commit, declarations.ReleasablesFile, v, tag)
}

// refuseFinalizedChangelog refuses a first release of a version whose
// finalized changelog file exists: a releasable whose releases predate the
// archives still has that record of them.
func (r *Record) refuseFinalizedChangelog(v semver.Version, tag string) error {
	finalized := path.Join(declarations.ChangelogDir(r.releasable), v.String()+".jsonl")
	found, err := exists(r.root(), finalized)
	if err != nil {
		return err
	}
	if !found {
		return nil
	}
	return destroyedTag(v, tag, "finalized changelog", finalized, "<release-commit>", "")
}
