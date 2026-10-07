package release

import (
	"fmt"
	"strings"

	"github.com/stricttools/rlsbl/internal/semver"
)

// CIError is a release stopped at its CI verdict on the pushed candidate.
// Under main-as-candidate ordering this is an ordinary stop: the candidate
// is on the release branch, and no tag, GitHub Release, or finalized
// changelog exists, so the version is not burnt; the fix goes forward on the
// branch and the release resumes at the same version.
type CIError struct{ Message string }

func (e *CIError) Error() string { return e.Message }

// notTagged is what a stop before the tag left undone.
func notTagged(v semver.Version, tag string) []string {
	return []string{
		"Nothing was tagged, released, or finalized:",
		fmt.Sprintf("  - no tag %s exists, here or on origin", tag),
		"  - no GitHub Release exists",
		fmt.Sprintf("  - the changelog is still unreleased (%s was not finalized)", v),
		"  - nothing reached any registry",
	}
}

// ciRedMessage is the stop of a red verdict: the fix goes forward on the
// branch, and the resume completes the same version.
func ciRedMessage(v semver.Version, tag, branch, candidate, detail string) string {
	lines := []string{
		fmt.Sprintf("CI did not pass on the release candidate of %s.", v),
		"  " + detail,
		fmt.Sprintf("  Candidate commit: %s (on origin/%s)", candidate, branch),
		"",
	}
	lines = append(lines, notTagged(v, tag)...)
	lines = append(lines, "",
		fmt.Sprintf("The version is not burnt. Fix forward on %s:", branch),
		fmt.Sprintf("  1. fix the failure and commit it on %s, recording it with `rlsbl changelog add`", branch),
		fmt.Sprintf("  2. rlsbl release resume: it pushes the new tip as the candidate, waits for CI again, and completes %s when CI passes", v),
		"",
		"Do not start a release at a higher version to escape a red verdict, and do not run CI again on the same commit expecting another answer: a failure in the code fails the same way every time.")
	return strings.Join(lines, "\n")
}

// ciNotRunMessage is the stop of a green run in which the releasable's own
// CI jobs never ran: nothing was proven about the candidate.
func ciNotRunMessage(v semver.Version, tag, branch, candidate, detail string) string {
	lines := []string{
		fmt.Sprintf("CI never ran for this releasable on the release candidate of %s.", v),
		"  " + detail,
		fmt.Sprintf("  Candidate commit: %s (on origin/%s)", candidate, branch),
		"",
	}
	lines = append(lines, notTagged(v, tag)...)
	lines = append(lines, "",
		"This is neither a CI failure nor a timeout: the runs passed, but the releasable's own jobs in them never ran, and the publish workflow asks the same question, so tagging now would create a version that can never publish.",
		fmt.Sprintf("The version is not burnt. Make the candidate hold a commit the releasable's CI runs on: commit a change under one of its members on %s (recording it with `rlsbl changelog add`), then run `rlsbl release resume`.", branch))
	return strings.Join(lines, "\n")
}

// ciTimeoutMessage is the stop of a CI wait that ran out of time: nothing
// was proven either way, and the runs may still be going.
func ciTimeoutMessage(v semver.Version, tag, branch, candidate, detail string) string {
	lines := []string{
		fmt.Sprintf("The CI wait for %s ran out of time before every run concluded.", v),
		"  " + detail,
		fmt.Sprintf("  Candidate commit: %s (on origin/%s)", candidate, branch),
		"",
		"This is not a CI failure: those runs may still be going, and nothing was proven about the candidate either way.",
		"",
	}
	lines = append(lines, notTagged(v, tag)...)
	lines = append(lines, "",
		"The version is not burnt:",
		fmt.Sprintf("  1. check the runs: `rlsbl watch %s`", candidate),
		fmt.Sprintf("  2. when they pass: `rlsbl release resume`, which completes %s", v),
		fmt.Sprintf("  3. when they fail: fix forward on %s and resume; when they are only slow, give the resume a longer --ci-timeout, or declare ci_seconds in the declarations", branch))
	return strings.Join(lines, "\n")
}
