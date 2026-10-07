package checks

import (
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The prepush family: what the pre-push hook refuses to let leave the
// machine. Each answers for the whole repository, since the hook runs at
// the repository root.
func prepushChecks() []check {
	return []check{
		errorCheck("prepush-changelog-coverage", checkPrepushChangelogCoverage),
		errorCheck("prepush-gitignore-guard", checkPrepushGitignoreGuard),
		errorCheck("prepush-manual-warning", checkPrepushManualWarning),
	}
}

// notInPush is the skip reason of a push check run outside a push.
const notInPush = "not in a push: only the pre-push hook reports the refs a push sends"

// pushRefs are the refs of the push the check runs for, and false outside
// one; lines that do not parse leave the check unanswered.
func pushRefs(c *Context) ([]git.PushedRef, bool) {
	refs, inPush, err := c.PushRefs()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return refs, inPush
}

func checkPrepushChangelogCoverage(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	refs, inPush := pushRefs(c)
	if !inPush {
		return r.Skipped(notInPush)
	}
	pushed, err := changelog.PushedCommits(c.Repo(), refs, c.UpstreamExclude())
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var problems []string
	for _, ref := range pushed.Rewritten {
		problems = append(problems, fmt.Sprintf("the push replaces %s on the remote, whose commit %s does not exist here, so the commits it sends cannot be told apart from the ones the remote holds: run `git fetch origin` and push again", ref.RemoteRef, ref.RemoteID))
	}
	ws := c.Workspace()
	checked := 0
	for _, rel := range ws.Releasables() {
		commits, err := workspace.FilterCommits(c.Repo(), pushed.Commits, ws.ScopeOfReleasable(rel.Name))
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if len(commits) == 0 {
			continue
		}
		checked++
		files, err := changelog.ReadAll(c.Root(), changelog.Dir(rel.Name))
		if err != nil {
			panic(unanswered(fmt.Sprintf("the changelog of %q cannot be read: %v", rel.Name, err)))
		}
		missing, err := changelog.PushCoverage(c.Repo(), files, commits)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if len(missing) > 0 {
			problems = append(problems, fmt.Sprintf("%s: the push sends commits no changelog entry of %q names: %s. Describe each from a directory of one of its members with `rlsbl changelog add --commits <commit> --description \"...\" --type <feature|fix|breaking>` (or `--user-facing=false` for a change no user sees), then push again", rel.Name, rel.Name, strings.Join(missing, ", ")))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d push coverage problem(s)", len(problems)), fmt.Sprintf("every pushed commit is covered (%d releasable(s) touched)", checked))
}

func checkPrepushGitignoreGuard(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	d := c.Declarations()
	seen := map[string]bool{}
	var paths []string
	for _, rel := range d.Releasables {
		for _, p := range changelog.TrackedRecords(d, rel.Name) {
			if !seen[p] {
				seen[p] = true
				paths = append(paths, p)
			}
		}
	}
	sort.Strings(paths)
	ignored, err := changelog.IgnoredPaths(c.Effects(), c.Root(), paths)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	finding := changelog.IgnoredRecordsFinding(c.Root(), ignored)
	if finding == "" {
		return r.Passed("no changelog record rlsbl keeps is ignored by git")
	}
	return reportErrors(r, []string{finding}, "changelog records are ignored by git", "")
}

func checkPrepushManualWarning(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	refs, inPush := pushRefs(c)
	if !inPush {
		return r.Skipped(notInPush)
	}
	branches := git.ManualPushBranches(refs, c.Declarations().ReleaseBranches)
	if len(branches) == 0 {
		return r.Passed("the push updates no release branch")
	}
	message := fmt.Sprintf("a push to the release branch %s by hand: a release branch moves only through `"+runstate.RunInvocation+"`, which pushes without running this hook, so never push it yourself", strings.Join(branches, ", "))
	return reportErrors(r, []string{message}, message, "")
}
