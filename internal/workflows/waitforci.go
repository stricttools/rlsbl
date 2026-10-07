package workflows

import (
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The wait-for-ci job.
//
// A publish workflow starts when a GitHub Release is published, and would
// race CI on the same commit, so every publish workflow starts with the
// wait-for-ci job and every publish job needs it. The job resolves the
// release commit from the rlsbl-ci-sha marker rlsbl writes into the Release
// body (read again a few times, since a new Release can lag on GitHub's read
// replicas), else from GITHUB_SHA (the tagged commit, for a release event
// and for a dispatch at the tag alike; it never reads the release event's
// payload, which a dispatch has none of), and polls the commit's check runs
// until the releasing project's CI concluded:
//
//   - success on every matching check run: the publish goes on;
//   - failure or timed_out: refused;
//   - cancelled or skipped: refused, explained, never waited for, since such
//     a check proves nothing about the commit;
//   - no matching check run within the grace window: refused.
//
// Same-named check runs collapse to the newest, except that a skip never
// outranks a verdict of the same job (its matrix entries included): the rule
// internal/ci applies before the release tags, so the two never disagree.
//
// Under the release's ordering CI has already gone green on the commit
// before it was tagged, so this job confirms rather than waits; a red answer
// means the world changed after the release, and the fix is a new release,
// never a retried publish.

// The job's limits, written into its env so a repository can adjust them by
// editing its workflow.
const (
	waitTimeoutMinutes  = 20
	waitGraceMinutes    = 5
	waitPollSeconds     = 15
	markerAttempts      = 5
	markerRetrySeconds  = 5
	waitJobMinutesSlack = 5
)

// PublishConcurrency is the concurrency block of every publish workflow, at
// the workflow's top level: one publish run per tag, a retry queued behind
// the run in flight, and a publish never cancelled.
const PublishConcurrency = "concurrency:\n  group: publish-" + TagExpression + "\n  cancel-in-progress: false\n"

// ciJobNames are the CI job names each target's scaffolded CI workflow
// declares: the check runs a standalone repository's CI produces are named
// after them (a matrix job appends " (...)").
var ciJobNames = map[string][]string{
	declarations.TargetGo:   {"test"},
	declarations.TargetNPM:  {"test"},
	declarations.TargetPyPI: {"test"},
}

// CheckPatternForTargets is the pattern of the check-run names a standalone
// repository's scaffolded CI produces for the targets: each CI job name,
// exactly or with its matrix suffix. A target rlsbl scaffolds no CI for is
// refused, and so is an empty target list.
func CheckPatternForTargets(targetNames []string) (string, error) {
	if len(targetNames) == 0 {
		return "", errors.New("a CI check pattern needs at least one target")
	}
	seen := map[string]bool{}
	var names []string
	for _, t := range targetNames {
		jobs, ok := ciJobNames[t]
		if !ok {
			return "", fmt.Errorf("rlsbl scaffolds no CI workflow for the target %q, so it cannot name the check runs its CI produces", t)
		}
		for _, j := range jobs {
			if !seen[j] {
				seen[j] = true
				names = append(names, regexp.QuoteMeta(j))
			}
		}
	}
	sort.Strings(names)
	return `^(` + strings.Join(names, "|") + `)( \(.*\))?$`, nil
}

// waitScript is the polling step. It reads only the runner's environment
// and the job's env, never a GitHub expression and never the event payload.
// Its collapse of same-named check runs is the one internal/ci applies.
const waitScript = `set -euo pipefail

# The release commit: the rlsbl-ci-sha marker in the Release body when it is
# there (read MARKER_ATTEMPTS times, MARKER_RETRY_SECONDS apart, since a new
# Release can lag on GitHub's read replicas), else GITHUB_SHA, the tagged
# commit for a release event and for a dispatch at the tag alike. The tag is
# the dispatch's tag input when one was given (TAG_INPUT), else the ref.
extract_ci_sha() {
  sed -n 's/.*<!-- rlsbl-ci-sha: \([0-9a-f]\{40\}\) -->.*/\1/p' | head -n1
}
tag="${TAG_INPUT:-$GITHUB_REF_NAME}"
sha=""
attempt=1
while [ "$attempt" -le "$MARKER_ATTEMPTS" ]; do
  if body="$(gh release view "$tag" --json body --jq .body 2>/dev/null)"; then
    sha="$(printf '%s\n' "$body" | extract_ci_sha)"
  fi
  if [ -n "$sha" ]; then
    break
  fi
  if [ "$attempt" -lt "$MARKER_ATTEMPTS" ]; then
    echo "wait-for-ci: the rlsbl-ci-sha marker is not visible in the '$tag' Release body yet (attempt $attempt/$MARKER_ATTEMPTS); reading it again in ${MARKER_RETRY_SECONDS}s"
    sleep "$MARKER_RETRY_SECONDS"
  fi
  attempt=$(( attempt + 1 ))
done
if [ -n "$sha" ]; then
  echo "wait-for-ci: the release commit is the one the rlsbl-ci-sha marker of the '$tag' Release names."
else
  sha="$GITHUB_SHA"
  echo "wait-for-ci: no rlsbl-ci-sha marker in the '$tag' Release body after $MARKER_ATTEMPTS reads; the release commit is GITHUB_SHA."
fi
echo "wait-for-ci: waiting for CI on $tag (commit $sha)"
echo "Check-run name pattern: $CI_CHECK_PATTERN"
# The limits are this job's env. Raising WAIT_TIMEOUT_MINUTES needs the job's
# timeout-minutes raised with it, or GitHub cancels the job first.

now() { date +%s; }
start="$(now)"
deadline=$(( start + WAIT_TIMEOUT_MINUTES * 60 ))
grace_deadline=$(( start + WAIT_GRACE_MINUTES * 60 ))

while :; do
  if ! resp="$(gh api --paginate "repos/$GITHUB_REPOSITORY/commits/$sha/check-runs?per_page=100")"; then
    if [ "$(now)" -ge "$deadline" ]; then
      echo "::error::wait-for-ci: the checks API request for $sha kept failing for $WAIT_TIMEOUT_MINUTES minutes."
      echo "Without the check runs nothing says whether CI passed, so the publish is refused."
      echo "Check the API response above and this job's checks: read permission, then dispatch this publish workflow again at the tag: gh workflow run <publish workflow> --ref $tag"
      exit 1
    fi
    echo "The checks API request failed; asking again in ${WAIT_POLL_SECONDS}s"
    sleep "$WAIT_POLL_SECONDS"
    continue
  fi
  # This project's check runs, minus this workflow run's own jobs. Same-named
  # check runs collapse to the newest (started_at, then id), except that a
  # skipped one gives way to the newest completed check run of the same name
  # that is not skipped (a run_all dispatch of the CI router puts a
  # verdict beside the push run's skip, often recorded earlier). Then a skip
  # is dropped when a completed, unskipped matrix entry of the same job exists
  # ("name (...)"), since GitHub records a skipped matrix job under the bare
  # name. Nothing else covers a skip.
  runs="$(jq -s --arg re "$CI_CHECK_PATTERN" --arg run_id "$GITHUB_RUN_ID" '
    [ .[].check_runs[]
      | select(.name | test($re))
      | select((.details_url // "") | contains("/actions/runs/" + $run_id + "/") | not)
      | {name, status, conclusion, id, started_at} ]
    | group_by(.name)
    | map(
        . as $g
        | ($g | sort_by(.started_at, .id) | last) as $newest
        | (if $newest.conclusion == "skipped"
           then ([ $g[] | select(.status == "completed" and .conclusion != "skipped") ]
                 | sort_by(.started_at, .id) | last // $newest)
           else $newest end))
    | . as $latest
    | map(. as $c
        | select(
            $c.conclusion != "skipped"
            or ([ $latest[]
                  | select((.name | startswith($c.name + " ("))
                           and .status == "completed"
                           and .conclusion != "skipped") ]
                | length) == 0))' <<< "$resp")"
  total="$(jq 'length' <<< "$runs")"

  if [ "$total" -eq 0 ]; then
    if [ "$(now)" -ge "$grace_deadline" ]; then
      echo "::error::wait-for-ci: no CI check run matching $CI_CHECK_PATTERN appeared on $sha within $WAIT_GRACE_MINUTES minutes."
      echo "rlsbl tags a commit only after its CI ran on it, so the check runs must be there."
      echo "They are missing when the check runs were deleted, the release commit was resolved wrongly, or the tag was created outside rlsbl."
      echo "When the CI jobs were renamed, change CI_CHECK_PATTERN in this job to match the new names."
      exit 1
    fi
    echo "No matching CI check run yet; asking again in ${WAIT_POLL_SECONDS}s"
    sleep "$WAIT_POLL_SECONDS"
    continue
  fi

  pending="$(jq '[ .[] | select(.status != "completed") ] | length' <<< "$runs")"
  if [ "$pending" -gt 0 ]; then
    if [ "$(now)" -ge "$deadline" ]; then
      echo "::error::wait-for-ci: CI did not complete on $sha within $WAIT_TIMEOUT_MINUTES minutes."
      jq -r '.[] | "  \(.name): status=\(.status) conclusion=\(.conclusion // "none")"' <<< "$runs"
      exit 1
    fi
    echo "$pending of $total matching CI check runs still running; asking again in ${WAIT_POLL_SECONDS}s"
    sleep "$WAIT_POLL_SECONDS"
    continue
  fi

  not_success="$(jq '[ .[] | select(.conclusion != "success") ]' <<< "$runs")"
  if [ "$(jq 'length' <<< "$not_success")" -gt 0 ]; then
    echo "::error::wait-for-ci: CI did not pass on $sha, so nothing is published."
    jq -r '.[] | "  \(.name): \(.conclusion)"' <<< "$not_success"
    while IFS= read -r conclusion; do
      case "$conclusion" in
        failure|timed_out)
          echo "CI concluded '$conclusion' on the release commit."
          echo "rlsbl tags and releases a commit only after its CI went green, so CI was run again on a released commit and failed, a required check was added after the release, or the tag and Release were created outside rlsbl."
          echo "Dispatching this workflow again gives the same answer: the failure is in the code at this commit."
          echo "Fix it on the release branch, release a new version with 'rlsbl release run --watch', and mark this one with 'rlsbl release deprecate'."
          ;;
        cancelled)
          echo "A CI check run was cancelled, which proves nothing about the commit."
          echo "Run that CI workflow again on this commit (gh run rerun <run-id>); when it passes, dispatch this workflow again at the tag: gh workflow run <publish workflow> --ref $tag"
          ;;
        skipped)
          echo "A CI check run matching the pattern was skipped: this project must run its own CI on the release commit."
          echo "Check the path filters and job conditions that skipped it, run CI on this commit again, then dispatch this workflow again at the tag."
          ;;
        *)
          echo "A CI check run concluded '$conclusion'; only success lets the publish go on."
          ;;
      esac
    done <<< "$(jq -r '.[].conclusion' <<< "$not_success" | sort -u)"
    exit 1
  fi

  echo "wait-for-ci: all $total matching CI check runs succeeded."
  jq -r '.[] | "  \(.name): \(.conclusion)"' <<< "$runs"
  exit 0
done
`

// waitJob renders the wait-for-ci job under a workflow's jobs key. env
// holds the extra env lines (already indented six spaces), and resolver,
// when not empty, is a step run before the wait that sets CI_CHECK_PATTERN.
func waitJob(env []string, resolver string) string {
	var b strings.Builder
	b.WriteString("  " + WaitForCIJobKey + ":\n")
	b.WriteString("    name: Wait for CI on the release commit\n")
	b.WriteString("    runs-on: ubuntu-latest\n")
	fmt.Fprintf(&b, "    timeout-minutes: %d\n", waitTimeoutMinutes+waitJobMinutesSlack)
	// checks: read polls the check runs; contents: read lets gh read the
	// rlsbl-ci-sha marker from the Release body.
	b.WriteString("    permissions:\n      checks: read\n      contents: read\n")
	b.WriteString("    env:\n")
	b.WriteString("      GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n")
	// The job has no checkout, so gh learns the repository from GH_REPO.
	b.WriteString("      GH_REPO: ${{ github.repository }}\n")
	fmt.Fprintf(&b, "      WAIT_TIMEOUT_MINUTES: \"%d\"\n", waitTimeoutMinutes)
	fmt.Fprintf(&b, "      WAIT_GRACE_MINUTES: \"%d\"\n", waitGraceMinutes)
	fmt.Fprintf(&b, "      WAIT_POLL_SECONDS: \"%d\"\n", waitPollSeconds)
	fmt.Fprintf(&b, "      MARKER_ATTEMPTS: \"%d\"\n", markerAttempts)
	fmt.Fprintf(&b, "      MARKER_RETRY_SECONDS: \"%d\"\n", markerRetrySeconds)
	for _, line := range env {
		b.WriteString(line + "\n")
	}
	b.WriteString("    steps:\n")
	if resolver != "" {
		b.WriteString("      - name: Resolve the releasing project's CI check pattern from the tag\n")
		b.WriteString("        run: |\n")
		b.WriteString(literalBlock(resolver, "          "))
	}
	b.WriteString("      - name: Wait for CI to succeed on the release commit\n")
	b.WriteString("        run: |\n")
	b.WriteString(literalBlock(waitScript, "          "))
	return b.String()
}

// checkPatternProblem says why pattern cannot be the job's check-run name
// pattern; empty when it can.
func checkPatternProblem(pattern string) string {
	if strings.TrimSpace(pattern) == "" {
		return "the CI check pattern is empty"
	}
	if strings.ContainsAny(pattern, "\n\r") {
		return fmt.Sprintf("the CI check pattern %q holds a line break", pattern)
	}
	if _, err := regexp.Compile(pattern); err != nil {
		return fmt.Sprintf("the CI check pattern %q is not a regular expression: %v", pattern, err)
	}
	return ""
}

// WaitForCIJob is the wait-for-ci job of a standalone repository's publish
// workflow, whose releasing project is the repository: the lines under the
// workflow's jobs key, the job key indented two spaces, ending in a newline.
// pattern matches the check-run names of the repository's CI
// (CheckPatternForTargets).
func WaitForCIJob(pattern string) (string, error) {
	if p := checkPatternProblem(pattern); p != "" {
		return "", errors.New(p)
	}
	return waitJob([]string{"      CI_CHECK_PATTERN: " + yamlSingleQuoted(pattern)}, ""), nil
}

// ReleasingProject is one project a workspace's publish router publishes:
// the tag scheme its releasable's tags follow and the pattern of its CI's
// check-run names.
type ReleasingProject struct {
	Tag          TagParts
	CheckPattern string
}

// RouterWaitForCIJob is the wait-for-ci job of a workspace's publish router.
// Its first step picks the releasing project from the tag (the dispatch's
// tag input, else the ref) and sets CI_CHECK_PATTERN; a tag no project's
// scheme matches fails the job. Projects sharing a tag scheme (the members
// of one releasable) share one branch whose pattern accepts every one of
// theirs, and longer schemes are tried first, so a tag of kernel/vulkan/v...
// is never read as kernel/v...'s.
func RouterWaitForCIJob(projects []ReleasingProject) (string, error) {
	if len(projects) == 0 {
		return "", errors.New("a publish router needs at least one releasing project")
	}
	var order []TagParts
	patterns := map[TagParts][]string{}
	for _, p := range projects {
		if p.Tag.Prefix == "" && p.Tag.Suffix == "" {
			return "", errors.New("a releasing project's tag scheme is empty around its version")
		}
		if prob := checkPatternProblem(p.CheckPattern); prob != "" {
			return "", errors.New(prob)
		}
		if _, ok := patterns[p.Tag]; !ok {
			order = append(order, p.Tag)
		}
		found := false
		for _, existing := range patterns[p.Tag] {
			if existing == p.CheckPattern {
				found = true
			}
		}
		if !found {
			patterns[p.Tag] = append(patterns[p.Tag], p.CheckPattern)
		}
	}
	sort.SliceStable(order, func(i, j int) bool {
		li := len(order[i].Prefix) + len(order[i].Suffix)
		lj := len(order[j].Prefix) + len(order[j].Suffix)
		return li > lj
	})
	var lines []string
	lines = append(lines,
		"set -euo pipefail",
		"# To publish a release again, dispatch this workflow at the tag:",
		"# gh workflow run publish.yml --ref <tag>. The tag picks the releasing",
		"# project; a dispatch at a branch matches none and fails here.",
		`tag_ref="${TAG_INPUT:-$GITHUB_REF_NAME}"`,
		`case "$tag_ref" in`,
	)
	var known []string
	for _, tag := range order {
		ps := patterns[tag]
		pattern := ps[0]
		if len(ps) > 1 {
			pattern = "(" + strings.Join(ps, "|") + ")"
		}
		casePattern := shellQuote(tag.Prefix) + "*"
		if tag.Suffix != "" {
			casePattern += shellQuote(tag.Suffix)
		}
		lines = append(lines, "  "+casePattern+")", "    pattern="+shellQuote(pattern), "    ;;")
		known = append(known, tag.Prefix+"<version>"+tag.Suffix)
	}
	lines = append(lines,
		"  *)",
		`    echo "::error::wait-for-ci: the tag '$tag_ref' matches no project's tag scheme (`+strings.ReplaceAll(strings.Join(known, ", "), `"`, `\"`)+`)."`,
		`    echo "To publish a release again, dispatch this workflow at the tag: gh workflow run publish.yml --ref <tag>"`,
		"    exit 1",
		"    ;;",
		"esac",
		`echo "CI_CHECK_PATTERN=$pattern" >> "$GITHUB_ENV"`,
		`echo "The releasing project's CI check pattern: $pattern"`,
	)
	resolver := strings.Join(lines, "\n") + "\n"
	return waitJob([]string{"      TAG_INPUT: ${{ inputs.tag }}"}, resolver), nil
}
