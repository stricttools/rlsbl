package release

import (
	"fmt"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/ci"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A release pins the branch tip when it starts and records every commit it
// makes. Every commit between the pin and the branch tip that it did not
// make arrived from somewhere else (another session, a hook, an editor), and
// would ship under this version without ever being reviewed as part of it,
// so the release refuses it at its checkpoints: when it starts mutating,
// before the candidate push, after the CI verdict, and before the final
// push. A resume pins again at the tip and adopts what was committed while
// the release was stopped, provided the changelog describes it.

// The checkpoints at which a release refuses commits it did not make.
const (
	CheckpointEntry         = "mutating entry"
	CheckpointCandidatePush = "candidate push"
	CheckpointCIGate        = "CI gate"
	CheckpointFinalPush     = "final push"
)

// What a refusal tells the operator to run once the cause is dealt with: a
// fresh release has nothing in progress until it saves its state, and a
// resume has.
const (
	RerunFresh  = "run `" + runstate.RunInvocation + "` again"
	RerunResume = "run `" + runstate.ResumeInvocation + "`"
)

// ForeignCommitError is the refusal of commits the release did not make.
// Nothing is rolled back: those commits are what must be kept.
type ForeignCommitError struct{ Message string }

func (e *ForeignCommitError) Error() string { return e.Message }

// UnverifiedCandidateError is the refusal of a release whose commit CI
// verified cannot be established: the tag would claim a verification that
// is not there, and the publish workflow believes the claim.
type UnverifiedCandidateError struct{ Message string }

func (e *UnverifiedCandidateError) Error() string { return e.Message }

// foreignCommits are the commits on the release branch (ref) since pin that
// created does not account for, newest first.
func foreignCommits(live git.Repo, ref, pin string, created []string) ([]string, error) {
	if pin == "" {
		return nil, nil
	}
	commits, err := live.Commits([]string{ref}, []string{pin})
	if err != nil {
		return nil, err
	}
	var out []string
	for _, c := range commits {
		if !slices.Contains(created, c) {
			out = append(out, c)
		}
	}
	return out, nil
}

// subjectLines are "  <short>  <subject>" for each commit.
func subjectLines(repo git.Repo, commits []string) []string {
	lines := make([]string, len(commits))
	for i, c := range commits {
		subject, err := repo.CommitSubject(c)
		if err != nil {
			subject = "(the subject cannot be read: " + err.Error() + ")"
		}
		lines[i] = fmt.Sprintf("  %s  %s", short(c), subject)
	}
	return lines
}

// GuardForeignCommits refuses, naming each commit with its subject, the
// commits on the release branch (ref) since pin that created does not
// account for. checkpoint names where the release stopped; rerun is what the
// operator runs once the commits are recorded.
func GuardForeignCommits(live git.Repo, ref, pin string, created []string, checkpoint, rerun string) error {
	foreign, err := foreignCommits(live, ref, pin, created)
	if err != nil || len(foreign) == 0 {
		return err
	}
	lines := []string{fmt.Sprintf("the release stopped at the %s: commits this release did not make appeared on the branch after it started.", checkpoint), "", "Commits that are not part of this release:"}
	lines = append(lines, subjectLines(live, foreign)...)
	lines = append(lines, "",
		fmt.Sprintf("They would ship under this version without ever being reviewed as part of it. The release pinned the branch at %s.", short(pin)),
		"",
		"To include them, record them with `rlsbl changelog add` and "+rerun+"; to exclude them, move them off the release branch first.")
	return &ForeignCommitError{Message: strings.Join(lines, "\n")}
}

// RequireAdoptedCovered refuses a resume that would adopt commits the
// releasable's unreleased changelog does not describe: commits the coverage
// check asks an entry for (in the releasable's scope, named by no entry, and
// not exempt). The refusal names the command recording each one.
func RequireAdoptedCovered(repo git.Repo, w *workspace.Workspace, releasable string, adopted []string, version semver.Version) error {
	if len(adopted) == 0 {
		return nil
	}
	subject, err := changelog.NewSubject(repo, w, releasable, "", nil)
	if err != nil {
		return err
	}
	f, err := changelog.ReadUnreleased(w.Root, subject.Dir())
	if err != nil {
		return err
	}
	covered, err := subject.CoveredCommits(f)
	if err != nil {
		return err
	}
	needing, err := changelog.CommitsNeedingEntries(repo, adopted, subject.Scope(), covered)
	if err != nil {
		return err
	}
	if len(needing.Commits) == 0 {
		return nil
	}
	lines := []string{fmt.Sprintf("the resume of %s would adopt commits made after the release stopped, and the changelog does not describe them:", version)}
	lines = append(lines, subjectLines(repo, needing.Commits)...)
	lines = append(lines, "", fmt.Sprintf("A resume pins again at the branch tip, so these ship under %s. Record each one, then run `"+runstate.ResumeInvocation+"`:", version))
	for _, c := range needing.Commits {
		lines = append(lines, fmt.Sprintf("  rlsbl changelog add --commits %s --type fix --description \"...\"", short(c)))
	}
	lines = append(lines, "(a commit that changes nothing a user would notice takes `--no-user-facing` instead of a type and description)")
	return &ForeignCommitError{Message: strings.Join(lines, "\n")}
}

// requireRecordedCandidate is the commit a resume past the CI verdict tags:
// the candidate the state records, which must resolve and be on the release
// branch (ref). There is no fallback to the branch tip, which is not
// evidence that CI ran on anything.
func requireRecordedCandidate(repo git.Repo, ref, recorded string, v semver.Version, fate releaserecord.Fate, statePath string) (string, error) {
	remedy := fmt.Sprintf(" The release cannot claim CI verification for %s. Put the commit CI verified back on the release branch and run `"+runstate.ResumeInvocation+"`; to give up on %s instead, run `rlsbl release abandon --approve-consequential`, which records it as never released.", v, v)
	if fate.Released() {
		remedy = fmt.Sprintf(" The release cannot claim CI verification for %s. Put the commit CI verified back on the release branch and run `"+runstate.ResumeInvocation+"`, or roll the release back with `rlsbl release undo`.", v)
	}
	if recorded == "" {
		return "", &UnverifiedCandidateError{Message: fmt.Sprintf("%s records the ci-verified step but no release_commit, so the commit CI verified is unknown.%s", statePath, remedy)}
	}
	sha, found, err := repo.ResolveCommit(recorded)
	if err != nil {
		return "", err
	}
	if !found {
		return "", &UnverifiedCandidateError{Message: fmt.Sprintf("the commit CI verified for %s, %s, does not resolve in this repository.%s", v, short(recorded), remedy)}
	}
	verdict, err := repo.Ancestry(sha, ref)
	if err != nil {
		return "", err
	}
	switch verdict {
	case git.IsAncestor:
		return sha, nil
	case git.NotAncestor:
		return "", &UnverifiedCandidateError{Message: fmt.Sprintf("the commit CI verified for %s, %s, is not on the release branch, so the branch no longer contains it.%s", v, short(sha), remedy)}
	}
	return "", &UnverifiedCandidateError{Message: fmt.Sprintf("git cannot tell whether the commit CI verified for %s, %s, is on the release branch: the history is shallow or missing objects. Deepen it (`git fetch --unshallow`) and run `"+runstate.ResumeInvocation+"`.%s", v, short(sha), remedy)}
}

// candidateWindow is the judgment of a candidate push's diff window against
// the CI router's path filters.
type candidateWindow int

const (
	// windowTriggers: the window triggers the releasable's CI, or there is
	// no router window to judge (a standalone repository, a branch origin
	// does not have yet).
	windowTriggers candidateWindow = iota
	// windowOwesDispatch: the window triggers nothing, the candidate was
	// published once already, and the router can be dispatched with
	// run_all: the release pushes and dispatches.
	windowOwesDispatch
)

// judgeCandidateWindow judges whether the push of candidate onto origin's
// branch (at remote) can trigger the CI of the releasable's members in the
// generated router, whose jobs are filtered by the paths a push touched. A
// window matching no member's filter would skip every job, and the publish
// workflow refuses a skipped check, so a fresh release whose own commits
// miss its filters is refused here rather than after a CI wait that can only
// end one way. A resume whose fix-forward honestly touches nothing of the
// releasable (its candidate published once before) owes a run_all dispatch
// instead. needsPush false means origin is at the candidate already, and
// the window is the release's own commits. rerun is what the refusal tells
// the operator to run once a change is committed.
func judgeCandidateWindow(repo git.Repo, w *workspace.Workspace, releasable, candidate, remote string, needsPush, publishedBefore bool, created []string, v semver.Version, tag, branch, rerun string) (candidateWindow, error) {
	if !w.IsWorkspace() || remote == "" {
		return windowTriggers, nil
	}
	filters, err := workflows.NewFilters(w)
	if err != nil {
		return 0, err
	}
	type project struct {
		name     string
		patterns []string
	}
	var projects []project
	for _, m := range sortedMembers(w.MembersOf(releasable)) {
		patterns, err := filters.PatternsFor(m)
		if err != nil {
			return 0, err
		}
		projects = append(projects, project{m.Name, patterns})
	}
	if len(projects) == 0 {
		return windowTriggers, nil
	}
	base := remote
	if !needsPush {
		// The candidate is on origin already: the run judged is the one an
		// earlier push started, whose window began below the release's own
		// first commit.
		for _, c := range created {
			if parent, found, err := repo.ResolveCommit(c + "^"); err == nil && found {
				base = parent
				break
			}
		}
	}
	changed, err := repo.ChangedBetween(base, candidate)
	if err != nil {
		return 0, fmt.Errorf("the candidate's diff window (%s..%s) cannot be read, so whether its push triggers the CI of %s cannot be judged: %w", short(base), short(candidate), releasable, err)
	}
	for _, p := range projects {
		if workflows.AnyPathMatches(changed, p.patterns) {
			return windowTriggers, nil
		}
	}
	router := filepath.Join(w.Root, filepath.FromSlash(workflows.Dir), workflows.RouterFile)
	hasRouter, err := fileExists(router)
	if err != nil {
		return 0, fmt.Errorf("the CI router %s/%s cannot be read, so whether the candidate's push triggers the CI of %s cannot be judged: %w", workflows.Dir, workflows.RouterFile, releasable, err)
	}
	if hasRouter && needsPush && publishedBefore {
		return windowOwesDispatch, nil
	}
	var followable, excluded []string
	for _, p := range projects {
		for _, pattern := range p.patterns {
			if rest, ok := strings.CutPrefix(pattern, "!"); ok {
				excluded = append(excluded, rest)
			} else {
				followable = append(followable, pattern)
			}
		}
	}
	sort.Strings(changed)
	shown := changed
	more := ""
	if len(shown) > 10 {
		more = fmt.Sprintf("\n    ... and %d more", len(shown)-10)
		shown = shown[:10]
	}
	lines := []string{
		fmt.Sprintf("the release candidate of %s cannot trigger the CI of %s, so its CI verdict could never pass.", v, releasable),
		fmt.Sprintf("  Candidate commit: %s", candidate),
		fmt.Sprintf("  Diff window: %s..%s", short(base), short(candidate)),
		"",
		"The CI router filters each member's jobs by the paths a push touched, and nothing in this window matches the filters of the releasable's members:",
		"  Filters:",
		"    " + strings.Join(orNone(followable), "\n    "),
	}
	if len(excluded) > 0 {
		lines = append(lines, "  Not counting (the filters exclude them):", "    "+strings.Join(excluded, "\n    "))
	}
	lines = append(lines, "  Changed in the window:", "    "+strings.Join(orNone(shown), "\n    ")+more, "",
		fmt.Sprintf("The members' jobs would conclude skipped, and the publish workflow refuses a skipped check, so %s would exist for a version that can never publish. Nothing was pushed, tagged, released, or finalized, and %s is not burnt.", tag, v),
		fmt.Sprintf("Commit a change under one of the filters above on %s (record it with `rlsbl changelog add`), then %s.", branch, rerun),
		"",
		ci.RunAllFix)
	return 0, &CIError{Message: strings.Join(lines, "\n")}
}

func orNone(items []string) []string {
	if len(items) == 0 {
		return []string{"(none)"}
	}
	return items
}
