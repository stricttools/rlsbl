package release

import (
	"errors"
	"fmt"
	"path/filepath"
	"slices"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/ci"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A batch release (`rlsbl monorepo release run`) releases several
// releasables of a workspace in one session, in two passes. The first
// releases each one up to its release commit and stops there, its state kept
// (ReleaseBatchMember). Then the branch tip, holding every releasable's
// release commit, is pushed as one candidate (PublishBatchCandidate) and CI
// judges it once, applying every releasable's own check filters
// (GateBatchCandidate): pushing per releasable would give each push a window
// touching one releasable's paths, and the CI router would skip every other
// releasable's jobs on the commit their tags point at. The second pass
// continues each releasable from the verdict on the candidate CI verified
// (CompleteBatchMember): it finalizes, tags, and publishes, as a resume does.

// RerunBatch is what a refusal of a batch release tells the operator to run
// once the cause is dealt with.
const RerunBatch = "run `rlsbl monorepo release run --watch` again"

// batchRole is a release's part in a batch release; the zero value is a
// release of its own.
type batchRole struct {
	// file is the releasable's table of the batch release file, which the
	// release reads instead of a release file of its own.
	file *releaserecord.ReleaseFile
	// deferCandidate stops the release after its release commit: the batch
	// pushes the candidate and waits for CI.
	deferCandidate bool
	// verified is the candidate CI verified for the whole batch, which the
	// release takes as its release commit instead of pushing one.
	verified string
	// rerun replaces what a stop tells the operator to run.
	rerun string
}

// releaseFileSource names where a release's fields come from, for its
// refusals.
type releaseFileSource struct {
	// file is the release file's repository-relative path, or the
	// description of a batch release file's table.
	file string
	// table is true for a table of the batch release file.
	table bool
}

// described names the source in a sentence.
func (s releaseFileSource) described() string {
	if s.table {
		return s.file
	}
	return "the release file " + s.file
}

// batchSource is the source of a batch releasable's fields: its table of the
// batch release file. A releasable that also has a release file of its own
// is refused: a release reads one of the two, and its archive would record
// the other.
func batchSource(root, liveRoot, releasable string) (releaseFileSource, error) {
	own := releaserecord.ReleaseFilePath(releasable)
	exists, err := fileExists(filepath.Join(root, filepath.FromSlash(own)))
	if err != nil {
		return releaseFileSource{}, err
	}
	if exists {
		return releaseFileSource{}, refuse("the releasable %q has a release file of its own, %s, and a [releasables.%s] table in the batch release file %s; a release takes its fields from one of them. Delete the release file and commit the deletion (saferm delete --on-error abort --description \"a release file the batch release replaces\" %s, then safegit commit -m \"Release %s through the batch release file\" -- %s), or remove the table from %s, then %s", releasable, own, releasable, releaserecord.BatchReleaseFilePath, filepath.Join(liveRoot, filepath.FromSlash(own)), releasable, own, releaserecord.BatchReleaseFilePath, RerunBatch)
	}
	return releaseFileSource{file: fmt.Sprintf("the [releasables.%s] table of %s", releasable, releaserecord.BatchReleaseFilePath), table: true}, nil
}

// ValidateBatchMember is Validate for a releasable of a batch release: the
// release's fields are rf, its table of the batch release file, and a
// release file of its own is refused. req.Dir is a directory of one of its
// members.
func ValidateBatchMember(e *strictcli.Effects, req Request, rf releaserecord.ReleaseFile) (*Validated, error) {
	return validate(e, req, &rf)
}

// ReleasableTargets are the names of the targets of the releasable's
// members, each once, in the order the members and their targets come.
func ReleasableTargets(ws *workspace.Workspace, r declarations.Releasable) ([]string, error) {
	return releasableTargets(ws, r)
}

// ReleaseBatchMember is the first pass of a batch release for one
// releasable: Release with the fields of its table rf, stopping after the
// release commit, which reaches the branch unpushed, with the release's
// state kept for the candidate push. req.Dir is a directory of one of its
// members. A stop discards the attempt as a release does, taking back only
// the advance this releasable made.
func ReleaseBatchMember(e *strictcli.Effects, s *Session, req RunRequest, rf releaserecord.ReleaseFile) error {
	if s.Checkout != nil {
		// An earlier releasable's advance is the batch's, never this one's
		// to take back.
		s.Checkout.last = nil
	}
	return release(e, s, req, batchRole{file: &rf, deferCandidate: true, rerun: RerunBatch})
}

// batchStates are the in-progress states of the releasables, refusing one
// that is gone.
func batchStates(liveRoot string, releasables []string) (map[string]runstate.InProgress, error) {
	out := map[string]runstate.InProgress{}
	for _, name := range releasables {
		st, found, err := runstate.LoadInProgress(liveRoot, name)
		if err != nil {
			return nil, err
		}
		if !found {
			return nil, fmt.Errorf("the release state of %s (%s) is gone, and the batch release committed its release; find out what removed it before running the batch again", name, filepath.Join(liveRoot, filepath.FromSlash(runstate.InProgressPath(name))))
		}
		out[name] = st
	}
	return out, nil
}

// union is list with every item of more it lacks appended, in order.
func union(list, more []string) []string {
	out := append([]string(nil), list...)
	for _, m := range more {
		if !slices.Contains(out, m) {
			out = append(out, m)
		}
	}
	return out
}

// PublishBatchCandidate pushes the branch tip, which holds the release
// commit of every pending releasable, to origin as the batch's one
// candidate, untagged and guarded by what origin held. It refuses first,
// pushing nothing, commits on the branch since pin that no pending
// releasable made, and a candidate whose diff window cannot trigger the CI
// of one of them (named, its state recording the refusal). A releasable
// whose candidate was published before and whose window triggers nothing
// owes a run_all dispatch of the CI router, made once for the batch after
// the push. Each pending releasable's state then records the candidate as
// pushed, and takes every commit of the batch among its own. It returns the
// candidate.
func PublishBatchCandidate(e *strictcli.Effects, s *Session, req RunRequest, pending []string, pin string) (string, error) {
	if s.Checkout == nil {
		return "", errors.New("a batch release pushes its candidate from the release checkout, and this session holds none")
	}
	co := s.Checkout
	if err := co.Advance("HEAD", "The batch release", RerunBatch); err != nil {
		return "", err
	}
	ws, err := workspace.Load(s.Root)
	if err != nil {
		return "", err
	}
	timeouts, err := ResolveTimeouts(ws.Declarations, req.Timeouts)
	if err != nil {
		return "", err
	}
	states, err := batchStates(s.LiveRoot, pending)
	if err != nil {
		return "", err
	}
	var trail []string
	for _, name := range pending {
		trail = union(trail, states[name].CreatedCommits)
	}
	live, ref, branch := co.LiveRepo(), co.BranchRef(), co.Branch
	if err := GuardForeignCommits(live, ref, pin, trail, "batch candidate push", RerunBatch); err != nil {
		return "", err
	}
	sha := co.Tip
	remote, found, err := live.RemoteRef(origin, ref)
	if err != nil {
		return "", fmt.Errorf("origin's %s could not be read, so the batch candidate push has no lease to go by: %w", branch, err)
	}
	if !found {
		remote = ""
	}
	needsPush := remote != sha
	for _, name := range pending {
		st := states[name]
		v, err := semver.Parse(st.Version)
		if err != nil {
			return "", fmt.Errorf("the release state of %s names the version %q: %w", name, st.Version, err)
		}
		window, err := judgeCandidateWindow(co.Repo(), ws, name, sha, remote, needsPush, st.Completed(StepCandidatePushed), st.CreatedCommits, v, st.Tag, branch, RerunBatch)
		if err != nil {
			st.Fail(StepCandidatePushed, err.Error())
			if serr := runstate.SaveInProgress(e, s.LiveRoot, st); serr != nil {
				return "", errors.Join(err, serr)
			}
			return "", err
		}
		if window == windowOwesDispatch {
			st.RunAllDispatchFor = sha
			if err := runstate.SaveInProgress(e, s.LiveRoot, st); err != nil {
				return "", err
			}
			states[name] = st
			req.Log(fmt.Sprintf("The push of %s cannot trigger the CI of %s, and its candidate was published once before: the CI router is dispatched with run_all=true after the push", short(sha), name))
		}
	}
	if needsPush {
		if err := live.Push(origin, git.RefUpdate{Ref: ref, New: sha, Expected: remote}, timeouts.Push); err != nil {
			return "", fmt.Errorf("the batch release candidate %s could not be pushed to origin/%s: %w\nNothing was tagged, released, or finalized, and no version is burnt: the release commits of %s stay on %s and their release states are kept. Once pushing works, %s; it continues them", short(sha), branch, err, strings.Join(pending, ", "), branch, RerunBatch)
		}
		req.Log(fmt.Sprintf("Pushed the batch release candidate %s to origin/%s (untagged, %d releasable(s))", short(sha), branch, len(pending)))
	} else {
		req.Log(fmt.Sprintf("origin/%s is at the batch candidate %s already", branch, short(sha)))
	}
	owed := false
	for _, name := range pending {
		if states[name].RunAllDispatchFor == sha {
			owed = true
		}
	}
	if owed {
		w, err := batchWatcher(e, live, ws, req)
		if err != nil {
			return "", err
		}
		if _, err := w.DispatchRunAll(branch, sha); err != nil {
			return "", err
		}
	}
	for _, name := range pending {
		st := states[name]
		if st.RunAllDispatchFor == sha {
			st.RunAllDispatchFor = ""
		}
		st.ReleaseCommit = sha
		st.CreatedCommits = union(st.CreatedCommits, trail)
		st.Complete(StepCandidatePushed)
		if err := runstate.SaveInProgress(e, s.LiveRoot, st); err != nil {
			return "", err
		}
	}
	return sha, nil
}

// batchWatcher watches CI runs of the workspace's GitHub repository.
func batchWatcher(e *strictcli.Effects, live git.Repo, ws *workspace.Workspace, req RunRequest) (ci.Watcher, error) {
	gh, err := github.New(e)
	if err != nil {
		return ci.Watcher{}, err
	}
	ghRepo, err := releaseRepository(live, ws.Declarations)
	if err != nil {
		return ci.Watcher{}, err
	}
	return ci.Watcher{GH: gh, Repo: ghRepo, Log: req.Log, Sleep: req.Sleep, Now: req.Now}, nil
}

// GateBatchCandidate waits for CI's verdict on the batch's candidate,
// applying the check filters of every pending releasable, so a releasable
// whose own jobs never ran on it stops the whole batch. Anything but a pass
// (or a repository declaring no push-triggered CI, which is said out loud)
// is a *CIError, recorded against the ci-verified step of every pending
// releasable's state: nothing is tagged, and the next batch run continues
// each releasable at its version.
func GateBatchCandidate(e *strictcli.Effects, s *Session, req RunRequest, pending []string, candidate string) error {
	ws, err := workspace.Load(s.Root)
	if err != nil {
		return err
	}
	timeouts, err := ResolveTimeouts(ws.Declarations, req.Timeouts)
	if err != nil {
		return err
	}
	live, err := git.Open(e, s.LiveRoot)
	if err != nil {
		return err
	}
	var filters []ci.CheckFilter
	for _, name := range pending {
		f, err := ci.ReleasableFilters(ws, name)
		if err != nil {
			return err
		}
		filters = append(filters, f...)
	}
	w, err := batchWatcher(e, live, ws, req)
	if err != nil {
		return err
	}
	fail := func(message string) error {
		states, err := batchStates(s.LiveRoot, pending)
		if err != nil {
			return errors.Join(&CIError{Message: message}, err)
		}
		for _, name := range pending {
			st := states[name]
			st.Fail(StepCIVerified, message)
			if err := runstate.SaveInProgress(e, s.LiveRoot, st); err != nil {
				return errors.Join(&CIError{Message: message}, err)
			}
		}
		return &CIError{Message: message}
	}
	branch := ""
	if s.Checkout != nil {
		branch = s.Checkout.Branch
	}
	untagged := fmt.Sprintf("Nothing in this batch was tagged, released, or finalized, and no version is burnt: the release commit of every releasable (%s) is on %s, which carries the candidate until CI passes.", strings.Join(pending, ", "), branch)
	verdict, results, err := w.WaitForCIGreen(ci.WaitInputs{Commit: candidate, Root: ws.Root, Timeout: timeouts.CI, DiscoveryGrace: ci.DiscoveryGrace, Filters: filters, Label: "the batch candidate " + short(candidate)})
	var notRun *ci.NotRunError
	var noRun *ci.WaitError
	switch {
	case errors.As(err, &notRun):
		return fail(strings.Join([]string{
			fmt.Sprintf("CI never ran for a releasable of this batch on the release candidate %s.", candidate),
			"  " + notRun.Message,
			"",
			"This is no CI failure: the runs concluded, and that releasable's own jobs in them never ran, so nothing was proven about the candidate for it. The publish workflow applies the same filter, so tagging now would make tags for versions that can never publish.",
			untagged,
			"",
			fmt.Sprintf("Commit a change under one of the paths that releasable's filter in .github/workflows/ci-router.yml lists (record it with `rlsbl changelog add`), then %s: each releasable continues at its own version.", RerunBatch),
		}, "\n"))
	case errors.As(err, &noRun):
		return fail(batchRedMessage(candidate, noRun.Message, untagged, branch))
	case err != nil:
		return err
	}
	switch verdict {
	case ci.NotConfigured:
		req.Warn("rlsbl: no workflow in .github/workflows triggers on push, so the batch release proceeds with no CI verdict on its candidate")
	case ci.Green:
		req.Log(fmt.Sprintf("CI passed on the batch candidate %s", short(candidate)))
	case ci.Timeout:
		var open []string
		for _, r := range results {
			if !r.Passed {
				open = append(open, r.Name)
			}
		}
		return fail(strings.Join([]string{
			fmt.Sprintf("The CI wait for the batch release candidate %s ran out of time before every run concluded.", candidate),
			fmt.Sprintf("  Runs unresolved after %s: %s", timeouts.CI, strings.Join(orNone(open), ", ")),
			"",
			"This is no CI failure: those runs may still be going, and nothing was proven about the candidate either way.",
			untagged,
			"",
			fmt.Sprintf("Watch the runs with `rlsbl watch %s`. Once they pass, %s: each releasable continues at its own version. If CI is merely slow, give the wait more time with --ci-timeout.", short(candidate), RerunBatch),
		}, "\n"))
	default:
		var failed []string
		for _, r := range results {
			if !r.Passed && !r.Unresolved {
				failed = append(failed, r.Name)
			}
		}
		return fail(batchRedMessage(candidate, "Failing runs: "+strings.Join(orNone(failed), ", "), untagged, branch))
	}
	return nil
}

func batchRedMessage(candidate, detail, untagged, branch string) string {
	return strings.Join([]string{
		fmt.Sprintf("CI did not pass on the batch release candidate %s.", candidate),
		"  " + detail,
		"",
		untagged,
		fmt.Sprintf("Fix forward on %s (record each fix with `rlsbl changelog add`), then %s: each releasable continues at its own version.", branch, RerunBatch),
	}, "\n")
}

// CompleteBatchMember is the second pass of a batch release for one
// releasable: its release continues as a resume does, taking verified, the
// candidate CI verified for the batch, as its release commit, and walks the
// steps from the finalized changelog to the post-release hooks. own are the
// commits the batch made since the releasable's release commit (the other
// releasables' finalization commits), taken among the release's own so the
// resume does not adopt them as somebody else's.
func CompleteBatchMember(e *strictcli.Effects, s *Session, req RunRequest, releasable, verified string, own []string) error {
	states, err := batchStates(s.LiveRoot, []string{releasable})
	if err != nil {
		return err
	}
	st := states[releasable]
	if !st.Completed(StepCandidatePushed) || st.ReleaseCommit != verified {
		return fmt.Errorf("the release state of %s records no push of the batch candidate %s, so the batch cannot finish it on that candidate; find out what rewrote %s, then %s", releasable, short(verified), filepath.Join(s.LiveRoot, filepath.FromSlash(runstate.InProgressPath(releasable))), RerunBatch)
	}
	st.CreatedCommits = union(st.CreatedCommits, own)
	if err := runstate.SaveInProgress(e, s.LiveRoot, st); err != nil {
		return err
	}
	if s.Checkout != nil {
		s.Checkout.last = nil
	}
	return resumeIn(e, s, req, releasable, batchRole{verified: verified})
}

// adoptBatchCandidate takes the candidate-pushed and ci-verified steps of a
// releasable's second batch pass: the batch pushed the candidate and CI
// verified it for every releasable of the batch, so the release records it
// as its release commit, provided it is still on the release branch.
func (x *execution) adoptBatchCandidate() error {
	if !x.state.Completed(StepCandidatePushed) {
		return fmt.Errorf("the batch verified the candidate %s, and the release state of %s records no push of it; %s", short(x.batch.verified), x.name(), RerunBatch)
	}
	verdict, err := x.live.Ancestry(x.batch.verified, x.branchRef())
	if err != nil {
		return err
	}
	if verdict != git.IsAncestor {
		return &UnverifiedCandidateError{Message: fmt.Sprintf("the batch candidate CI verified for %s, %s, is not on %s (%s), so the release cannot claim CI verification for %s. Put that commit back on the release branch and run `rlsbl release resume --watch`", x.version, short(x.batch.verified), x.branch, verdict, x.version)}
	}
	x.candidate = x.batch.verified
	x.pushed = true
	x.state.ReleaseCommit = x.candidate
	x.req.Log(fmt.Sprintf("CI verified the batch candidate %s for %s", short(x.candidate), x.name()))
	return x.complete(StepCIVerified)
}
