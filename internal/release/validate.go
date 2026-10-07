package release

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/ci"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/pipelines"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// ValidationError is a release refused before it wrote anything.
type ValidationError struct{ Message string }

func (e *ValidationError) Error() string { return e.Message }

func refuse(format string, args ...any) error {
	return &ValidationError{Message: fmt.Sprintf(format, args...)}
}

// Request is what a release is asked to validate.
type Request struct {
	// Root is where the release reads committed files: the release
	// checkout, or the working tree under --dry-run.
	Root string
	// LiveRoot is the working tree, where run state, the environment file,
	// and the dev overlays live.
	LiveRoot string
	// Dir is the working directory the release was started from, absolute
	// and inside the working tree.
	Dir string
	// Releasable is --releasable, empty when it was not given.
	Releasable string
	Fork       Fork
	Now        time.Time
	GitHub     github.Client
	Registry   registry.Client
	// Environment is what every program the release runs gets (the
	// checkout's, Checkout.Environment); the environment file's variables
	// are added to it.
	Environment map[string]string
	// Log tells the operator what the validation decided.
	Log func(string)
}

// Validated is a release that passed validation: everything the rest of the
// release reads, decided before anything is written.
type Validated struct {
	Workspace      *workspace.Workspace
	Repo           git.Repo
	Releasable     declarations.Releasable
	Representative declarations.Member
	ReleaseFile    releaserecord.ReleaseFile
	// Primary is the target the release file includes first.
	Primary string
	Branch  string
	Current semver.Version
	// Decision is the version, bump, and tag the release ships.
	Decision      releaserecord.Decision
	Record        *releaserecord.Record
	Latest        releaserecord.LatestFact
	Lifecycle     *lifecycle.Record
	GitHub        github.Repository
	Syncs         []LockfileSync
	CommitMessage string
	// Environment is the request's environment with the environment file's
	// variables added.
	Environment map[string]string
	// CompletedState is set when an in-progress state of an earlier release
	// is complete (every step done, no fatal failure): its run finished and
	// stopped before clearing it, and the release clears it.
	CompletedState *runstate.InProgress
	// PublishesFromCI is whether the release's Release starts CI publish
	// workflows.
	PublishesFromCI bool
}

// Validate refuses, before anything is written, a release that cannot or
// must not run: every guard the release keeps runs here, in an order that
// reads local state before asking the network. The run state, the
// environment file, and the dev overlays are read in the working tree;
// everything committed is read under Root.
func Validate(e *strictcli.Effects, req Request) (*Validated, error) {
	log := req.Log
	if log == nil {
		log = func(string) {}
	}
	ws, err := workspace.Load(req.Root)
	if err != nil {
		return nil, err
	}
	repo, err := git.Open(e, req.Root)
	if err != nil {
		return nil, err
	}
	live, err := git.Open(e, req.LiveRoot)
	if err != nil {
		return nil, err
	}
	v := &Validated{Workspace: ws, Repo: repo}
	if v.Releasable, v.Representative, err = selectRelease(ws, req); err != nil {
		return nil, err
	}
	name := v.Releasable.Name
	if v.CompletedState, err = refuseInProgress(repo, req.LiveRoot, v.Releasable); err != nil {
		return nil, err
	}
	if v.ReleaseFile, err = releaserecord.ReadReleaseFile(req.Root, name); err != nil {
		return nil, err
	}
	if v.Lifecycle, err = lifecycle.Load(req.Root); err != nil {
		return nil, err
	}
	if err := refuseHeldLifecycle(v.Lifecycle, ws, v.Releasable, req.Now); err != nil {
		return nil, err
	}
	if v.Environment, err = releaseEnvironmentFile(ws.Declarations, req.LiveRoot, req.Environment); err != nil {
		return nil, err
	}
	if err := live.RefuseStash("release", "The release commits, tags, and pushes this working tree, and a stash rides along in none of it."); err != nil {
		return nil, refuse("%v", err)
	}
	if err := validateTargets(ws, v.Releasable, v.ReleaseFile); err != nil {
		return nil, err
	}
	v.Primary = v.ReleaseFile.Include[0]
	if err := validatePipelines(ws, v.Releasable, v.Environment); err != nil {
		return nil, err
	}
	if err := refuseVersionSkew(ws, v.Releasable, req.LiveRoot, req.Registry); err != nil {
		return nil, err
	}
	if v.Syncs, err = OwedLockfileSyncs(repo, ws, name); err != nil {
		return nil, err
	}
	if err := RefuseUntidyGoModules(e, req.Root, req.LiveRoot, v.Syncs, v.Environment); err != nil {
		return nil, err
	}
	if err := refuseUserFacing(req.Root, name, v.ReleaseFile.Bump); err != nil {
		return nil, err
	}
	if err := refuseAppendOnlyBreach(repo, ws, v.Releasable, v.Lifecycle, req.Fork); err != nil {
		return nil, err
	}
	if err := decideVersion(v, req, log); err != nil {
		return nil, err
	}
	// The network: GitHub, the branch on origin, the tag on origin.
	if err := req.GitHub.CheckInstalled(); err != nil {
		return nil, refuse("%v", err)
	}
	if err := req.GitHub.CheckAuth(); err != nil {
		return nil, refuse("%v", err)
	}
	if v.GitHub, err = releaseRepository(live, ws.Declarations); err != nil {
		return nil, err
	}
	info, err := req.GitHub.Info(v.GitHub)
	if err != nil {
		return nil, refuse("GitHub could not be asked about %s (%v), and a release must know it may push there and whether the repository is public; check `gh auth status` and run the release again", v.GitHub, err)
	}
	if !info.CanPush {
		return nil, refusePushAccess(req.GitHub, v.GitHub)
	}
	if err := refusePublishing(v, repo, info, req.Now); err != nil {
		return nil, err
	}
	if v.Branch, err = validateBranch(live, ws.Declarations, log); err != nil {
		return nil, err
	}
	if err := refuseTagOnOrigin(repo, v.Decision.Tag); err != nil {
		return nil, err
	}
	v.PublishesFromCI = publishesFromCI(ws, v.Releasable)
	runs := ci.PublishRuns{GH: req.GitHub, Repo: v.GitHub, Git: repo, Log: log, Sleep: time.Sleep, Now: time.Now}
	if err := runs.Preflight("HEAD", v.PublishesFromCI); err != nil {
		return nil, refuse("%v", err)
	}
	v.CommitMessage = commitMessage(ws, name, v.Decision)
	return v, nil
}

// commitMessage is the version-bump commit's subject: the tag in a
// standalone repository, "<releasable>: release v<version>" in a workspace,
// the spellings releaserecord.VersionBumpSubjects recognizes.
func commitMessage(ws *workspace.Workspace, releasable string, d releaserecord.Decision) string {
	subjects := releaserecord.VersionBumpSubjects(releasable, d.Tag, d.Version)
	if ws.IsWorkspace() {
		return subjects[1]
	}
	return subjects[0]
}

// selectRelease is the releasable the release releases and the member it was
// started from. --releasable is required where the working directory selects
// no releasable (a member versioned under none), and refused where it
// selects one, since the directory already says which.
func selectRelease(ws *workspace.Workspace, req Request) (declarations.Releasable, declarations.Member, error) {
	rel, err := filepath.Rel(resolvePath(req.LiveRoot), resolvePath(req.Dir))
	if err != nil || rel == ".." || strings.HasPrefix(rel, "../") {
		return declarations.Releasable{}, declarations.Member{}, refuse("%s is outside the repository at %s", req.Dir, req.LiveRoot)
	}
	member, err := ws.MemberAtDirectory(filepath.Join(ws.Root, rel))
	if err != nil {
		return declarations.Releasable{}, declarations.Member{}, err
	}
	var names []string
	for _, r := range ws.Releasables() {
		names = append(names, r.Name)
	}
	if r, ok := ws.ReleasableOf(member); ok {
		if req.Releasable != "" {
			return declarations.Releasable{}, declarations.Member{}, refuse("--releasable %s is refused here: the working directory lies in the member %q, which is versioned under the releasable %q, so the directory already selects what to release; run without --releasable, or run from a member of %q", req.Releasable, member.Name, r.Name, req.Releasable)
		}
		return r, member, nil
	}
	if req.Releasable == "" {
		return declarations.Releasable{}, declarations.Member{}, refuse("the working directory lies in the member %q, which is versioned under no releasable, so it does not say what to release; name it with --releasable (%s), or run from a member of the releasable", member.Name, strings.Join(names, ", "))
	}
	r, ok := ws.Declarations.Releasable(req.Releasable)
	if !ok {
		return declarations.Releasable{}, declarations.Member{}, refuse("--releasable %s names no releasable; the declared releasables are %s", req.Releasable, strings.Join(names, ", "))
	}
	members := ws.MembersOf(r.Name)
	if len(members) == 0 {
		return declarations.Releasable{}, declarations.Member{}, refuse("the releasable %q has no member", r.Name)
	}
	return r, members[0], nil
}

// refuseInProgress refuses a release while an earlier one of the releasable
// is in progress, naming how to continue or undo it. A complete state (its
// run finished and stopped before clearing it) is returned for the release
// to clear.
func refuseInProgress(repo git.Repo, liveRoot string, r declarations.Releasable) (*runstate.InProgress, error) {
	state, found, err := runstate.LoadInProgress(liveRoot, r.Name)
	if err != nil || !found {
		return nil, err
	}
	if state.IsComplete(StepNames(), FatalSteps()) {
		return &state, nil
	}
	missing := state.MissingSteps(StepNames())
	fatal := FatalSteps()
	var failed []string
	for step := range state.FailedSteps {
		if fatal[step] {
			failed = append(failed, step)
		}
	}
	sort.Strings(failed)
	parts := []string{fmt.Sprintf("a release of %s is in progress (%s, %d of %d steps done", r.Name, state.Version, len(StepNames())-len(missing), len(StepNames()))}
	if len(missing) > 0 {
		parts = append(parts, "; left: "+strings.Join(missing, ", "))
	}
	if len(failed) > 0 {
		parts = append(parts, "; failed: "+strings.Join(failed, ", "))
	}
	statePath := filepath.Join(liveRoot, filepath.FromSlash(runstate.InProgressPath(r.Name)))
	fate := releaserecord.FateAbsent
	if version, perr := semver.Parse(state.Version); perr == nil {
		scheme, err := workspace.SchemeOf(r)
		if err != nil {
			return nil, err
		}
		if fate, err = releaserecord.New(repo, r.Name, scheme, "").Fate(version); err != nil {
			return nil, err
		}
	}
	switch {
	case fate.Released():
		parts = append(parts, "). Run `rlsbl release resume` to continue it, or `rlsbl release undo` to roll it back.")
	case fate == releaserecord.FateNeverReleased:
		parts = append(parts, fmt.Sprintf("). %s is recorded as never released, so the in-progress state is left over from an attempt already abandoned: delete it (saferm delete --on-error abort --description \"an abandoned release's state\" %s) and run the release again.", state.Version, statePath))
	default:
		parts = append(parts, fmt.Sprintf("). Run `rlsbl release resume` to continue it. `rlsbl release undo` does not apply: %s stopped before the step that records it, so there is no recorded release to revert. To abandon %s instead, run `rlsbl release abandon --approve-consequential`: it records %s as never released and deletes the in-progress state.", state.Version, state.Version, state.Version))
	}
	return nil, &ValidationError{Message: strings.Join(parts, "")}
}

// refuseHeldLifecycle evaluates lifecycle-allows-release for the releasable
// and every member versioned under it: a subject on hold or retired is not
// released.
func refuseHeldLifecycle(record *lifecycle.Record, ws *workspace.Workspace, r declarations.Releasable, now time.Time) error {
	subjects := []string{r.Name}
	for _, m := range ws.MembersOf(r.Name) {
		if m.Name != r.Name {
			subjects = append(subjects, m.Name)
		}
	}
	for _, s := range subjects {
		if err := record.ReleaseAllowed(s, now); err != nil {
			return &ValidationError{Message: err.Error()}
		}
	}
	return nil
}

// releaseEnvironmentFile is base with the declarations' environment file's
// variables added. A relative environment_file names a file beside the
// working tree, which is never committed, so it resolves there even while
// the release runs in the release checkout.
func releaseEnvironmentFile(d *declarations.Releasables, liveRoot string, base map[string]string) (map[string]string, error) {
	out := map[string]string{}
	for k, val := range base {
		out[k] = val
	}
	if d.EnvironmentFile == "" {
		return out, nil
	}
	file := d.EnvironmentFile
	if !strings.HasPrefix(file, "~/") && !filepath.IsAbs(file) {
		file = filepath.Join(liveRoot, filepath.FromSlash(file))
	}
	env, err := declarations.LoadEnvironmentFile(file)
	if err != nil {
		return nil, err
	}
	for k, val := range env {
		out[k] = val
	}
	return out, nil
}

// releaseTargets are the targets of the releasable's members, by name.
func releaseTargets(ws *workspace.Workspace, releasable string) (map[string]bool, error) {
	found := map[string]bool{}
	for _, m := range ws.MembersOf(releasable) {
		ts, err := targets.MemberTargets(ws.Root, m)
		if err != nil {
			return nil, err
		}
		for _, t := range ts {
			found[t.Name] = true
		}
	}
	return found, nil
}

// validateTargets refuses a release file whose include list is empty, that
// names a target rlsbl does not support, or whose include and exclude lists
// do not between them name every target the releasable's members have and
// nothing else.
func validateTargets(ws *workspace.Workspace, r declarations.Releasable, rf releaserecord.ReleaseFile) error {
	path := releaserecord.ReleaseFilePath(r.Name)
	if len(rf.Include) == 0 {
		return refuse("the release file %s includes no target; add at least one to include", path)
	}
	for _, name := range append(append([]string(nil), rf.Include...), rf.Exclude...) {
		if _, err := targets.Get(name); err != nil {
			return refuse("the release file %s names a target: %v", path, err)
		}
	}
	detected, err := releaseTargets(ws, r.Name)
	if err != nil {
		return err
	}
	declared := map[string]bool{}
	for _, name := range append(append([]string(nil), rf.Include...), rf.Exclude...) {
		declared[name] = true
	}
	var missing, extra []string
	for name := range detected {
		if !declared[name] {
			missing = append(missing, name)
		}
	}
	for name := range declared {
		if !detected[name] {
			extra = append(extra, name)
		}
	}
	sort.Strings(missing)
	sort.Strings(extra)
	if len(missing) > 0 {
		return refuse("the releasable %q has targets the release file %s names in neither include nor exclude: %s; add each to one of them", r.Name, path, strings.Join(missing, ", "))
	}
	if len(extra) > 0 {
		return refuse("the release file %s names targets no member of the releasable %q has: %s; remove them from include and exclude", path, r.Name, strings.Join(extra, ", "))
	}
	return nil
}

// validatePipelines refuses a releasable publishing nothing whose members
// declare a local pipeline, and a local pipeline whose credentials the
// environment lacks.
func validatePipelines(ws *workspace.Workspace, r declarations.Releasable, env map[string]string) error {
	var missing []string
	for _, m := range ws.MembersOf(r.Name) {
		for _, p := range m.Pipelines {
			if !p.Local {
				continue
			}
			if r.PublishMode == declarations.PublishNone {
				return refuse("the releasable %q publishes nothing (publish_mode = \"none\"), yet the member %q declares the local pipeline %q; delete the pipeline or its local = true, or set publish_mode = \"ci\" in %s", r.Name, m.Name, p.Name, declarations.ReleasablesFile)
			}
			names, err := pipelines.LocalSecretNames(p)
			if err != nil {
				return err
			}
			for _, n := range names {
				if _, ok := env[n]; ok {
					continue
				}
				if _, ok := os.LookupEnv(n); ok {
					continue
				}
				missing = append(missing, fmt.Sprintf("the pipeline %q of the member %q needs %s", p.Name, m.Name, n))
			}
		}
	}
	if len(missing) > 0 {
		return refuse("local pipelines lack their credentials in the environment:\n  %s\nSet each variable, in the environment or in the environment_file %s names, then run the release again", strings.Join(missing, "\n  "), declarations.ReleasablesFile)
	}
	return nil
}

// refuseVersionSkew refuses a release developed against a local checkout of
// a dependency ahead of what PyPI publishes: the dev overlays (in the working
// tree) install sibling checkouts, and a release built against unreleased
// dependency code must wait for that dependency's release. Only the
// project's package document is read from PyPI.
func refuseVersionSkew(ws *workspace.Workspace, r declarations.Releasable, liveRoot string, reg registry.Client) error {
	dirs := []string{liveRoot}
	for _, m := range ws.MembersOf(r.Name) {
		dirs = append(dirs, filepath.Join(liveRoot, filepath.FromSlash(m.Path)))
	}
	overlays, err := dependencies.CollectActiveOverlays(uniqueSorted(dirs))
	if err != nil {
		return err
	}
	pypi, err := targets.Get(targets.PyPI)
	if err != nil {
		return err
	}
	for _, o := range overlays {
		local, err := pypi.ReadVersion(o.Path)
		if err != nil {
			return refuse("the dev overlay of %s installs the checkout %s, whose version cannot be read (%v), so the release cannot tell whether it was developed against unreleased code of %s", o.Package, o.Path, err, o.Package)
		}
		latest, found, err := reg.LatestVersion(registry.Pypi, o.Package)
		if err != nil {
			return refuse("the dev overlay of %s installs a local checkout, and PyPI could not be asked whether %s %s is published (%v); the release needs the answer, so run it again once PyPI answers", o.Package, o.Package, local, err)
		}
		if !found {
			return refuse("release the dependency first: the dev overlay installs %s %s from %s, and PyPI publishes no %s at all", o.Package, local, o.Path, o.Package)
		}
		published, err := semver.Parse(latest)
		if err != nil {
			return refuse("PyPI's latest %s is %s, which is no MAJOR.MINOR.PATCH version the release can compare with the checkout's %s", o.Package, latest, local)
		}
		if semver.Compare(local, published) > 0 {
			return refuse("release the dependency first: the dev overlay installs %s %s from %s, ahead of %s on PyPI, so this release was developed and tested against code nobody can install", o.Package, local, o.Path, published)
		}
	}
	return nil
}

// refuseUserFacing holds the unreleased changelog to the bump's rule: an
// infra release has no user-facing entry, and every other release has at
// least one.
func refuseUserFacing(root, releasable string, bump semver.Bump) error {
	f, err := changelog.ReadUnreleased(root, changelog.Dir(releasable))
	if err != nil {
		return err
	}
	userFacing := false
	for _, entry := range f.Entries() {
		if entry.UserFacing {
			userFacing = true
		}
	}
	file := releaserecord.ReleaseFilePath(releasable)
	if bump == semver.Infra && userFacing {
		return refuse("an infra release has no user-facing changelog entry, and %s holds one; use patch, minor, or major in %s instead", f.Path, file)
	}
	if bump != semver.Infra && !userFacing {
		return refuse("a %s release needs at least one user-facing changelog entry in %s; a release of infrastructure changes alone sets bump = \"infra\" in %s", bump, f.Path, file)
	}
	return nil
}

// refuseAppendOnlyBreach holds the lifecycle-and-license record to the
// record at the releasable's nearest release commit: a closed period and a
// held registry name are never changed or dropped.
func refuseAppendOnlyBreach(repo git.Repo, ws *workspace.Workspace, r declarations.Releasable, record *lifecycle.Record, fork Fork) error {
	scheme, err := workspace.SchemeOf(r)
	if err != nil {
		return err
	}
	head, err := repo.Head()
	if err != nil {
		return err
	}
	nearest, err := releaserecord.New(repo, r.Name, scheme, fork.Upstream).Nearest(head)
	if err != nil || nearest == nil {
		return err
	}
	text, found, err := repo.FileAt(nearest.ReleaseCommit, lifecycle.RecordFile)
	if err != nil || !found {
		return err
	}
	previous, err := lifecycle.Parse([]byte(text))
	if err != nil {
		return refuse("the lifecycle-and-license record at %s, the release commit of %s %s, cannot be read, so the record cannot be held to it: %v", nearest.ReleaseCommit, r.Name, nearest.Version, err)
	}
	if err := record.CheckAppendOnly(previous); err != nil {
		return refuse("against the lifecycle-and-license record at %s, the release commit of %s %s: %v", nearest.ReleaseCommit, r.Name, nearest.Version, err)
	}
	return nil
}

// currentVersion reads what the version files name: a workspace
// releasable's version file, or a standalone project's primary target.
func currentVersion(ws *workspace.Workspace, releasable, primary string) (semver.Version, error) {
	if ws.IsWorkspace() {
		return ws.ReadReleasableVersion(releasable)
	}
	return standaloneVersion(ws, releasable, primary)
}

// decideVersion decides what the release ships through the release record
// (the decision `release run`, batch planning, and `rewrite
// project-name` share), on a checkout that contains the latest release, and
// refuses a tag that already exists here.
func decideVersion(v *Validated, req Request, log func(string)) error {
	name := v.Releasable.Name
	current, err := currentVersion(v.Workspace, name, v.Primary)
	if err != nil {
		return err
	}
	v.Current = current
	scheme, err := workspace.SchemeOf(v.Releasable)
	if err != nil {
		return err
	}
	v.Record = releaserecord.New(v.Repo, name, scheme, req.Fork.Upstream)
	head, err := v.Repo.Head()
	if err != nil {
		return err
	}
	if v.Latest, err = v.Record.RequireCheckoutContainsLatest(head); err != nil {
		return err
	}
	if v.Decision, err = v.Record.DecideVersion(current, v.ReleaseFile.Bump, v.Latest); err != nil {
		return err
	}
	if v.Decision.FirstRelease {
		log(fmt.Sprintf("First release: %s", v.Decision.Version))
	} else {
		log(fmt.Sprintf("Releasing %s: %s -> %s (%s)", name, current, v.Decision.Version, v.Decision.Bump))
	}
	_, tagged, err := v.Repo.TagCommit(v.Decision.Tag)
	if err != nil {
		return err
	}
	if tagged {
		return refuse("the tag %s already exists here, so %s cannot be released under it; a release never moves an existing tag. If it is left from a release that was undone, delete it (git tag -d %s) after checking nothing published it, then run the release again", v.Decision.Tag, v.Decision.Version, v.Decision.Tag)
	}
	return nil
}

// refuseTagOnOrigin refuses a tag origin already has at any commit: a stale
// tag left by an interrupted or partly undone release would otherwise
// surface only at the push, after the release mutated everything else. A
// remote that cannot be asked is refused too: a release does not start
// blind about whether its tag is free.
func refuseTagOnOrigin(repo git.Repo, tag string) error {
	commit, found, err := repo.RemoteTagCommit("origin", tag)
	if err != nil {
		return refuse("whether origin already has the tag %s could not be read (%v); a release does not start without knowing its tag is free, so make origin reachable and run the release again", tag, err)
	}
	if found {
		return refuse("origin already has the tag %s, at %s: the version may already be released, or a stale tag is left on origin. Find out which; delete a stale tag on origin (git push origin :refs/tags/%s) before releasing", tag, commit, tag)
	}
	return nil
}

// releaseRepository is the GitHub repository the release publishes to.
func releaseRepository(live git.Repo, d *declarations.Releasables) (github.Repository, error) {
	origin := ""
	configured, err := live.RemoteConfigured("origin")
	if err != nil {
		return github.Repository{}, err
	}
	if configured {
		if origin, err = live.RemoteURL("origin"); err != nil {
			return github.Repository{}, err
		}
	}
	repo, err := github.ResolveRepository(d.GitHubRepository, origin)
	if err != nil {
		return github.Repository{}, refuse("the release publishes a GitHub Release, and %v; declare github_repository in %s or add the origin remote", err, declarations.ReleasablesFile)
	}
	return repo, nil
}

// refusePushAccess is the refusal of an account that may not push to the
// repository, naming the account and a token in the environment that may
// stand in for gh's own login.
func refusePushAccess(gh github.Client, repo github.Repository) error {
	user, err := gh.AuthenticatedUser()
	if err != nil {
		user = "(the account could not be read: " + err.Error() + ")"
	}
	msg := fmt.Sprintf("the account gh is authenticated as, %s, may not push to %s, and a release pushes its candidate and its tag there", user, repo)
	for _, name := range []string{"GH_TOKEN", "GITHUB_TOKEN"} {
		if _, ok := os.LookupEnv(name); ok {
			msg += fmt.Sprintf("; %s is set in the environment and takes the place of gh's own login: unset it (unset %s) to use that login", name, name)
			break
		}
	}
	return refuse("%s", msg)
}

// visibilityOf is GitHub's visibility as the lifecycle library names it;
// GitHub's internal visibility is private.
func visibilityOf(info github.RepositoryInfo) lifecycle.Visibility {
	if info.Visibility == github.Public {
		return lifecycle.VisibilityPublic
	}
	return lifecycle.VisibilityPrivate
}

// refusePublishing evaluates the lifecycle and publishing rules a release
// is held to: proprietary-requires-private (the repository's visibility
// agrees with the record), proprietary-refuses-public-output and
// private-repository-publishing over everything the releasable's
// declarations and the committed workflows publish, and the deploy
// command's precondition.
func refusePublishing(v *Validated, repo git.Repo, info github.RepositoryInfo, now time.Time) error {
	source, err := publishrules.KnownVisibility(visibilityOf(info))
	if err != nil {
		return err
	}
	if err := publishrules.CheckVisibility(v.Lifecycle, source, now); err != nil {
		return &ValidationError{Message: err.Error()}
	}
	uses, err := publishrules.ReleasableUses(v.Workspace, v.Releasable.Name)
	if err != nil {
		return err
	}
	workflows, err := publishrules.CommittedWorkflows(repo, "HEAD")
	if err != nil {
		return err
	}
	workflowUses, err := publishrules.WorkflowUses(workflows)
	if err != nil {
		return err
	}
	if err := publishrules.CheckUses(v.Lifecycle, append(uses, workflowUses...), source, now); err != nil {
		return &ValidationError{Message: err.Error()}
	}
	if err := publishrules.DeployAllowed(v.Lifecycle, v.Releasable, now); err != nil {
		return &ValidationError{Message: err.Error()}
	}
	return nil
}

// validateBranch refuses a release from anything but a release branch, and
// from a branch behind origin's. The branch is fetched first; a fetch that
// fails is refused, since whether the branch is behind cannot then be
// known. A branch origin does not have yet is the first push, and passes.
func validateBranch(live git.Repo, d *declarations.Releasables, log func(string)) (string, error) {
	branch, err := LiveBranch(live)
	if err != nil {
		return "", err
	}
	isRelease := false
	for _, b := range d.ReleaseBranches {
		if b == branch {
			isRelease = true
		}
	}
	if !isRelease {
		return "", refuse("cannot release from %q: it is not a release branch (release_branches in %s: %s). Merge %q into a release branch, check that out, and release from there", branch, declarations.ReleasablesFile, strings.Join(d.ReleaseBranches, ", "), branch)
	}
	if err := live.FetchOrigin(); err != nil {
		return "", refuse("origin could not be fetched (%v), so whether %s is behind origin's cannot be known; make origin reachable and run the release again", err, branch)
	}
	remote, found, err := live.RemoteTrackingCommit("origin", branch)
	if err != nil {
		return "", err
	}
	if !found {
		log(fmt.Sprintf("origin has no %s yet; the candidate push creates it", branch))
		return branch, nil
	}
	behind, err := live.CountCommits([]string{remote}, []string{"refs/heads/" + branch})
	if err != nil {
		return "", err
	}
	if behind > 0 {
		return "", refuse("%s is %d commit(s) behind origin/%s; pull (git pull --ff-only) and run the release again", branch, behind, branch)
	}
	return branch, nil
}

// publishesFromCI reports whether the releasable's Release starts CI
// publish workflows: it publishes, and a member's pipeline publishes from
// CI.
func publishesFromCI(ws *workspace.Workspace, r declarations.Releasable) bool {
	if r.PublishMode != declarations.PublishCI {
		return false
	}
	for _, m := range ws.MembersOf(r.Name) {
		for _, p := range m.Pipelines {
			if !p.Local {
				return true
			}
		}
	}
	return false
}
