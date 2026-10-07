package release

import (
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/checks"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// RunRequest is what `rlsbl release run` and `rlsbl release resume` are
// asked.
type RunRequest struct {
	// Dir is the working directory the command was started from, absolute.
	Dir string
	// LiveRoot is the root of the working tree holding Dir.
	LiveRoot string
	// Releasable is --releasable, empty when it was not given; resume takes
	// none.
	Releasable string
	DryRun     bool
	// Watch is --watch: after the release, watch CI on the release commit
	// and verify that the registries list the version.
	Watch    bool
	Timeouts TimeoutOverrides
	Fork     Fork
	// RlsblVersion is the running rlsbl's version, which the scaffold state
	// records.
	RlsblVersion string
	// Checks runs the preflight checks: the checks.Runner of the app, which
	// runs them for the member directory each check context names.
	Checks CheckRunner
	// Home is the user's home directory and IndexPath the machine-local
	// confidential-name index, both read by the checks; IndexPath is also
	// what the published texts are scanned against.
	Home      string
	IndexPath string
	// Scratch is the command's scratch directory, where npm writes its
	// cache when the release lists an npm upload.
	Scratch targets.Scratch
	Now     func() time.Time
	Sleep   func(time.Duration)
	// Log reports progress; Warn reports what --quiet must not swallow.
	Log  func(string)
	Warn func(string)
}

func (r RunRequest) check() error {
	switch {
	case !filepath.IsAbs(r.Dir) || !filepath.IsAbs(r.LiveRoot):
		return fmt.Errorf("a release needs the absolute working directory and repository root, not %q and %q", r.Dir, r.LiveRoot)
	case r.Checks == nil:
		return errors.New("a release needs the check runner its preflight runs through")
	case r.Now == nil || r.Sleep == nil || r.Log == nil || r.Warn == nil:
		return errors.New("a release needs a clock, a sleep, a log, and a warning stream")
	case r.IndexPath == "":
		return errors.New("a release needs the path of the confidential-name index")
	}
	return nil
}

// Run is `rlsbl release run`: it selects the releasable the working
// directory (or --releasable) names, takes the repository for the release's
// write scope (Enter), and releases it (Release).
func Run(e *strictcli.Effects, req RunRequest) (err error) {
	if err := req.check(); err != nil {
		return err
	}
	live, err := git.Open(e, req.LiveRoot)
	if err != nil {
		return err
	}
	ws, err := workspace.Load(req.LiveRoot)
	if err != nil {
		return err
	}
	r, _, err := selectRelease(ws, Request{LiveRoot: req.LiveRoot, Dir: req.Dir, Releasable: req.Releasable})
	if err != nil {
		return err
	}
	scope, err := WriteScope(ws, r.Name)
	if err != nil {
		return err
	}
	session, err := Enter(e, live, scope, EnterOptions{DryRun: req.DryRun, What: "The release", Rerun: RerunFresh, OnWait: waitNotice(req.Log)})
	if err != nil {
		return err
	}
	defer func() { err = errors.Join(err, session.Close()) }()
	return Release(e, session, req)
}

func waitNotice(log func(string)) func(string) {
	return func(path string) {
		log(fmt.Sprintf("Another rlsbl process holds the release lock (%s); waiting for it", path))
	}
}

// Release releases what req selects in a session the caller entered: it
// validates the release, runs the changelog preflight, the pre-release
// pipeline, the preflight, and the pre-release hooks, regenerates the
// changelog, records the release's in-progress state, and walks the steps.
// Under --dry-run it previews: the programs it would start and the files it
// would write are recorded, and it stops after the version-bumped step,
// listing the steps it would take after it.
func Release(e *strictcli.Effects, s *Session, req RunRequest) error {
	return release(e, s, req, batchRole{})
}

// release is Release in the role batch gives it: none for `rlsbl release
// run`, or a releasable's first pass of a batch release.
func release(e *strictcli.Effects, s *Session, req RunRequest, batch batchRole) error {
	if err := req.check(); err != nil {
		return err
	}
	if report := IgnoredReport(s.Ignored); report != "" {
		req.Log(report)
	}
	if req.DryRun && len(s.Blocking) > 0 {
		req.Warn("Under a real run this would refuse: " + ConflictMessage(s.Blocking, "The release", RerunFresh))
	}
	env := map[string]string{}
	if s.Checkout != nil {
		env = s.Checkout.Environment()
	}
	gh, err := github.New(e)
	if err != nil {
		return err
	}
	reg, err := registry.New(registry.Reads(e))
	if err != nil {
		return err
	}
	now := req.Now()
	v, err := validate(e, Request{Root: s.Root, LiveRoot: s.LiveRoot, Dir: req.Dir, Releasable: req.Releasable, Fork: req.Fork, Now: now, GitHub: gh, Registry: reg, Environment: env, Log: req.Log}, batch.file)
	if err != nil {
		return err
	}
	name := v.Releasable.Name
	if done := v.CompletedState; done != nil {
		verb := "clearing it"
		if req.DryRun {
			verb = "it would be cleared"
		}
		req.Log(fmt.Sprintf("The release of %s %s completed every step; its in-progress state was left behind, and %s", name, done.Version, verb))
		for _, step := range sortedKeys(done.FailedSteps) {
			req.Warn(fmt.Sprintf("  the step %s of that release failed: %s", step, done.FailedSteps[step]))
		}
		if !req.DryRun {
			if err := runstate.ClearInProgress(e, s.LiveRoot, name); err != nil {
				return err
			}
		}
	}
	x, err := newExecution(e, s, req, executionSetup{
		ws: v.Workspace, repo: v.Repo, releasable: v.Releasable, representative: v.Representative,
		version: v.Decision.Version, current: v.Current, primary: v.Primary, syncs: v.Syncs,
		environment: v.Environment, gh: gh, ghRepo: v.GitHub, reg: reg, record: v.Lifecycle,
		publishesFromCI: v.PublishesFromCI, branch: v.Branch, now: now, batch: batch,
	})
	if err != nil {
		return err
	}
	rf := v.ReleaseFile
	pin, err := v.Repo.Head()
	if err != nil {
		return err
	}
	if err := x.changelogPreflight(); err != nil {
		return err
	}
	// A preview cannot ask anything once it recorded a write, and the
	// pipeline and the hooks write: its preflight runs first. The checks
	// it runs read the same files either way, since the recorded pipeline
	// changed nothing.
	if req.DryRun {
		if err := x.preflight(); err != nil {
			return err
		}
	}
	hooks := x.hookContext(string(v.Decision.Bump), rf.Description)
	pipe, err := RunPipeline(e, PipelineInputs{
		Workspace: x.ws, Repo: x.repo, Releasable: name, Representative: x.representative.Name,
		Version: x.version, Hooks: hooks, Environment: x.env, HookTimeout: x.timeouts.Hook,
		ProgramTimeout: x.timeouts.Check, DryRun: req.DryRun, Log: req.Log,
	})
	if err != nil {
		return err
	}
	if !req.DryRun {
		if err := x.preflight(); err != nil {
			return err
		}
	}
	runs, err := HookRuns(x.ws, name, x.representative.Name, PreRelease, hooks)
	if err != nil {
		return err
	}
	if len(runs) > 0 {
		req.Log(fmt.Sprintf("Running %d pre-release hook(s)", len(runs)))
	}
	if err := RunHooks(e, runs, x.env, x.timeouts.Hook); err != nil {
		return err
	}
	if !req.DryRun {
		changed, err := changedPaths(x.repo)
		if err != nil {
			return err
		}
		x.generated = sortedKeys(changed)
	}
	pending := &changelog.Pending{Version: x.version, Description: rf.Description, Context: rf.Context, Bump: v.Decision.Bump}
	if err := x.publishedSectionClean(pending); err != nil {
		return err
	}
	if _, err := changelog.Regenerate(e, x.ws.Root, x.ws.Declarations, name, pending); err != nil {
		return err
	}
	var created []string
	if pipe.SelfdocCommit != "" {
		created = append(created, pipe.SelfdocCommit)
	}
	x.state = runstate.InProgress{
		Releasable:           name,
		RepresentativeMember: x.representative.Name,
		Version:              x.version.String(),
		Tag:                  v.Decision.Tag,
		Branch:               v.Branch,
		Registry:             v.Primary,
		Bump:                 string(v.Decision.Bump),
		CommitMessage:        v.CommitMessage,
		Description:          rf.Description,
		Context:              rf.Context,
		Include:              nonNil(rf.Include),
		Exclude:              nonNil(rf.Exclude),
		PreReleaseCommit:     pin,
		PinCommit:            pin,
		CreatedCommits:       created,
	}
	if err := x.save(); err != nil {
		return err
	}
	if req.DryRun {
		return x.preview()
	}
	return x.walk()
}

// Resume is `rlsbl release resume`: it continues the release of the
// releasable whose release is in progress, from the step it stopped at,
// pinned again at the branch tip, adopting what was committed since it
// stopped once the changelog describes it.
func Resume(e *strictcli.Effects, req RunRequest) (err error) {
	if err := req.check(); err != nil {
		return err
	}
	if req.Releasable != "" {
		return errors.New("release resume takes no --releasable: it continues the release in progress of the releasable the working directory selects")
	}
	live, err := git.Open(e, req.LiveRoot)
	if err != nil {
		return err
	}
	ws, err := workspace.Load(req.LiveRoot)
	if err != nil {
		return err
	}
	name, err := resumedReleasable(ws, req.LiveRoot, req.Dir)
	if err != nil {
		return err
	}
	scope, err := WriteScope(ws, name)
	if err != nil {
		return err
	}
	session, err := Enter(e, live, scope, EnterOptions{DryRun: req.DryRun, What: "The resume", Rerun: RerunResume, OnWait: waitNotice(req.Log)})
	if err != nil {
		return err
	}
	defer func() { err = errors.Join(err, session.Close()) }()
	return ResumeIn(e, session, req, name)
}

// resumedReleasable is the releasable a resume continues: the releasable of
// the member the working directory lies in, or, from a member versioned
// under none, the one releasable with a release in progress.
func resumedReleasable(ws *workspace.Workspace, liveRoot, dir string) (string, error) {
	member, err := ws.MemberAtDirectory(dir)
	if err != nil {
		return "", err
	}
	if r, ok := ws.ReleasableOf(member); ok {
		return r.Name, nil
	}
	names, err := runstate.InProgressReleasables(liveRoot)
	if err != nil {
		return "", err
	}
	switch len(names) {
	case 0:
		return "", &ValidationError{Message: "no release is in progress in this repository, so there is nothing to resume; `" + runstate.RunInvocation + "` starts a release"}
	case 1:
		return names[0], nil
	}
	return "", &ValidationError{Message: fmt.Sprintf("the working directory lies in the member %q, which is versioned under no releasable, and releases of %s are in progress; run `"+runstate.ResumeInvocation+"` from a member of the releasable whose release it continues", member.Name, strings.Join(names, ", "))}
}

// ResumeIn resumes the release of releasable in a session the caller
// entered. Under --dry-run it reports what the resume would adopt and which
// steps it would take, and writes nothing.
func ResumeIn(e *strictcli.Effects, s *Session, req RunRequest, releasable string) error {
	return resumeIn(e, s, req, releasable, batchRole{})
}

// resumeIn is ResumeIn in the role batch gives it: none for `rlsbl release
// resume`, or a releasable's second pass of a batch release.
func resumeIn(e *strictcli.Effects, s *Session, req RunRequest, releasable string, batch batchRole) error {
	state, found, err := runstate.LoadInProgress(s.LiveRoot, releasable)
	if err != nil {
		return err
	}
	statePath := filepath.Join(s.LiveRoot, filepath.FromSlash(runstate.InProgressPath(releasable)))
	if !found {
		return &ValidationError{Message: fmt.Sprintf("no release of %s is in progress (%s does not exist), so there is nothing to resume; `"+runstate.RunInvocation+"` starts a release", releasable, statePath)}
	}
	if unknown := state.UnknownSteps(StepNames()); len(unknown) > 0 {
		return &ValidationError{Message: fmt.Sprintf("%s records steps this rlsbl does not have (%s): another version of rlsbl wrote it. Resume it with that version, or give the attempt up with `rlsbl release abandon --approve-consequential`", statePath, strings.Join(unknown, ", "))}
	}
	live, err := git.Open(e, s.LiveRoot)
	if err != nil {
		return err
	}
	branch, err := LiveBranch(live)
	if err != nil {
		return err
	}
	if branch != state.Branch {
		return &ValidationError{Message: fmt.Sprintf("the release of %s %s runs on %s, and the working tree is on %s; check out %s and run `"+runstate.ResumeInvocation+"` again", releasable, state.Version, state.Branch, branch, state.Branch)}
	}
	if err := live.RefuseStash("release resume", "The resume commits, tags, and pushes this working tree, and a stash rides along in none of it."); err != nil {
		return &ValidationError{Message: err.Error()}
	}
	version, err := semver.Parse(state.Version)
	if err != nil {
		return fmt.Errorf("%s names the version %q: %w", statePath, state.Version, err)
	}
	ws, err := workspace.Load(s.Root)
	if err != nil {
		return err
	}
	r, ok := ws.Declarations.Releasable(releasable)
	if !ok {
		return &ValidationError{Message: fmt.Sprintf("%s is the state of a release of %q, which %s no longer declares; declare it again to resume, or give the attempt up with `rlsbl release abandon --approve-consequential`", statePath, releasable, declarations.ReleasablesFile)}
	}
	rep, ok := ws.Declarations.Member(state.RepresentativeMember)
	if !ok || rep.Releasable != releasable {
		return &ValidationError{Message: fmt.Sprintf("%s resolves %s through the member %q, which is no longer a member of it; declare it again to resume", statePath, releasable, state.RepresentativeMember)}
	}
	repo, err := git.Open(e, s.Root)
	if err != nil {
		return err
	}
	tip, err := repo.Head()
	if err != nil {
		return err
	}
	adopted, err := foreignCommits(live, "refs/heads/"+branch, state.PinCommit, state.CreatedCommits)
	if err != nil {
		return err
	}
	// Once the changelog is finalized, the released version's changelog and
	// the candidate CI verified are settled: commits made since belong to
	// the next release, which their changelog entries wait for, and the
	// resume finishes this one without them.
	var deferred []string
	if state.Completed(StepChangelogFinalized) {
		deferred, adopted = adopted, nil
	}
	if err := RequireAdoptedCovered(repo, ws, releasable, adopted, version); err != nil {
		return err
	}
	gh, err := github.New(e)
	if err != nil {
		return err
	}
	if err := gh.CheckInstalled(); err != nil {
		return &ValidationError{Message: err.Error()}
	}
	if err := gh.CheckAuth(); err != nil {
		return &ValidationError{Message: err.Error()}
	}
	ghRepo, err := releaseRepository(live, ws.Declarations)
	if err != nil {
		return err
	}
	record, err := lifecycle.Load(s.Root)
	if err != nil {
		return err
	}
	// What was allowed when the release started may not be now: the
	// releasable put on hold or retired, classified, or given a deploy
	// command since. The steps left publish and deploy, so they are held to
	// the rules release validation applies.
	info, err := gh.Info(ghRepo)
	if err != nil {
		return &ValidationError{Message: fmt.Sprintf("GitHub could not be asked about %s (%v), and a resume must know whether the repository is public before it publishes; check `gh auth status` and run `"+runstate.ResumeInvocation+"` again", ghRepo, err)}
	}
	if err := RefuseRepublishing(ws, r, record, repo, "HEAD", info, req.Now()); err != nil {
		return err
	}
	if req.DryRun {
		return previewResume(req, state, version, adopted, deferred, live)
	}
	if len(deferred) > 0 {
		req.Log(fmt.Sprintf("The changelog of %s is finalized, so the commits made since it stopped are left for the next release:", state.Version))
		for _, line := range subjectLines(live, deferred) {
			req.Log(line)
		}
	}
	base := map[string]string{}
	if s.Checkout != nil {
		base = s.Checkout.Environment()
	}
	env, err := releaseEnvironmentFile(ws.Declarations, s.LiveRoot, base)
	if err != nil {
		return err
	}
	reg, err := registry.New(registry.Reads(e))
	if err != nil {
		return err
	}
	if exists, err := fileExists(filepath.Join(s.Root, filepath.FromSlash(releaserecord.ReleaseFilePath(releasable)))); err != nil {
		return err
	} else if exists {
		// The release file waits on disk, editable, for as long as the
		// release is stopped; the archive step reads it again.
		if _, err := releaserecord.ReadReleaseFile(s.Root, releasable); err != nil {
			return err
		}
	}
	var syncs []LockfileSync
	if !state.Completed(StepCommitted) {
		if syncs, err = OwedLockfileSyncs(repo, ws, releasable); err != nil {
			return err
		}
	}
	if err := RefuseUntidyGoModules(e, s.Root, s.LiveRoot, syncs, env); err != nil {
		return err
	}
	primary := state.Registry
	current, err := currentVersion(ws, releasable, primary)
	if err != nil {
		return err
	}
	x, err := newExecution(e, s, req, executionSetup{
		ws: ws, repo: repo, releasable: r, representative: rep, version: version, current: current,
		primary: primary, syncs: syncs, environment: env, gh: gh, ghRepo: ghRepo, reg: reg, record: record,
		publishesFromCI: publishesFromCI(ws, r), branch: branch, now: req.Now(), resuming: true, batch: batch,
	})
	if err != nil {
		return err
	}
	x.entry = cloneState(state)
	x.state = state
	if len(adopted) > 0 {
		req.Log(fmt.Sprintf("Adopting %d commit(s) made since the release stopped", len(adopted)))
		if state.Completed(StepCIVerified) {
			// The recorded verdict is about a commit that no longer holds
			// everything this release ships: the tip is pushed as the
			// candidate and judged again.
			x.state.CompletedSteps = without(x.state.CompletedSteps, StepCIVerified)
			x.state.ReleaseCommit = ""
			req.Log("The recorded CI verdict does not cover the adopted commits, so the tip is pushed as the candidate and CI judges it again")
		}
	}
	x.state.PinCommit = tip
	if !x.state.Completed(StepChangelogFinalized) {
		// The fix-forward's entry belongs in this version's changelog.
		bump, err := semver.ParseBump(state.Bump)
		if state.Bump == "" {
			bump, err = "", nil
		}
		if err != nil {
			return fmt.Errorf("%s names the bump %q: %w", statePath, state.Bump, err)
		}
		pending := &changelog.Pending{Version: version, Description: state.Description, Context: state.Context, Bump: bump}
		if err := x.publishedSectionClean(pending); err != nil {
			return err
		}
		if _, err := changelog.Regenerate(e, ws.Root, ws.Declarations, releasable, pending); err != nil {
			return err
		}
	}
	if err := x.save(); err != nil {
		return err
	}
	return x.walk()
}

// previewResume reports what a resume would adopt and the steps it would
// take, writing nothing.
func previewResume(req RunRequest, state runstate.InProgress, v semver.Version, adopted, deferred []string, live git.Repo) error {
	req.Log(fmt.Sprintf("Would resume the release of %s %s (%s) on %s", state.Releasable, v, state.Tag, state.Branch))
	if len(deferred) > 0 {
		req.Log(fmt.Sprintf("Would leave the commits made since it stopped for the next release, since the changelog of %s is finalized:", v))
		for _, line := range subjectLines(live, deferred) {
			req.Log(line)
		}
	}
	if len(adopted) > 0 {
		req.Log("Would adopt the commits made since it stopped:")
		for _, line := range subjectLines(live, adopted) {
			req.Log(line)
		}
		if state.Completed(StepCIVerified) {
			req.Log("The recorded CI verdict would not cover them: the tip would be pushed as the candidate and judged by CI again")
		}
	}
	var remaining []string
	for _, step := range StepNames() {
		if !state.Completed(step) {
			remaining = append(remaining, step)
		}
	}
	if len(remaining) == 0 {
		req.Log("Would run: nothing (every step is done); the state would be cleared")
		return nil
	}
	req.Log("Would run: " + strings.Join(remaining, ", "))
	return nil
}

// executionSetup is what a release's walk is built from.
type executionSetup struct {
	ws              *workspace.Workspace
	repo            git.Repo
	releasable      declarations.Releasable
	representative  declarations.Member
	version         semver.Version
	current         semver.Version
	primary         string
	syncs           []LockfileSync
	environment     map[string]string
	gh              github.Client
	ghRepo          github.Repository
	reg             registry.Client
	record          *lifecycle.Record
	publishesFromCI bool
	branch          string
	now             time.Time
	resuming        bool
	batch           batchRole
}

func newExecution(e *strictcli.Effects, s *Session, req RunRequest, in executionSetup) (*execution, error) {
	timeouts, err := ResolveTimeouts(in.ws.Declarations, req.Timeouts)
	if err != nil {
		return nil, err
	}
	scheme, err := workspace.SchemeOf(in.releasable)
	if err != nil {
		return nil, err
	}
	live, err := git.Open(e, s.LiveRoot)
	if err != nil {
		return nil, err
	}
	idx, err := index.Load(req.IndexPath)
	if err != nil {
		return nil, err
	}
	scanner, err := publishrules.NewScanner(in.record, in.now, idx)
	if err != nil {
		return nil, err
	}
	optionsRegistry, err := options.Shipped()
	if err != nil {
		return nil, err
	}
	opts, err := options.Load(optionsRegistry, in.ws.Root, in.ws.Declarations)
	if err != nil {
		return nil, err
	}
	taggingOff, err := opts.IsOff(options.EcosystemTagging, in.representative.Path)
	if err != nil {
		return nil, err
	}
	return &execution{
		e: e, session: s, co: s.Checkout, live: live, repo: in.repo, ws: in.ws,
		releasable: in.releasable, representative: in.representative, scheme: scheme,
		version: in.version, current: in.current, primary: in.primary, syncs: in.syncs,
		env: in.environment, gh: in.gh, ghRepo: in.ghRepo, reg: in.reg, record: in.record,
		index: idx, scanner: scanner, timeouts: timeouts, ecosystemTagging: !taggingOff,
		publishesFromCI: in.publishesFromCI, branch: in.branch, now: in.now, resuming: in.resuming, batch: in.batch, req: req,
	}, nil
}

// changelogPreflight runs the checks on the changelog the release runs
// first, in the representative member's directory.
func (x *execution) changelogPreflight() error {
	sel, err := ChangelogPreflightSelectionFor()
	if err != nil {
		return err
	}
	return x.runPreflight(x.representative, sel, "Changelog preflight")
}

// preflight runs every member's preflight selection, a check answering for
// the whole repository once.
func (x *execution) preflight() error {
	members := sortedMembers(x.ws.MembersOf(x.releasable.Name))
	sels, err := PreflightSelectionsFor(x.releasable, members)
	if err != nil {
		return err
	}
	for i, m := range members {
		sel := sels[i]
		if sel.TestsReplacedBy != "" {
			x.req.Log(fmt.Sprintf("The pre-release hook %s declares runs instead of the built-in tests of %s; every other preflight check runs", sel.TestsReplacedBy, m.Name))
		}
		if err := x.runPreflight(m, sel, "Preflight ("+m.Name+")"); err != nil {
			return err
		}
	}
	return nil
}

func (x *execution) runPreflight(m declarations.Member, sel PreflightSelection, label string) error {
	ctx, err := checks.NewContext(x.e, checks.Inputs{
		Dir: x.ws.MemberDir(m), Releasable: x.releasable.Name, Now: x.now, CheckTimeout: x.timeouts.Check,
		Home: x.req.Home, IndexPath: x.req.IndexPath, Scratch: x.req.Scratch,
	})
	if err != nil {
		return err
	}
	report, err := RunPreflight(x.req.Checks, ctx, sel, x.req.DryRun)
	for _, name := range report.Impure {
		x.req.Log(fmt.Sprintf("%s: would run %s (it starts programs, so a preview does not)", label, name))
	}
	if err != nil {
		return err
	}
	var passed []string
	for _, r := range report.Results {
		if r.Status() == "pass" {
			passed = append(passed, r.Name)
		}
	}
	if len(passed) > 0 {
		x.req.Log(fmt.Sprintf("%s: %d check(s) passed (%s)", label, len(passed), strings.Join(passed, ", ")))
	}
	return nil
}

// hookContext is what the release tells its hooks: the version, the bump
// (empty for a first release), the version it bumps from (empty when the
// version files already name the released version), and the description.
func (x *execution) hookContext(bump, description string) HookContext {
	previous := ""
	if semver.Compare(x.current, x.version) != 0 {
		previous = x.current.String()
	}
	return HookContext{Version: x.version.String(), Bump: bump, PreviousVersion: previous, Description: description}
}

// publishedSectionClean scans the changelog section the release publishes
// (in CHANGELOG.md and in the GitHub Release) for confidential names, in a
// public repository, before anything is written.
func (x *execution) publishedSectionClean(pending *changelog.Pending) error {
	sections, err := changelog.Sections(x.ws.Root, x.releasable.Name, 2, pending)
	if err != nil {
		return err
	}
	if len(sections) == 0 {
		return nil
	}
	return x.scanner.ScanTexts([]publishrules.Text{{Name: fmt.Sprintf("the changelog section of %s %s", x.releasable.Name, x.version), Content: sections[0]}})
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func nonNil(list []string) []string {
	if list == nil {
		return []string{}
	}
	return list
}

func without(list []string, item string) []string {
	var out []string
	for _, l := range list {
		if l != item {
			out = append(out, l)
		}
	}
	return out
}

func cloneState(s runstate.InProgress) runstate.InProgress {
	c := s
	c.CompanionTags = append([]string(nil), s.CompanionTags...)
	c.Include = append([]string(nil), s.Include...)
	c.Exclude = append([]string(nil), s.Exclude...)
	c.CreatedCommits = append([]string(nil), s.CreatedCommits...)
	c.CompletedSteps = append([]string(nil), s.CompletedSteps...)
	c.PublishedTargets = append([]string(nil), s.PublishedTargets...)
	c.PublishedMembers = append([]string(nil), s.PublishedMembers...)
	c.FailedSteps = map[string]string{}
	for k, v := range s.FailedSteps {
		c.FailedSteps[k] = v
	}
	return c
}
