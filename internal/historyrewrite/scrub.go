package historyrewrite

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The scrub's own steps after safegit's rewrite, in order. Each completed
// step is saved in the scrub result, so a scrub that stops is finished by
// running the same command again, from the step that stopped it.
const (
	stepHashesValidated   = "hashes-validated"
	stepReleaseCommits    = "release-commits-remapped"
	stepChangelogVerified = "changelog-verified"
	stepCachesDeleted     = "validation-caches-deleted"
	stepCommitted         = "committed"
	stepBranchPushed      = "branch-pushed"
	stepTagsPushed        = "tags-pushed"
	stepReleasesUpdated   = "releases-updated"
)

var scrubSteps = []string{stepHashesValidated, stepReleaseCommits, stepChangelogVerified, stepCachesDeleted, stepCommitted, stepBranchPushed, stepTagsPushed, stepReleasesUpdated}

// pruneCommand is the cleanup that removes objects a rewrite left
// unreachable.
const pruneCommand = "git reflog expire --expire=now --all && git gc --prune=now"

// scrubState is the scrub result: what safegit's rewrite did and which of
// the scrub's steps have completed. It is run state, never committed.
type scrubState struct {
	Mode      string `json:"mode"`
	Reason    string `json:"reason"`
	StartedAt string `json:"started_at"`
	// SafegitArgs is the rewrite's safegit argv; only the scrub that made a
	// rewrite finishes it.
	SafegitArgs      []string          `json:"safegit_args"`
	Rewrites         map[string]string `json:"rewrites"`
	Tags             []TagRewrite      `json:"tags"`
	CommitsRewritten int               `json:"commits_rewritten"`
	OldHead          string            `json:"old_head"`
	NewHead          string            `json:"new_head"`
	CleanupOK        *bool             `json:"cleanup_ok"`
	CleanupErrors    []string          `json:"cleanup_errors"`
	// RemoteRefs is origin's refs as read before the rewrite: the lease of
	// every force-push.
	RemoteRefs map[string]string `json:"remote_refs"`
	// Unverified marks a rewrite safegit made and then failed after (its
	// verification or its cleanup); the next run asks safegit again first.
	Unverified         bool     `json:"unverified"`
	CompletedSteps     []string `json:"completed_steps"`
	RemappedFiles      []string `json:"remapped_files"`
	ReleaseCommitFiles []string `json:"release_commit_files"`
	DeletedCaches      []string `json:"deleted_caches"`
	ArchivePath        string   `json:"archive_path"`
	ReleasesWritten    int      `json:"releases_written"`
}

func (s *scrubState) done(step string) bool { return slices.Contains(s.CompletedSteps, step) }

func (s *scrubState) startedAt() (time.Time, error) {
	t, err := time.Parse(time.RFC3339, s.StartedAt)
	if err != nil {
		return time.Time{}, fmt.Errorf("the scrub result's started_at %q is not an RFC 3339 time: %w", s.StartedAt, err)
	}
	return t, nil
}

// loadScrubState reads the scrub result strictly. found is false when no
// scrub is in progress.
func loadScrubState(root string) (*scrubState, bool, error) {
	data, found, err := runstate.LoadScrubResult(root)
	if err != nil || !found {
		return nil, false, err
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var s scrubState
	if err := dec.Decode(&s); err != nil {
		return nil, false, fmt.Errorf("%s cannot be read (%v); it is the state of a scrub in progress, written by rlsbl. If no scrub is in progress any more, remove it and run the scrub again", runstate.ScrubResultPath, err)
	}
	return &s, true, nil
}

func saveScrubState(e *strictcli.Effects, root string, s *scrubState) error {
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	return runstate.SaveScrubResult(e, root, append(data, '\n'))
}

// complete records step as completed and saves the state.
func (s *scrubState) complete(e *strictcli.Effects, root, step string) error {
	if !s.done(step) {
		s.CompletedSteps = append(s.CompletedSteps, step)
	}
	return saveScrubState(e, root, s)
}

// newScrubState is the state of a rewrite safegit reported.
func newScrubState(req ScrubRequest, args []string, p *scrubPayload, remote map[string]string, started time.Time) *scrubState {
	s := &scrubState{
		Mode:          string(req.Mode),
		Reason:        req.Reason,
		StartedAt:     started.UTC().Format(time.RFC3339),
		SafegitArgs:   append([]string(nil), args...),
		Rewrites:      p.Rewrites,
		Tags:          p.Tags,
		OldHead:       p.OldHead,
		NewHead:       p.NewHead,
		CleanupOK:     p.CleanupOK,
		CleanupErrors: p.CleanupErrors,
		RemoteRefs:    remote,
	}
	if p.CommitsRewritten != nil {
		s.CommitsRewritten = *p.CommitsRewritten
	}
	if s.Rewrites == nil {
		s.Rewrites = map[string]string{}
	}
	if s.RemoteRefs == nil {
		s.RemoteRefs = map[string]string{}
	}
	return s
}

// composeRewrites folds a second safegit run's rewrite into the saved one:
// each original commit maps to where it ends up, a tag keeps its original
// old_sha with its final new_sha, and the second run's new head is the head.
func composeRewrites(saved *scrubState, again *scrubPayload) {
	if !again.rewritten() {
		return
	}
	composed := map[string]string{}
	written := map[string]bool{}
	for old, mid := range saved.Rewrites {
		written[mid] = true
		if final, ok := again.Rewrites[mid]; ok {
			composed[old] = final
		} else {
			composed[old] = mid
		}
	}
	for old, final := range again.Rewrites {
		if !written[old] {
			composed[old] = final
		}
	}
	saved.Rewrites = composed
	for _, t := range again.Tags {
		i := slices.IndexFunc(saved.Tags, func(s TagRewrite) bool { return s.Refname == t.Refname })
		if i >= 0 {
			saved.Tags[i].NewSHA = t.NewSHA
		} else {
			saved.Tags = append(saved.Tags, t)
		}
	}
	saved.NewHead = again.NewHead
	if again.CleanupOK != nil {
		saved.CleanupOK = again.CleanupOK
		saved.CleanupErrors = again.CleanupErrors
	}
}

// scrubRun is one `release scrub` in progress.
type scrubRun struct {
	ctx  *strictcli.Context
	e    *strictcli.Effects
	root string
	ws   *workspace.Workspace
	repo git.Repo
	req  ScrubRequest
	now  func() time.Time
}

func (r *scrubRun) say(line string) { r.ctx.Out(line) }

// Scrub runs `release scrub` in the repository rooted at root: safegit
// rewrites the history (remapping the changelog's commit ids at every
// rewritten commit), then the scrub validates the changelog's commit ids,
// moves the archives' release commits through the rewrite, verifies the
// generated changelogs, deletes the validation caches, commits, and
// force-pushes the branch and the moved tags with leases taken from origin
// before the rewrite, then rewrites each moved tag's GitHub Release in place.
// now dates the rewrite.
func Scrub(ctx *strictcli.Context, root string, req ScrubRequest, now func() time.Time) (err error) {
	if err := req.validate(); err != nil {
		return err
	}
	if req.Mode == ModeRecipe {
		if info, statErr := os.Stat(req.Recipe); statErr != nil || info.IsDir() {
			return fmt.Errorf("the recipe %s is not a file: name the scrub recipe's path, relative to this directory or absolute", req.Recipe)
		}
	}
	e := ctx.Effects()
	if err := requireSafegit(e); err != nil {
		return err
	}
	ws, err := workspace.Load(root)
	if err != nil {
		return err
	}
	repo, err := git.Open(e, root)
	if err != nil {
		return err
	}
	r := &scrubRun{ctx: ctx, e: e, root: root, ws: ws, repo: repo, req: req, now: now}
	fileOnDisk := false
	if req.Mode == ModeFile {
		if _, statErr := os.Lstat(filepath.Join(root, filepath.FromSlash(req.File))); statErr == nil {
			fileOnDisk = true
		} else if !errors.Is(statErr, fs.ErrNotExist) {
			return fmt.Errorf("reading %s: %w", req.File, statErr)
		}
	}
	args := req.safegitArgs(root, changelog.RemapGlobs(), fileOnDisk, false)

	state, resuming, err := loadScrubState(root)
	if err != nil {
		return err
	}
	if resuming {
		if err := r.requireSameScrub(state, args); err != nil {
			return err
		}
		if ctx.DryRun() {
			r.say(fmt.Sprintf("A scrub is in progress (%s). Without --dry-run this command finishes it, from: %s.", runstate.ScrubResultPath, strings.Join(r.remaining(state), ", ")))
			return nil
		}
	} else {
		if err := r.refuseStoppedReleases(); err != nil {
			return err
		}
		if err := r.requireReleaseBranch(); err != nil {
			return err
		}
		if ctx.DryRun() {
			// The rewrite is recorded, never run, under --dry-run: the would-do
			// log names the safegit invocation that prints safegit's own counts.
			if _, err := e.Run(argv("safegit", req.safegitArgs(root, changelog.RemapGlobs(), fileOnDisk, true)), strictcli.Cwd(root), strictcli.Timeout(safegitScrubTimeout)); err != nil {
				return err
			}
			r.say("The rewrite was recorded, not run: run the safegit command the preview names to see safegit's own match counts.")
			return nil
		}
	}

	lock, err := runstate.Acquire(e, root, runstate.AcquireOptions{Wait: runstate.WaitForHolder, OnWait: func(path string) {
		r.say(fmt.Sprintf("Another rlsbl process holds %s; waiting for it to finish before rewriting.", path))
	}})
	if err != nil {
		return err
	}
	defer func() {
		if rerr := lock.Release(); rerr != nil && err == nil {
			err = rerr
		}
	}()

	if resuming {
		r.say("Resuming the scrub saved in " + runstate.ScrubResultPath + ".")
		if state.Unverified {
			if err := r.verifySavedRewrite(state); err != nil {
				return err
			}
		}
	} else {
		if state, err = r.rewrite(args); err != nil || state == nil {
			return err
		}
	}
	return r.finish(state, resuming)
}

// remaining lists the steps a saved scrub has left.
func (r *scrubRun) remaining(s *scrubState) []string {
	var left []string
	if s.Unverified {
		left = append(left, "asking safegit again to confirm its rewrite")
	}
	for _, step := range scrubSteps {
		if !s.done(step) {
			left = append(left, step)
		}
	}
	return left
}

// requireSameScrub refuses a saved scrub that is not this one: HEAD must be
// its rewrite's new head (or its scrub commit on top of it), and its safegit
// invocation this run's. A saved rewrite is finished only by the scrub that
// made it.
func (r *scrubRun) requireSameScrub(s *scrubState, args []string) error {
	head, err := r.repo.Head()
	if err != nil {
		return err
	}
	if head != s.NewHead && !r.isScrubCommitOn(s, head) {
		return fmt.Errorf("%s holds a scrub whose rewrite left HEAD at %s, but HEAD is %s. Check out the branch that scrub rewrote and run it again to finish it; if that scrub was finished or given up some other way, remove %s and run this scrub again", runstate.ScrubResultPath, s.NewHead, head, runstate.ScrubResultPath)
	}
	if !slices.Equal(s.SafegitArgs, args) {
		return fmt.Errorf("%s holds a rewrite made by `safegit %s`, and this run would run `safegit %s`. Run `rlsbl release scrub` with the arguments the saved scrub was started with, so it finishes that scrub", runstate.ScrubResultPath, strings.Join(s.SafegitArgs, " "), strings.Join(args, " "))
	}
	return nil
}

// isScrubCommitOn reports whether head is the commit the committed step made
// on the rewrite's new head: its parent is the new head and its message
// names the rewrite's heads.
func (r *scrubRun) isScrubCommitOn(s *scrubState, head string) bool {
	if !s.done(stepCommitted) || s.NewHead == "" {
		return false
	}
	parent, found, err := r.repo.ResolveCommit(head + "^")
	if err != nil || !found || parent != s.NewHead {
		return false
	}
	message, err := r.repo.CommitMessage(head)
	return err == nil && strings.Contains(message, ".."+s.NewHead)
}

// refuseStoppedReleases refuses the scrub while a release is stopped
// mid-flight: its state records commits of the history about to be
// rewritten, and it resumes in the release checkout, which the scrub
// removes.
func (r *scrubRun) refuseStoppedReleases() error {
	names, err := runstate.InProgressReleasables(r.root)
	if err != nil || len(names) == 0 {
		return err
	}
	var lines []string
	for _, name := range names {
		where := "."
		if s, found, err := runstate.LoadInProgress(r.root, name); err == nil && found {
			if m, ok := r.ws.Declarations.Member(s.RepresentativeMember); ok {
				where = m.Path
			}
		}
		lines = append(lines, fmt.Sprintf("  %s (run the fix from %s)", runstate.InProgressPath(name), where))
	}
	return fmt.Errorf("a release is stopped mid-flight:\n%s\nThe scrub rewrites the history that release's state records and removes the release checkout it resumes in. Finish the release with `rlsbl release resume --watch` (or --no-watch), or give it up with `rlsbl release abandon`, from the directory named, then run this scrub again", strings.Join(lines, "\n"))
}

// requireReleaseBranch refuses a scrub off a release branch: the scrub
// force-pushes the branch it rewrote, and releases are the only writers of
// origin's branches.
func (r *scrubRun) requireReleaseBranch() error {
	branch, err := r.repo.CurrentBranch()
	if err != nil {
		return err
	}
	if !slices.Contains(r.ws.Declarations.ReleaseBranches, branch) {
		return fmt.Errorf("the scrub force-pushes the branch it rewrites, and %s is not a release branch (release_branches in %s: %s); check out a release branch and run the scrub there", branch, declarations.ReleasablesFile, strings.Join(r.ws.Declarations.ReleaseBranches, ", "))
	}
	return nil
}

// removeReleaseCheckout removes the release checkout, whose detached HEAD
// pins the history as it was before the rewrite. The next release creates
// it afresh.
func (r *scrubRun) removeReleaseCheckout() error {
	common, err := r.repo.CommonDir()
	if err != nil {
		return err
	}
	checkout := filepath.Join(common, "rlsbl", "release-checkout")
	resolved := checkout
	if p, err := filepath.EvalSymlinks(checkout); err == nil {
		resolved = p
	}
	worktrees, err := r.repo.Worktrees()
	if err != nil {
		return err
	}
	if !slices.Contains(worktrees, resolved) {
		return nil
	}
	if err := r.repo.RemoveWorktree(checkout); err != nil {
		return err
	}
	r.say(fmt.Sprintf("Removed the release checkout at %s: it pinned the history as it was before the rewrite. The next release creates it afresh.", checkout))
	return nil
}

// rewrite runs safegit's rewrite and saves what it did. It returns nil state
// when safegit found nothing to rewrite (after validating and repairing the
// changelog and the archives from an earlier rewrite's journal).
func (r *scrubRun) rewrite(args []string) (*scrubState, error) {
	remote, err := r.repo.RemoteRefsPeeled(origin)
	if err != nil {
		return nil, fmt.Errorf("origin's refs cannot be read (%v): a scrub force-pushes the rewritten history, so origin must answer before the history is rewritten", err)
	}
	if err := r.refuseStoppedReleases(); err != nil {
		return nil, err
	}
	if err := r.removeReleaseCheckout(); err != nil {
		return nil, err
	}
	started := r.now()
	done, err := r.e.Run(argv("safegit", args), strictcli.Cwd(r.root), strictcli.Check(false), strictcli.Timeout(safegitScrubTimeout))
	if err != nil {
		return nil, fmt.Errorf("safegit scrub could not be run: %w", err)
	}
	if done.ExitCode() != 0 {
		return nil, r.failedRewrite(done, args, remote, started)
	}
	payload, err := parseMachineDocument(done.Stdout())
	if err != nil {
		return nil, err
	}
	if !payload.rewritten() {
		r.say("No matches found, nothing to rewrite.")
		return nil, r.repairWithoutRewrite()
	}
	state := newScrubState(r.req, args, payload, remote, started)
	if err := saveScrubState(r.e, r.root, state); err != nil {
		return nil, err
	}
	return state, nil
}

// failedRewrite is the error of a safegit run that exited non-zero. safegit
// reports its rewrite even when a stage after it (its verification, its
// cleanup) failed: such a rewrite is saved, so running the scrub again asks
// safegit to confirm it and then finishes it, instead of finding nothing
// left to rewrite and never pushing the rewritten history.
func (r *scrubRun) failedRewrite(done strictcli.Completed, args []string, remote map[string]string, started time.Time) error {
	report := strings.TrimSpace(done.Stderr())
	payload, perr := parseMachineDocument(done.Stdout())
	if perr != nil || !payload.rewritten() {
		return fmt.Errorf("safegit scrub failed (exit %d):\n%s", done.ExitCode(), report)
	}
	head, err := r.repo.Head()
	if err != nil || head != payload.NewHead {
		return fmt.Errorf("safegit scrub failed (exit %d):\n%s", done.ExitCode(), report)
	}
	state := newScrubState(r.req, args, payload, remote, started)
	state.Unverified = true
	if err := saveScrubState(r.e, r.root, state); err != nil {
		return err
	}
	return unverifiedError(done.ExitCode(), report)
}

func unverifiedError(code int, report string) error {
	return fmt.Errorf("safegit rewrote the local history, then failed (exit %d):\n%s\nThe rewrite is saved in %s, and nothing was pushed: origin, its tags, and the release record still hold the old history. Remove what still holds the old history (safegit names the objects above; a git worktree checked out at an old commit is one such holder), run `%s`, and run this same command again: it asks safegit to confirm the content is gone, then finishes the scrub", code, report, runstate.ScrubResultPath, pruneCommand)
}

// verifySavedRewrite runs the saved scrub's safegit invocation again before
// finishing a rewrite safegit failed after. A run that fails again keeps the
// saved result; one that rewrites further is folded into it.
func (r *scrubRun) verifySavedRewrite(s *scrubState) error {
	done, err := r.e.Run(argv("safegit", s.SafegitArgs), strictcli.Cwd(r.root), strictcli.Check(false), strictcli.Timeout(safegitScrubTimeout))
	if err != nil {
		return fmt.Errorf("safegit scrub could not be run: %w", err)
	}
	if done.ExitCode() != 0 {
		if again, perr := parseMachineDocument(done.Stdout()); perr == nil && again.rewritten() {
			composeRewrites(s, again)
			if err := saveScrubState(r.e, r.root, s); err != nil {
				return err
			}
		}
		return unverifiedError(done.ExitCode(), strings.TrimSpace(done.Stderr()))
	}
	again, err := parseMachineDocument(done.Stdout())
	if err != nil {
		return err
	}
	if again != nil {
		composeRewrites(s, again)
		if again.CleanupOK != nil {
			s.CleanupOK = again.CleanupOK
			s.CleanupErrors = again.CleanupErrors
		}
	}
	s.Unverified = false
	if err := saveScrubState(r.e, r.root, s); err != nil {
		return err
	}
	r.say("safegit confirmed the saved rewrite; finishing the scrub.")
	return nil
}

// finish runs the scrub's steps after the rewrite, skipping the completed
// ones, and removes the scrub result once every step is done.
func (r *scrubRun) finish(s *scrubState, resuming bool) error {
	if len(s.Rewrites) == 0 {
		r.say("No matches found, nothing to rewrite.")
		if err := runstate.ClearScrubResult(r.e, r.root); err != nil {
			return err
		}
		return r.repairWithoutRewrite()
	}
	if !resuming {
		r.say(fmt.Sprintf("%d commit(s) rewritten, %d tag(s) moved. Repairing the records, then force-pushing the rewritten history.", len(s.Rewrites), len(s.Tags)))
	}
	if err := r.requireCleanup(s); err != nil {
		return err
	}
	for _, step := range []struct {
		name string
		run  func(*scrubState) error
	}{
		{stepHashesValidated, r.validateHashes},
		{stepReleaseCommits, r.moveReleaseCommits},
		{stepChangelogVerified, r.verifyChangelogs},
		{stepCachesDeleted, r.deleteValidationCaches},
		{stepCommitted, r.commit},
		{stepBranchPushed, r.pushBranch},
		{stepTagsPushed, r.pushTags},
		{stepReleasesUpdated, r.updateReleases},
	} {
		if s.done(step.name) {
			continue
		}
		if err := step.run(s); err != nil {
			return fmt.Errorf("%w\nThe scrub stopped before its %s step; %s is kept, so running the same command again resumes from there", err, step.name, runstate.ScrubResultPath)
		}
		if err := s.complete(r.e, r.root, step.name); err != nil {
			return err
		}
	}
	if err := runstate.ClearScrubResult(r.e, r.root); err != nil {
		return err
	}
	summary := fmt.Sprintf("Scrub complete. %d commit(s) rewritten, %d tag(s) pushed, %d GitHub Release(s) written.", len(s.Rewrites), len(s.Tags), s.ReleasesWritten)
	if n := len(s.RemappedFiles); n > 0 {
		summary += fmt.Sprintf(" %d changelog file(s) repaired from the rewrite journal.", n)
	}
	r.say(summary)
	return nil
}

// requireCleanup refuses to go on while safegit reported a failed cleanup
// and an object the rewrite replaced still exists: a changelog commit id
// that no longer names a commit is only detectable once the old commits are
// gone, so validating before the prune would pass what the next prune
// breaks. It asks the object store each time, so a prune completed by hand
// lets the next run go on.
func (r *scrubRun) requireCleanup(s *scrubState) error {
	if s.CleanupOK == nil || *s.CleanupOK {
		return nil
	}
	var old []string
	for from, to := range s.Rewrites {
		if from != to {
			old = append(old, from)
		}
	}
	// A rewrite of tag annotations alone maps every commit to itself and
	// leaves the head where it was; that head legitimately exists forever.
	if s.OldHead != "" && s.OldHead != s.NewHead && !slices.Contains(old, s.OldHead) {
		old = append(old, s.OldHead)
	}
	sort.Strings(old)
	var present []string
	for _, sha := range old {
		has, err := r.repo.HasObject(sha)
		if err != nil {
			return err
		}
		if has {
			present = append(present, sha)
		}
	}
	if len(present) == 0 {
		ok := true
		s.CleanupOK = &ok
		r.say("safegit reported a failed cleanup after its rewrite, and no object the rewrite replaced exists any more; going on.")
		return saveScrubState(r.e, r.root, s)
	}
	detail := ""
	if len(s.CleanupErrors) > 0 {
		detail = "\n  - " + strings.Join(s.CleanupErrors, "\n  - ")
	}
	return fmt.Errorf("safegit reported that its cleanup after the rewrite (reflog expiry, repack, prune) did not succeed:%s\n%d object(s) the rewrite replaced still exist (%s, for one). Validating the changelog's commit ids now would pass ids that the next prune breaks. Investigate the errors above, complete the prune (`%s`), and run the same command again; %s is kept", detail, len(present), short(present[0]), pruneCommand, runstate.ScrubResultPath)
}

// validateHashes requires every commit id of every changelog to name a
// commit after the rewrite. Ids safegit's in-history remap did not reach (a
// scrub stopped before this flow, a rewrite run outside it) are repaired
// from safegit's journal when its commit map renames them.
func (r *scrubRun) validateHashes(s *scrubState) error {
	repaired, unresolved, err := r.repairFromJournal()
	if err != nil {
		return err
	}
	s.RemappedFiles = mergePaths(s.RemappedFiles, repaired)
	if len(unresolved) > 0 {
		return fmt.Errorf("after the rewrite, changelog commit ids name no commit, and no rewrite journal renames them:\n%s\nFix the entries (`rlsbl changelog edit` or `rlsbl changelog remove`) and run the scrub again", describeUnresolved(unresolved))
	}
	return nil
}

// repairFromJournal remaps the changelog's unresolved commit ids through
// safegit's journal when its commit map renames one of them. It returns the
// files it rewrote and the ids still unresolved.
func (r *scrubRun) repairFromJournal() ([]string, map[string][]string, error) {
	unresolved, err := changelog.UnresolvedCommits(r.repo, r.root)
	if err != nil || len(unresolved) == 0 {
		return nil, unresolved, err
	}
	journal, found, err := ReadJournal(r.repo)
	if err != nil || !found {
		return nil, unresolved, err
	}
	fixable := false
	for _, ids := range unresolved {
		for _, id := range ids {
			if changelog.CanRemap(id, journal.CommitMap) {
				fixable = true
			}
		}
	}
	if !fixable {
		return nil, unresolved, nil
	}
	r.say(fmt.Sprintf("Changelog commit ids name no commit; repairing them from the safegit rewrite journal %s (rewrite %s, %s, created %s).", journal.Path, journal.ID, journal.Op, journal.CreatedAt))
	if !journal.Complete {
		r.say("That rewrite has a start record and no complete record: it stopped part-way. Its commit map was written before any ref moved, so it repairs the ids, but run `safegit doctor` before trusting the repository's state.")
	}
	report, err := changelog.Remap(r.e, r.root, journal.CommitMap)
	if err != nil {
		return nil, nil, err
	}
	var repaired []string
	for _, f := range report.Files {
		r.say(fmt.Sprintf("  repaired %s: %d commit id(s) in %d entr(ies)", f.Path, f.CommitsRemapped, f.EntriesModified))
		repaired = append(repaired, f.Path)
	}
	unresolved, err = changelog.UnresolvedCommits(r.repo, r.root)
	return repaired, unresolved, err
}

func describeUnresolved(unresolved map[string][]string) string {
	var lines []string
	for _, file := range sortedKeys(unresolved) {
		lines = append(lines, fmt.Sprintf("  %s: %s", file, strings.Join(unresolved[file], ", ")))
	}
	return strings.Join(lines, "\n")
}

// moveReleaseCommits moves every archive's release commit through the
// rewrite. Released trees change by construction here (safegit rewrote the
// changelog files inside them), so the scrub declares the record answer:
// the new trees are recorded and each changed path is printed.
func (r *scrubRun) moveReleaseCommits(s *scrubState) error {
	remaps, files, err := releaserecord.RepairReleaseCommits(r.e, r.repo, s.Rewrites, "rlsbl release scrub to "+s.NewHead, releaserecord.ContentChangeRecord, r.now())
	if err != nil {
		return err
	}
	if len(remaps) > 0 {
		r.say(fmt.Sprintf("Moved %d release commit(s) through the rewrite:", len(remaps)))
		for _, m := range remaps {
			r.say(fmt.Sprintf("  %s %s: %s -> %s", m.Dir, m.Version, short(m.OldCommit), short(m.NewCommit)))
			for _, c := range m.Changed {
				r.say(fmt.Sprintf("    content changed at %s: %s -> %s (the rewrite altered what this version's tree holds; the archive now records the rewritten tree)", c.Path, short(c.Recorded), short(c.Found)))
			}
		}
	}
	s.ReleaseCommitFiles = mergePaths(s.ReleaseCommitFiles, files)
	return nil
}

// verifyChangelogs requires every generated changelog to be what generating
// it gives: with the ids remapped inside the history, the changelog at HEAD
// is already consistent, so a difference means something else is wrong (a
// hand-edited CHANGELOG.md, one generated by another rlsbl). Nothing is
// written.
func (r *scrubRun) verifyChangelogs(_ *scrubState) error {
	d := r.ws.Declarations
	type generated struct{ rel, content string }
	var outputs []generated
	for _, rel := range d.Releasables {
		if info, err := os.Stat(filepath.Join(r.root, filepath.FromSlash(changelog.Dir(rel.Name)))); err != nil || !info.IsDir() {
			continue
		}
		content, err := changelog.Document(r.root, rel.Name, nil)
		if err != nil {
			return err
		}
		outputs = append(outputs, generated{changelog.Home(d, rel.Name), content})
	}
	if d.IsWorkspace() && len(outputs) > 0 {
		content, err := changelog.RollUp(r.root, d, "", nil)
		if err != nil {
			return err
		}
		outputs = append(outputs, generated{changelog.RollUpPath, content})
	}
	var differences []string
	for _, o := range outputs {
		current, err := os.ReadFile(filepath.Join(r.root, filepath.FromSlash(o.rel)))
		if err != nil && !errors.Is(err, fs.ErrNotExist) {
			return fmt.Errorf("reading %s: %w", o.rel, err)
		}
		if string(current) != o.content {
			differences = append(differences, firstDifference(o.rel, string(current), o.content))
		}
	}
	if len(differences) > 0 {
		return fmt.Errorf("a generated changelog is not what generating it gives, though the rewrite left the changelog consistent, so something else changed it (a hand edit, another rlsbl's generation); nothing was written:\n%s\nIf the difference is generation drift, generate the changelog with `rlsbl changelog generate`, commit the result, and run the scrub again", strings.Join(differences, "\n"))
	}
	return nil
}

// firstDifference names a file's first line that differs from what
// generating it gives.
func firstDifference(rel, have, want string) string {
	if have == "" {
		return "  " + rel + ": missing"
	}
	haveLines := strings.Split(have, "\n")
	wantLines := strings.Split(want, "\n")
	for i := 0; i < max(len(haveLines), len(wantLines)); i++ {
		var h, w string
		if i < len(haveLines) {
			h = haveLines[i]
		}
		if i < len(wantLines) {
			w = wantLines[i]
		}
		if h != w {
			return fmt.Sprintf("  %s, line %d:\n    on disk:   %q\n    generated: %q", rel, i+1, h, w)
		}
	}
	return "  " + rel + ": differs"
}

// deleteValidationCaches deletes every changelog validation cache: each
// names commits of the history the rewrite replaced. A tracked cache's
// deletion is committed.
func (r *scrubRun) deleteValidationCaches(s *scrubState) error {
	dir := filepath.Join(r.root, filepath.FromSlash(declarations.ChangelogValidationDir))
	entries, err := os.ReadDir(dir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("listing %s: %w", declarations.ChangelogValidationDir, err)
	}
	tracked, err := r.repo.TrackedFiles()
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".toml") {
			continue
		}
		rel := declarations.ChangelogValidationDir + "/" + entry.Name()
		if _, err := r.e.Remove(filepath.Join(dir, entry.Name())); err != nil {
			return fmt.Errorf("removing %s: %w", rel, err)
		}
		if slices.Contains(tracked, rel) {
			s.DeletedCaches = mergePaths(s.DeletedCaches, []string{rel})
		}
	}
	return nil
}

// commit commits the scrub's own files: the changelog files the journal
// repaired, the moved archives and the transition record, the deleted
// caches, and the rewrite's archive, which is always new, so the commit is
// never empty. Its trailer names the rewrite's heads.
func (r *scrubRun) commit(s *scrubState) error {
	started, err := s.startedAt()
	if err != nil {
		return err
	}
	if s.ArchivePath == "" {
		rel, err := WriteRewriteArchive(r.e, r.root, started, RewriteArchive{
			Operation:        "scrub",
			Mode:             s.Mode,
			Reason:           s.Reason,
			OldHead:          s.OldHead,
			NewHead:          s.NewHead,
			CommitsRewritten: s.CommitsRewritten,
			Rewrites:         s.Rewrites,
			Tags:             archivedTags(s.Tags),
		})
		if err != nil {
			return err
		}
		s.ArchivePath = rel
		if err := saveScrubState(r.e, r.root, s); err != nil {
			return err
		}
	}
	// The ownership manifests a first write into a directory creates are
	// committed with the records; one already committed and unchanged is
	// left out of the commit.
	paths := mergePaths(mergePaths(mergePaths(s.RemappedFiles, s.ReleaseCommitFiles), s.DeletedCaches), []string{s.ArchivePath, rewritesManifest, transitionsManifest})
	if _, err := r.repo.Commit(git.CommitRequest{
		Message:       fmt.Sprintf("scrub: %s\n\nScrub-remap: %s..%s", s.Reason, s.OldHead, s.NewHead),
		Paths:         paths,
		RequireChange: true,
	}); err != nil {
		return fmt.Errorf("the scrub's records could not be committed (%w); nothing was pushed, since publishing the rewritten history without them would publish inconsistent records", err)
	}
	return nil
}

func archivedTags(tags []TagRewrite) []ArchivedTag {
	out := make([]ArchivedTag, len(tags))
	for i, t := range tags {
		out[i] = ArchivedTag{Refname: t.Refname, OldSHA: t.OldSHA, NewSHA: t.NewSHA, Annotated: t.Annotated}
	}
	return out
}

func (r *scrubRun) pushTimeout() (time.Duration, error) {
	return PushTimeout(r.ws.Declarations, 0, false)
}

// pushBranch force-pushes the rewritten branch, the scrub commit on top,
// with the lease origin's value before the rewrite gives it.
func (r *scrubRun) pushBranch(s *scrubState) error {
	timeout, err := r.pushTimeout()
	if err != nil {
		return err
	}
	branch, err := r.repo.CurrentBranch()
	if err != nil {
		return err
	}
	ref := "refs/heads/" + branch
	target, found, err := r.repo.ResolveCommit(ref)
	if err != nil {
		return err
	}
	if !found {
		return fmt.Errorf("the branch %s names no commit", branch)
	}
	return pushWithLease(r.repo, ref, s.RemoteRefs[ref], target, timeout)
}

// pushTags force-pushes every tag the rewrite moved, each with its lease.
func (r *scrubRun) pushTags(s *scrubState) error {
	timeout, err := r.pushTimeout()
	if err != nil {
		return err
	}
	return pushRewrittenTags(r.repo, s.Tags, s.RemoteRefs, timeout)
}

// updateReleases rewrites the GitHub Release document of every moved tag.
func (r *scrubRun) updateReleases(s *scrubState) error {
	if len(s.Tags) == 0 {
		return nil
	}
	repair, err := newReleaseRepair(r.e, r.repo, r.ws, s.Rewrites, r.say)
	if err != nil {
		return err
	}
	written, err := RewriteReleases(repair, s.Tags)
	s.ReleasesWritten = written
	return err
}

// newReleaseRepair binds the Release repair to the repository's GitHub
// repository: the declared github_repository, else the one origin names.
func newReleaseRepair(e *strictcli.Effects, repo git.Repo, ws *workspace.Workspace, rewrites map[string]string, say func(string)) (ReleaseRepair, error) {
	gh, err := github.New(e)
	if err != nil {
		return ReleaseRepair{}, err
	}
	url := ""
	if ws.Declarations.GitHubRepository == "" {
		if url, err = repo.RemoteURL(origin); err != nil {
			return ReleaseRepair{}, err
		}
	}
	repository, err := github.ResolveRepository(ws.Declarations.GitHubRepository, url)
	if err != nil {
		return ReleaseRepair{}, err
	}
	record, err := lifecycle.Load(ws.Root)
	if err != nil {
		return ReleaseRepair{}, err
	}
	return ReleaseRepair{Repo: repo, Workspace: ws, Record: record, GitHub: gh, Repository: repository, Rewrites: rewrites, Say: say}, nil
}

// repairWithoutRewrite validates the changelog when safegit found nothing to
// rewrite: the moment damage a crashed or direct rewrite left is still
// cheaply repairable, with safegit's journal at hand. Changelog ids and the
// archives' release commits are repaired from the journal and committed; an
// id the journal cannot repair is an error naming it. Nothing is pushed:
// no rewrite happened here.
func (r *scrubRun) repairWithoutRewrite() error {
	repaired, unresolved, err := r.repairFromJournal()
	if err != nil {
		return err
	}
	journal, found, err := ReadJournal(r.repo)
	if err != nil {
		return err
	}
	if found && len(journal.CommitMap) > 0 {
		remaps, files, err := releaserecord.RepairReleaseCommits(r.e, r.repo, journal.CommitMap, journal.Label(), releaserecord.ContentChangeRefuse, r.now())
		if err != nil {
			return err
		}
		for _, m := range remaps {
			r.say(fmt.Sprintf("  re-recorded %s %s: %s -> %s", m.Dir, m.Version, short(m.OldCommit), short(m.NewCommit)))
		}
		repaired = mergePaths(repaired, files)
	}
	if len(unresolved) > 0 {
		note := ""
		if len(repaired) > 0 {
			note = fmt.Sprintf("\n(%d file(s) were repaired from the rewrite journal and are left uncommitted: %s.)", len(repaired), strings.Join(repaired, ", "))
		}
		return fmt.Errorf("the scrub found nothing to rewrite, but changelog commit ids name no commit (left, likely, by an earlier rewrite), and the rewrite journal cannot repair them:\n%s%s\nFix the entries (`rlsbl changelog edit` or `rlsbl changelog remove`) and run the scrub again", describeUnresolved(unresolved), note)
	}
	if len(repaired) == 0 {
		return nil
	}
	if _, err := r.repo.Commit(git.CommitRequest{
		Message:       "scrub: repair changelog commit ids and release commits from the rewrite journal",
		Paths:         repaired,
		RequireChange: true,
	}); err != nil {
		return err
	}
	r.say(fmt.Sprintf("Committed %d file(s) repaired from the rewrite journal.", len(repaired)))
	return nil
}

// mergePaths is a and b, each path once, in order.
func mergePaths(a, b []string) []string {
	out := append([]string(nil), a...)
	for _, p := range b {
		if !slices.Contains(out, p) {
			out = append(out, p)
		}
	}
	return out
}
