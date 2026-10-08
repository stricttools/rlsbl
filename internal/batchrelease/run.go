package batchrelease

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A batch release releases the releasables its batch release file names,
// in ReleasableOrder, in one session in the release checkout, in the two
// passes internal/release describes beside ReleaseBatchMember (each
// releasable up to its release commit, one candidate and one CI verdict,
// then each releasable finished on that candidate). The first run plans the
// batch: every releasable is validated before anything is written, and the
// version and tag each one ships are frozen in the run state's batch plan,
// with the blob of the batch release file it runs from. A later run (a
// batch stopped part of the way) follows the plan, never plans again: a
// releasable the plan names as released (its version file at the planned
// version, its tag here and on origin) is skipped, one whose release is in
// progress short of the CI verdict joins the run's candidate, and one past
// the verdict is refused, naming `rlsbl release resume`, since a new
// candidate would move the commit its verdict is about. The run that
// releases the last releasable archives the batch release file, commits the
// archive, pushes it, and removes the plan.

// ArchiveCommitPrefix begins the subject of the commit archiving a batch
// release file.
const ArchiveCommitPrefix = "chore: archive the batch release file as "

// RootSelfdocCommitMessage is the subject of the commit of what selfdoc
// wrote at a workspace's root.
const RootSelfdocCommitMessage = "selfdoc: regenerate (workspace root)"

// Run is `rlsbl monorepo release run`: it takes the workspace for the write
// scope of a batch release (release.Enter with
// release.WorkspaceWriteScope) and releases the batch in it. Under
// --dry-run it validates every releasable the batch would release and
// reports the plan, writing nothing.
func Run(e *strictcli.Effects, req release.RunRequest) (err error) {
	if req.Releasable != "" {
		return errors.New("a batch release takes no --releasable: the batch release file names the releasables it releases")
	}
	if req.Checks == nil || req.Now == nil || req.Sleep == nil || req.Log == nil || req.Warn == nil || req.IndexPath == "" {
		return errors.New("a batch release needs the check runner, a clock, a sleep, a log, a warning stream, and the path of the confidential-name index")
	}
	live, err := git.Open(e, req.LiveRoot)
	if err != nil {
		return err
	}
	ws, err := workspace.Load(req.LiveRoot)
	if err != nil {
		return err
	}
	if err := requireWorkspace(ws, "run"); err != nil {
		return err
	}
	scope, err := release.WorkspaceWriteScope(ws)
	if err != nil {
		return err
	}
	log := req.Log
	session, err := release.Enter(e, live, scope, release.EnterOptions{
		DryRun: req.DryRun, What: "The batch release", Rerun: release.RerunBatch,
		OnWait: func(p string) {
			log(fmt.Sprintf("Another rlsbl process holds the release lock (%s); waiting for it", p))
		},
	})
	if err != nil {
		return err
	}
	defer func() { err = errors.Join(err, session.Close()) }()
	b := &batch{e: e, s: session, req: req}
	return b.run()
}

// batch is one run of a batch release.
type batch struct {
	e   *strictcli.Effects
	s   *release.Session
	req release.RunRequest
	// ws, live, and file are read once the session holds the repository.
	ws   *workspace.Workspace
	live git.Repo
	file releaserecord.BatchReleaseFile
	// blob is the git blob id of the batch release file.
	blob  string
	order []string
	plan  runstate.BatchPlan
	// planned is false when no batch was in progress: the run plans one.
	planned bool
}

func (b *batch) run() error {
	if report := release.IgnoredReport(b.s.Ignored); report != "" {
		b.req.Log(report)
	}
	// Reported once for the batch, not again by each releasable's release.
	b.s.Ignored = nil
	if b.req.DryRun && len(b.s.Blocking) > 0 {
		b.req.Warn("Under a real run this would refuse: " + release.ConflictMessage(b.s.Blocking, "The batch release", release.RerunBatch))
	}
	var err error
	if b.ws, err = workspace.Load(b.s.Root); err != nil {
		return err
	}
	if b.live, err = git.Open(b.e, b.s.LiveRoot); err != nil {
		return err
	}
	if b.file, err = releaserecord.ReadBatchReleaseFile(b.s.Root); err != nil {
		return err
	}
	data, err := os.ReadFile(filepath.Join(b.s.Root, filepath.FromSlash(releaserecord.BatchReleaseFilePath)))
	if err != nil {
		return err
	}
	repo, err := git.Open(b.e, b.s.Root)
	if err != nil {
		return err
	}
	if b.blob, err = repo.HashObject(string(data)); err != nil {
		return err
	}
	if b.order, err = ReleasableOrder(b.ws, b.file.Names()); err != nil {
		return fmt.Errorf("the batch release file %s: %w", releaserecord.BatchReleaseFilePath, err)
	}
	if b.plan, b.planned, err = runstate.LoadBatchPlan(b.s.LiveRoot); err != nil {
		return err
	}
	if b.planned {
		if err := b.checkPlan(); err != nil {
			return err
		}
	}
	if b.req.DryRun {
		return b.preview()
	}
	released, err := b.releasedItems()
	if err != nil {
		return err
	}
	if b.planned && len(released) == len(b.plan.Items) {
		return b.finishCompletedPlan()
	}
	if b.planned && b.plan.BatchFile != b.blob {
		// The batch runs from this file now (its prose may have been
		// edited since the plan was made); a later run that finds the plan
		// finished tells its own file by this blob.
		b.plan.BatchFile = b.blob
		if err := runstate.SaveBatchPlan(b.e, b.s.LiveRoot, b.plan); err != nil {
			return err
		}
	}
	stranded, err := b.stranded()
	if err != nil {
		return err
	}
	if err := b.validateAndPlan(released, stranded); err != nil {
		return err
	}
	if err := b.rootSelfdoc(); err != nil {
		return err
	}
	pin := b.s.Checkout.Tip
	b.req.Log(fmt.Sprintf("Batch release: %s", strings.Join(b.order, ", ")))
	var pending []string
	for _, name := range b.order {
		if released[name] {
			item, _ := b.plan.Item(name)
			b.req.Log(fmt.Sprintf("--- Skipping %s: released at %s (%s) ---", name, item.TargetVersion, item.Tag))
			continue
		}
		if stranded[name] {
			b.req.Log(fmt.Sprintf("--- Continuing %s: an earlier run committed its release; it joins this run's candidate ---", name))
			pending = append(pending, name)
			continue
		}
		b.req.Log(fmt.Sprintf("--- Releasing %s (%s) ---", name, b.file.Releasables[name].Bump))
		if err := release.ReleaseBatchMember(b.e, b.s, b.memberRequest(name), b.file.Releasables[name]); err != nil {
			return b.firstPassFailed(name, pending, err)
		}
		state, found, err := runstate.LoadInProgress(b.s.LiveRoot, name)
		if err != nil {
			return err
		}
		if !found || !state.Completed(release.StepCommitted) {
			return fmt.Errorf("internal error: the release of %s returned without recording its release commit in %s", name, runstate.InProgressPath(name))
		}
		pending = append(pending, name)
	}
	var completed []string
	if len(pending) > 0 {
		candidate, err := release.PublishBatchCandidate(b.e, b.s, b.req, pending, pin)
		if err != nil {
			return err
		}
		if err := release.GateBatchCandidate(b.e, b.s, b.req, pending, candidate); err != nil {
			return err
		}
		var own []string
		for _, name := range pending {
			b.req.Log(fmt.Sprintf("--- Completing %s ---", name))
			before, err := b.branchTip()
			if err != nil {
				return err
			}
			if err := release.CompleteBatchMember(b.e, b.s, b.req, name, candidate, own); err != nil {
				_, inProgress, serr := runstate.LoadInProgress(b.s.LiveRoot, name)
				if serr != nil {
					return errors.Join(err, serr)
				}
				return b.secondPassFailed(name, completed, pending, inProgress, err)
			}
			after, err := b.branchTip()
			if err != nil {
				return err
			}
			made, err := b.live.Commits([]string{after}, []string{before})
			if err != nil {
				return err
			}
			for _, c := range made {
				if !slices.Contains(own, c) {
					own = append(own, c)
				}
			}
			completed = append(completed, name)
		}
	}
	if len(completed) == 0 {
		return fmt.Errorf("the batch released nothing: every releasable of %s was skipped", releaserecord.BatchReleaseFilePath)
	}
	b.req.Log("Batch release complete: " + strings.Join(completed, ", "))
	return b.archiveIfComplete()
}

// memberRequest is the release request of one releasable of the batch: the
// batch's, started from the directory of the releasable's first member.
func (b *batch) memberRequest(name string) release.RunRequest {
	r := b.req
	members := b.ws.MembersOf(name)
	r.Dir = filepath.Join(b.s.LiveRoot, filepath.FromSlash(members[0].Path))
	r.Releasable = ""
	return r
}

// branchTip is where the release branch is.
func (b *batch) branchTip() (string, error) {
	sha, found, err := b.live.ResolveCommit(b.s.Checkout.BranchRef())
	if err != nil {
		return "", err
	}
	if !found {
		return "", fmt.Errorf("the release branch %s has disappeared", b.s.Checkout.Branch)
	}
	return sha, nil
}

func (b *batch) planPath() string {
	return filepath.Join(b.s.LiveRoot, filepath.FromSlash(runstate.BatchPlanPath))
}

// checkPlan refuses a batch release file that no longer names the
// releasables and bumps of the plan a batch in progress follows: the plan is
// never made again while its batch is in progress.
func (b *batch) checkPlan() error {
	var problems []string
	planned := map[string]bool{}
	for _, it := range b.plan.Items {
		planned[it.Name] = true
		rf, ok := b.file.Releasables[it.Name]
		switch {
		case !ok:
			problems = append(problems, fmt.Sprintf("the plan releases %s, which the batch release file does not name", it.Name))
		case string(rf.Bump) != it.Bump:
			problems = append(problems, fmt.Sprintf("the plan bumps %s %s, and the batch release file %s", it.Name, it.Bump, rf.Bump))
		}
	}
	for _, name := range b.file.Names() {
		if !planned[name] {
			problems = append(problems, fmt.Sprintf("the batch release file names %s, which the plan does not release", name))
		}
	}
	if len(problems) == 0 {
		return nil
	}
	return fmt.Errorf("the batch release file %s does not match the plan of the batch in progress (%s):\n  %s\nA batch follows the plan it started with and never plans again. Put back the releasables and bumps the batch started with and %s; to give the batch up instead, delete its plan (saferm delete --on-error abort --description \"the plan of a batch release given up\" %s) and %s", releaserecord.BatchReleaseFilePath, runstate.BatchPlanPath, strings.Join(problems, "\n  "), release.RerunBatch, b.planPath(), release.RerunBatch)
}

// releasedItems are the plan's releasables whose release happened: the
// version file names the planned version, and the planned tag is here and
// on origin. Nothing is released before a plan exists.
func (b *batch) releasedItems() (map[string]bool, error) {
	out := map[string]bool{}
	if !b.planned {
		return out, nil
	}
	for _, it := range b.plan.Items {
		ok, err := b.itemReleased(it)
		if err != nil {
			return nil, err
		}
		if ok {
			out[it.Name] = true
		}
	}
	return out, nil
}

func (b *batch) itemReleased(it runstate.BatchPlanItem) (bool, error) {
	versionFile := filepath.Join(b.ws.Root, filepath.FromSlash(declarations.VersionFile(it.Name)))
	if _, err := os.Stat(versionFile); errors.Is(err, fs.ErrNotExist) {
		return false, nil
	} else if err != nil {
		return false, err
	}
	current, err := b.ws.ReadReleasableVersion(it.Name)
	if err != nil {
		return false, err
	}
	if current.String() != it.TargetVersion {
		return false, nil
	}
	if _, here, err := b.live.TagCommit(it.Tag); err != nil || !here {
		return false, err
	}
	_, onOrigin, err := b.live.RemoteTagCommit("origin", it.Tag)
	if err != nil {
		return false, fmt.Errorf("whether origin has the tag %s of %s could not be read (%v), so whether %s is released is unknown; make origin reachable and %s", it.Tag, it.Name, err, it.Name, release.RerunBatch)
	}
	return onOrigin, nil
}

// stranded are the plan's releasables whose release an earlier run left in
// progress short of the CI verdict, which this run's candidate carries. One
// past the verdict is refused: its release is bound to the commit CI
// verified, and a new candidate would move it.
func (b *batch) stranded() (map[string]bool, error) {
	out := map[string]bool{}
	if !b.planned {
		return out, nil
	}
	var sealed []string
	for _, it := range b.plan.Items {
		state, found, err := runstate.LoadInProgress(b.s.LiveRoot, it.Name)
		if err != nil {
			return nil, err
		}
		if !found {
			continue
		}
		if state.Completed(release.StepCIVerified) {
			sealed = append(sealed, it.Name)
			continue
		}
		out[it.Name] = true
	}
	if len(sealed) > 0 {
		return nil, b.sealedError(sealed)
	}
	return out, nil
}

func (b *batch) sealedError(sealed []string) error {
	var dirs []string
	for _, name := range sealed {
		dirs = append(dirs, fmt.Sprintf("%s (from %s)", name, filepath.Join(b.s.LiveRoot, filepath.FromSlash(b.ws.MembersOf(name)[0].Path))))
	}
	return fmt.Errorf("the release of %s is in progress past the CI verdict, bound to the commit CI verified; this batch would push a new candidate and move that commit. Finish each where it stands with `rlsbl release resume --watch` run from a member of it: %s (or roll a released one back with `rlsbl release undo`), then %s", strings.Join(sealed, ", "), strings.Join(dirs, ", "), release.RerunBatch)
}

// validate validates the release of one releasable of the batch, writing
// nothing.
func (b *batch) validate(name string) (*release.Validated, error) {
	gh, err := github.New(b.e)
	if err != nil {
		return nil, err
	}
	reg, err := registry.New(registry.Reads(b.e))
	if err != nil {
		return nil, err
	}
	env := map[string]string{}
	if b.s.Checkout != nil {
		env = b.s.Checkout.Environment()
	}
	r := b.memberRequest(name)
	return release.ValidateBatchMember(b.e, release.Request{
		Root: b.s.Root, LiveRoot: b.s.LiveRoot, Dir: r.Dir, Fork: b.req.Fork, Now: b.req.Now(),
		GitHub: gh, Registry: reg, Environment: env, Log: b.req.Log,
	}, b.file.Releasables[name])
}

// validateAndPlan validates, before anything is written, every releasable
// the run releases from the start, and plans a batch with none in progress.
func (b *batch) validateAndPlan(released, stranded map[string]bool) error {
	var items []runstate.BatchPlanItem
	for _, name := range b.order {
		if released[name] || stranded[name] {
			continue
		}
		v, err := b.validate(name)
		if err != nil {
			return fmt.Errorf("the release of %s would be refused, so nothing of the batch was released: %w", name, err)
		}
		items = append(items, runstate.BatchPlanItem{
			Name: name, BaseVersion: v.Current.String(), TargetVersion: v.Decision.Version.String(),
			Tag: v.Decision.Tag, Registry: v.Primary, Bump: string(b.file.Releasables[name].Bump),
		})
	}
	if b.planned {
		return nil
	}
	b.plan = runstate.BatchPlan{BatchFile: b.blob, Items: items}
	if err := runstate.SaveBatchPlan(b.e, b.s.LiveRoot, b.plan); err != nil {
		return err
	}
	b.planned = true
	var lines []string
	for _, it := range items {
		lines = append(lines, fmt.Sprintf("  %s: %s -> %s (%s)", it.Name, it.BaseVersion, it.TargetVersion, it.Tag))
	}
	b.req.Log("Planned the batch release:\n" + strings.Join(lines, "\n"))
	return nil
}

// rootSelfdoc runs `selfdoc gen` and `selfdoc check` at the workspace's
// root when the root member is versioned under no releasable and declares
// selfdoc.json (a root member of a releasable gets them in its release), and
// commits what they wrote with the Autogenerated trailer: what gen wrote, the
// staleness baseline included, since check is selfdoc's read-only verdict.
func (b *batch) rootSelfdoc() error {
	if _, ok := b.ws.ReleasableOf(b.ws.RootMember()); ok {
		return nil
	}
	if _, err := os.Stat(filepath.Join(b.s.Root, "selfdoc.json")); errors.Is(err, fs.ErrNotExist) {
		return nil
	} else if err != nil {
		return err
	}
	if _, err := exec.LookPath("selfdoc"); err != nil {
		return fmt.Errorf("selfdoc.json at the workspace's root declares documentation that selfdoc generates and checks, and selfdoc is not on PATH; a release never ships its documentation unchecked, so install selfdoc and %s", release.RerunBatch)
	}
	timeouts, err := release.ResolveTimeouts(b.ws.Declarations, b.req.Timeouts)
	if err != nil {
		return err
	}
	co := b.s.Checkout
	repo := co.Repo()
	before, err := changedPaths(repo)
	if err != nil {
		return err
	}
	for _, argv := range [][]interface{}{{"selfdoc", "gen", "--no-auto-commit"}, {"selfdoc", "check"}} {
		verb := argv[1]
		b.req.Log(fmt.Sprintf("Running selfdoc %s at the workspace's root", verb))
		if _, err := b.e.Run(argv, strictcli.Cwd(b.s.Root), strictcli.EffectEnv(co.Environment()), strictcli.Stream(true), strictcli.Timeout(timeouts.Check)); err != nil {
			return fmt.Errorf("selfdoc %s at the workspace's root failed, so the batch stops before anything is released: %w. Fix what it reports and %s", verb, err, release.RerunBatch)
		}
	}
	after, err := changedPaths(repo)
	if err != nil {
		return err
	}
	var written []string
	for p := range after {
		if !before[p] {
			written = append(written, p)
		}
	}
	if len(written) == 0 {
		return nil
	}
	sort.Strings(written)
	if _, err := repo.CommitDetached(git.CommitRequest{Message: RootSelfdocCommitMessage, Paths: written, Autogenerated: true}); err != nil {
		return err
	}
	b.req.Log(fmt.Sprintf("Committed what selfdoc wrote at the root (%d file(s))", len(written)))
	return co.Advance("HEAD", "The batch release", release.RerunBatch)
}

// changedPaths are the repository's changed paths, rlsbl's run state left
// out.
func changedPaths(repo git.Repo) (map[string]bool, error) {
	changes, err := repo.Changes()
	if err != nil {
		return nil, err
	}
	out := map[string]bool{}
	for _, c := range changes {
		if !strings.HasPrefix(c.Path, declarations.ReleaseStateDir+"/") {
			out[c.Path] = true
		}
	}
	return out, nil
}

func (b *batch) firstPassFailed(name string, committed []string, err error) error {
	rest := "Nothing else of the batch was committed by this run."
	if len(committed) > 0 {
		rest = fmt.Sprintf("The releasables committed before it (%s) keep their release commits on %s, unpushed, and their release states; the next run continues them.", strings.Join(committed, ", "), b.s.Checkout.Branch)
	}
	return fmt.Errorf("the release of %s stopped, so the batch stopped: %w\n%s Once the cause is dealt with, %s", name, err, rest, release.RerunBatch)
}

// secondPassFailed is the stop of a releasable's second pass. inProgress
// tells a release stopped at a step (its state kept) from one that finished
// every step and failed checking what it published afterwards.
func (b *batch) secondPassFailed(name string, completed, pending []string, inProgress bool, err error) error {
	var left []string
	for _, p := range pending {
		if p != name && !slices.Contains(completed, p) {
			left = append(left, p)
		}
	}
	lines := []string{fmt.Sprintf("the release of %s stopped after CI verified the batch candidate, so the batch stopped: %v", name, err)}
	if !inProgress {
		lines[0] = fmt.Sprintf("%s is released, and checking what it published failed, so the batch stopped: %v", name, err)
		completed = append(append([]string(nil), completed...), name)
	}
	if len(completed) > 0 {
		lines = append(lines, "Released by this run: "+strings.Join(completed, ", ")+".")
	}
	if len(left) > 0 {
		lines = append(lines, fmt.Sprintf("Not finished yet: %s, whose release commits are on origin, untagged.", strings.Join(left, ", ")))
	}
	if inProgress {
		lines = append(lines, fmt.Sprintf("Finish %s with `rlsbl release resume --watch` from one of its members, then %s, which finishes the rest.", name, release.RerunBatch))
	} else {
		lines = append(lines, fmt.Sprintf("Once the cause is dealt with, %s, which finishes the rest.", release.RerunBatch))
	}
	return errors.New(strings.Join(lines, "\n"))
}

// finishCompletedPlan is a run that finds every releasable of the plan
// released. With no release left in progress, the batch finished and only
// its archive is missing: when the batch release file is the one the plan
// was made from, its archive is finished; otherwise the plan is left over
// and removed, and the batch release file, the next batch's, is left alone.
func (b *batch) finishCompletedPlan() error {
	var inProgress []string
	for _, it := range b.plan.Items {
		_, found, err := runstate.LoadInProgress(b.s.LiveRoot, it.Name)
		if err != nil {
			return err
		}
		if found {
			inProgress = append(inProgress, it.Name)
		}
	}
	if len(inProgress) > 0 {
		return fmt.Errorf("every releasable of the batch plan %s is released, and the release state of %s remains: finish each with `rlsbl release resume --watch` from one of its members (or roll it back with `rlsbl release undo`), then %s", runstate.BatchPlanPath, strings.Join(inProgress, ", "), release.RerunBatch)
	}
	if b.plan.BatchFile != "" && b.plan.BatchFile == b.blob {
		b.req.Log("Every releasable of the batch is released; finishing the archive of " + releaserecord.BatchReleaseFilePath)
		return b.archive()
	}
	if err := runstate.ClearBatchPlan(b.e, b.s.LiveRoot); err != nil {
		return err
	}
	abs := filepath.Join(b.s.LiveRoot, filepath.FromSlash(releaserecord.BatchReleaseFilePath))
	return fmt.Errorf("every releasable the batch plan %s names is released, so this run had nothing to release; the plan was left over from a finished batch and is removed. %s is not the file that batch was planned from, so it was left alone: if it describes the next batch, %s and it is planned afresh; if it is left over from the finished batch, delete it and commit the deletion (saferm delete --on-error abort --description \"a finished batch release file\" %s, then safegit commit -m \"Remove a finished batch release file\" -- %s)", runstate.BatchPlanPath, releaserecord.BatchReleaseFilePath, release.RerunBatch, abs, releaserecord.BatchReleaseFilePath)
}

// archiveIfComplete archives the batch release file once every releasable
// of the plan is released and none has a release in progress.
func (b *batch) archiveIfComplete() error {
	released, err := b.releasedItems()
	if err != nil {
		return err
	}
	if len(released) != len(b.plan.Items) {
		var open []string
		for _, it := range b.plan.Items {
			if !released[it.Name] {
				open = append(open, it.Name)
			}
		}
		return fmt.Errorf("the batch release file is not archived: %s of its plan %s not released yet; %s", strings.Join(open, ", "), plural(len(open), "is", "are"), release.RerunBatch)
	}
	for _, it := range b.plan.Items {
		if _, found, err := runstate.LoadInProgress(b.s.LiveRoot, it.Name); err != nil {
			return err
		} else if found {
			return fmt.Errorf("the batch release file is not archived: the release state of %s remains; finish it with `rlsbl release resume --watch` from one of its members, then %s", it.Name, release.RerunBatch)
		}
	}
	return b.archive()
}

// archive moves the batch release file to its archive, read-only, commits
// both paths with the Autogenerated trailer, advances the release branch,
// removes the plan, and pushes the branch.
func (b *batch) archive() error {
	co := b.s.Checkout
	src := releaserecord.BatchReleaseFilePath
	srcAbs := filepath.Join(b.s.Root, filepath.FromSlash(src))
	data, err := os.ReadFile(srcAbs)
	if err != nil {
		return err
	}
	dst := releaserecord.BatchArchivePath(b.req.Now())
	dstAbs := filepath.Join(b.s.Root, filepath.FromSlash(dst))
	if _, err := os.Stat(dstAbs); err == nil {
		return fmt.Errorf("the archive %s exists already, so the batch release file cannot be archived under that name; wait a second and %s", dst, release.RerunBatch)
	}
	if _, err := b.e.Write(dstAbs, data, strictcli.Mode(0o444)); err != nil {
		return err
	}
	if _, err := b.e.Remove(srcAbs); err != nil {
		return err
	}
	message := ArchiveCommitPrefix + path.Base(dst)
	if _, err := co.Repo().CommitDetached(git.CommitRequest{Message: message, Paths: []string{dst, src}, Autogenerated: true, RequireChange: true}); err != nil {
		return fmt.Errorf("every releasable of the batch is released, and the commit archiving %s failed: %w. The plan is kept: once the cause is dealt with, %s; it finds the finished plan and archives the file", src, err, release.RerunBatch)
	}
	if err := co.Advance("HEAD", "The batch release", release.RerunBatch); err != nil {
		return err
	}
	b.req.Log(fmt.Sprintf("Archived the batch release file as %s", dst))
	if err := runstate.ClearBatchPlan(b.e, b.s.LiveRoot); err != nil {
		return err
	}
	timeouts, err := release.ResolveTimeouts(b.ws.Declarations, b.req.Timeouts)
	if err != nil {
		return err
	}
	ref := co.BranchRef()
	if err := b.live.PushFastForward("origin", ref, co.Tip, timeouts.Push); err != nil {
		return fmt.Errorf("every releasable of the batch is released, tagged, and on origin, and the archive of the batch release file is committed as %s, which was not pushed: %w\nThis is no failed release, and nothing is left to resume: %s is one commit ahead of origin, and the candidate push of the next release carries that commit", short(co.Tip), err, co.Branch)
	}
	b.req.Log(fmt.Sprintf("Pushed the archive commit %s to origin/%s", short(co.Tip), co.Branch))
	return nil
}

func short(sha string) string {
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}

// preview reports what the batch would do, writing nothing: each
// releasable's validation and the version it would ship, the ones it would
// skip or continue, and the steps after them.
func (b *batch) preview() error {
	b.req.Log(fmt.Sprintf("Would release the batch in this order: %s", strings.Join(b.order, ", ")))
	if !b.planned {
		if _, ok := b.ws.ReleasableOf(b.ws.RootMember()); !ok {
			if _, err := os.Stat(filepath.Join(b.s.Root, "selfdoc.json")); err == nil {
				b.req.Log("Would run selfdoc gen and selfdoc check at the workspace's root and commit what they write")
			}
		}
	}
	for i, name := range b.order {
		if b.planned {
			item, _ := b.plan.Item(name)
			released, err := b.itemReleased(item)
			if err != nil {
				return err
			}
			if released {
				b.req.Log(fmt.Sprintf("  %d. %s: would be skipped, released at %s (%s)", i+1, name, item.TargetVersion, item.Tag))
				continue
			}
			state, found, err := runstate.LoadInProgress(b.s.LiveRoot, name)
			if err != nil {
				return err
			}
			if found {
				if state.Completed(release.StepCIVerified) {
					b.req.Warn("Under a real run this would refuse: " + b.sealedError([]string{name}).Error())
				} else {
					b.req.Log(fmt.Sprintf("  %d. %s: its release of %s is in progress; it would join the candidate", i+1, name, state.Version))
				}
				continue
			}
		}
		v, err := b.validate(name)
		if err != nil {
			return fmt.Errorf("the release of %s would be refused: %w", name, err)
		}
		rf := b.file.Releasables[name]
		b.req.Log(fmt.Sprintf("  %d. %s: %s -> %s (%s, %s): %s", i+1, name, v.Current, v.Decision.Version, v.Decision.Tag, rf.Bump, rf.Description))
	}
	b.req.Log("Would then release each up to its release commit in this order, push the branch tip as one candidate, wait for CI's verdict on it once, then finalize, tag, and publish each on the commit CI verified, and archive the batch release file")
	return nil
}
