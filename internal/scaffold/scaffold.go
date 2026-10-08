// Package scaffold is `rlsbl scaffold`: it renders a member's release
// infrastructure from rlsbl's embedded templates and keeps it current. For
// each target the member has (declared in
// .strictmetadata/releasables/releasables.toml, or detected from its
// manifests) it writes the CI workflow, the Go module's VERSION, goreleaser
// configuration and version.go, the npm package's .npmignore, and an npm
// go-binary package's launcher; for the member's releasable it writes the
// publish workflow of every pipeline publishing from CI; and every member
// gets the .gitignore lines, the scratch directories, the stub go.mod files
// keeping private directories out of a Go module, a LICENSE from the
// lifecycle-and-license record, and the sandboxed test runner while
// rlsbl:test-sandbox is on. Settings the project's own files need (pytest's
// norecursedirs and nested-member ignores, hatchling's sdist exclusions) are
// merged into them. It installs the pre-push and post-rewrite git hooks. A
// workspace member's scaffold ends by regenerating the workspace's CI router
// and publish router, whose inputs the member's workflows are.
//
// Rendering is a plan first: every file is planned, every refusal made, and
// only then is anything written. Existing files are merged three ways
// against the merge base kept under .strictmetadata/.scaffold-bases/, and
// the files scaffold manages are recorded with their hashes in
// .strictmetadata/.scaffold-state/scaffold-state.toml, so a file the next
// run no longer renders is removed while it is unmodified. The template
// grammar is in render.go, the pinned action versions in actions.toml.
package scaffold

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/saferm"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// WorkspaceRootSkip is what scaffold says at a workspace's root, which it
// does not scaffold.
const WorkspaceRootSkip = "Skipping: rlsbl scaffold does not scaffold a workspace's root member. Its CI and publish workflows are the ones `rlsbl monorepo sync --auto-commit` generates, and its other files are written by hand."

// Inputs are one scaffold run.
type Inputs struct {
	// Dir is the absolute directory scaffold runs in, which must be a
	// member's directory: the member scaffolded.
	Dir string
	// Version is this rlsbl's version, recorded in the scaffold state.
	Version string
	// Target, when set, is a target the member must have: one it neither
	// declares nor has detected is added to its declared targets.
	Target string
	// PublishMode, when set, is the publish mode the member's releasable is
	// declared with ("ci" or "none").
	PublishMode string
	// AutoCommit commits what the run wrote.
	AutoCommit bool
	// SkipShared leaves the files every member gets (.gitignore, scratch
	// directories, stub go.mod files, LICENSE, the test runner) alone.
	SkipShared bool
	// DryRun is whether the command runs under --dry-run.
	DryRun bool
	// Now is the date the lifecycle-and-license record is asked about and
	// the year a LICENSE carries.
	Now time.Time
	// Say prints one line of the run's report.
	Say func(string)
	// GitHub answers the repository's visibility and adds its topic.
	GitHub github.Client
}

// Row is one line of the report's file table.
type Row struct {
	Path   string
	Status string
}

// Run scaffolds the member at in.Dir.
func Run(e *strictcli.Effects, in Inputs) error {
	if in.Say == nil {
		return errors.New("scaffold needs somewhere to report to")
	}
	root, err := declarations.FindRepositoryRoot(in.Dir)
	if err != nil {
		return err
	}
	data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(declarations.ReleasablesFile)))
	if errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("%s does not exist: rlsbl scaffold renders what the repository's release declarations say, and %s declares none. A workspace's are written by `rlsbl monorepo init`; a standalone project's declare its one releasable and its root member:\n\n%s", declarations.ReleasablesFile, root, standaloneSnippet)
	}
	if err != nil {
		return fmt.Errorf("reading %s: %w", declarations.ReleasablesFile, err)
	}
	d, err := declarations.Parse(data)
	if err != nil {
		return err
	}
	ws, err := workspace.New(root, d)
	if err != nil {
		return err
	}
	member, err := memberAt(ws, in.Dir)
	if err != nil {
		return err
	}
	if ws.IsWorkspace() && member.IsRoot() {
		in.Say(WorkspaceRootSkip)
		return nil
	}
	edited, data, err := applyFlags(ws, member, data, in)
	if err != nil {
		return err
	}
	if edited {
		if d, err = declarations.Parse(data); err != nil {
			return err
		}
		if ws, err = workspace.New(root, d); err != nil {
			return err
		}
		member, _ = d.MemberAt(member.Path)
	}
	repo, err := git.Open(e, root)
	if err != nil {
		return err
	}
	lock, err := runstate.Acquire(e, root, runstate.AcquireOptions{
		DryRun: in.DryRun,
		Wait:   runstate.WaitForHolder,
		OnWait: func(path string) {
			in.Say(fmt.Sprintf("Waiting for %s: another rlsbl process is changing this repository's release state", path))
		},
	})
	if err != nil {
		return err
	}
	defer lock.Release()
	return scaffoldMember(e, repo, ws, member, edited, data, in)
}

// standaloneSnippet is the declarations of a standalone project, for the
// refusal of a repository that declares none.
const standaloneSnippet = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]

[[releasables]]
name = "<the project's name>"
tag_format = "v{version}"
publish_mode = "ci"

[[members]]
path = "."
name = "root"
releasable = "<the project's name>"`

// memberAt is the member whose directory dir is. A directory inside a
// member but not its own is refused, naming the member.
func memberAt(ws *workspace.Workspace, dir string) (declarations.Member, error) {
	rel, err := ws.RelativePath(dir)
	if err != nil {
		return declarations.Member{}, err
	}
	if m, ok := ws.Declarations.MemberAt(rel); ok {
		return m, nil
	}
	m, _ := ws.MemberForDirectory(rel, true)
	return declarations.Member{}, fmt.Errorf("%s is not a member's directory: rlsbl scaffold scaffolds the member whose directory it runs in, and %s lies inside the member %q at %s; run it in %s", rel, rel, m.Name, m.Path, filepath.Join(ws.Root, filepath.FromSlash(m.Path)))
}

// applyFlags carries out --publish-mode and --target on the declarations
// text, and reports whether it changed it.
func applyFlags(ws *workspace.Workspace, m declarations.Member, data []byte, in Inputs) (bool, []byte, error) {
	ed, err := declarations.NewEditor(data)
	if err != nil {
		return false, nil, err
	}
	edited := false
	if in.PublishMode != "" {
		r, ok := ws.ReleasableOf(m)
		if !ok {
			return false, nil, fmt.Errorf("--publish-mode declares the publish mode of the member's releasable, and the member %q is versioned under none in %s", m.Name, declarations.ReleasablesFile)
		}
		mode := declarations.PublishMode(in.PublishMode)
		if mode != r.PublishMode {
			if err := ed.SetPublishMode(r.Name, mode); err != nil {
				return false, nil, err
			}
			edited = true
		}
	}
	if in.Target != "" {
		if _, err := targets.Get(in.Target); err != nil {
			return false, nil, fmt.Errorf("--target: %w", err)
		}
		current, err := targets.MemberTargets(ws.Root, m)
		if err != nil {
			return false, nil, err
		}
		has := false
		for _, t := range current {
			if t.Name == in.Target {
				has = true
			}
		}
		if !has {
			if err := ed.SetMemberTargets(m.Path, append(current, declarations.Target{Name: in.Target})); err != nil {
				return false, nil, err
			}
			edited = true
		}
	}
	if !edited {
		return false, data, nil
	}
	out, _, err := ed.Result()
	if err != nil {
		return false, nil, err
	}
	return true, out, nil
}

// run is one scaffold of one member, its refusals made before any write.
type run struct {
	e      *strictcli.Effects
	repo   git.Repo
	in     Inputs
	ctx    *memberContext
	rows   []Row
	commit []string
}

func (r *run) row(path, status string) { r.rows = append(r.rows, Row{path, status}) }

func scaffoldMember(e *strictcli.Effects, repo git.Repo, ws *workspace.Workspace, m declarations.Member, edited bool, declarationsText []byte, in Inputs) error {
	root := ws.Root
	r := &run{e: e, repo: repo, in: in}
	ctx, err := newMemberContext(e, repo, ws, m, in)
	if err != nil {
		return err
	}
	r.ctx = ctx
	if err := refuseTrackedScratchFiles(repo, m.Path); err != nil {
		return err
	}
	hooksDir, err := HooksDirectory(e, root)
	if err != nil {
		return err
	}
	hooks, err := planHooks(hooksDir)
	if err != nil {
		return err
	}
	state, stateFound, err := ReadState(root)
	if err != nil {
		return err
	}
	if err := refuseMissingBases(root, ws, m, state, stateFound); err != nil {
		return err
	}
	if err := ctx.refuseNewVersionDisagreement(); err != nil {
		return err
	}
	renders, err := ctx.renders()
	if err != nil {
		return err
	}
	publish, publishSkip, err := ctx.publishRender()
	if err != nil {
		return err
	}
	if publishSkip == "" {
		renders = append(renders, publish)
	}
	var plans []filePlan
	planned := map[string]bool{}
	for _, f := range renders {
		planned[f.path] = true
		if f.shared && in.SkipShared {
			continue
		}
		p, err := planFile(root, repo, e, f)
		if err != nil {
			return err
		}
		plans = append(plans, p)
	}
	merges, err := ctx.projectFileMerges()
	if err != nil {
		return err
	}
	orphans, err := planOrphans(root, ws, m, state, planned, publishSkip)
	if err != nil {
		return err
	}
	tagged, err := ctx.taggingPlan(r)
	if err != nil {
		return err
	}

	// Everything is decided: write.
	if edited {
		if _, err := declarations.Write(e, root, declarationsText); err != nil {
			return err
		}
		r.row(declarations.ReleasablesFile, statusUpdated)
		r.commit = append(r.commit, declarations.ReleasablesFile)
	}
	newState := State{RlsblVersion: in.Version, Files: map[string]string{}}
	for p, hash := range state.Files {
		owner, _ := ws.Declarations.MemberForPath(p)
		if owner.Path != m.Path || (in.SkipShared && planned[p] && isSharedPath(renders, p)) {
			newState.Files[p] = hash
		}
	}
	var conflicted []string
	for _, p := range plans {
		if p.healed != "" {
			if in.DryRun {
				in.Say(fmt.Sprintf("%s: merge base would be rebuilt from the last scaffold commit %s", p.path, p.healed))
			} else {
				in.Say(fmt.Sprintf("%s: merge base rebuilt from the last scaffold commit %s", p.path, p.healed))
			}
		}
		if err := applyPlan(e, root, p); err != nil {
			return err
		}
		if p.record {
			newState.Files[p.path] = p.hash
		}
		r.row(p.path, p.status)
		if len(p.conflicts) > 0 {
			conflicted = append(conflicted, DescribeConflicts(p.path, p.conflicts))
			continue
		}
		if p.write || p.chmod || p.uncommittedBase {
			r.commit = append(r.commit, p.path)
		}
		if p.writeBase || p.uncommittedBase {
			r.commit = append(r.commit, BasePath(p.path))
		}
	}
	for _, mg := range merges {
		r.row(mg.path, mg.status)
		if mg.content == nil {
			continue
		}
		if err := replaceFile(e, filepath.Join(root, filepath.FromSlash(mg.path)), mg.content); err != nil {
			return err
		}
		r.commit = append(r.commit, mg.path)
	}
	for _, o := range orphans {
		if err := saferm.Delete(e, root, saferm.Request{Path: o.path, Description: "rlsbl scaffold no longer renders this file (" + o.reason + ")", SkipMissing: true}); err != nil {
			return err
		}
		r.commit = append(r.commit, o.path)
		if o.base {
			if err := saferm.Delete(e, root, saferm.Request{Path: BasePath(o.path), Description: "the merge base of a file rlsbl scaffold no longer renders", SkipMissing: true}); err != nil {
				return err
			}
			r.commit = append(r.commit, BasePath(o.path))
		}
		r.row(o.path, statusRemoved+" ("+o.reason+")")
	}
	if err := installHooks(e, hooks, in.DryRun, in.Say); err != nil {
		return err
	}
	if err := WriteState(e, root, newState); err != nil {
		return err
	}
	// The run-state directory's .gitignore, written when the run took the
	// lock, is committed with the scaffold so the directory never shows as
	// untracked.
	r.commit = append(r.commit, declarations.ScaffoldStateFile, stateDir+"/manifest.toml", declarations.ScaffoldBasesDir+"/manifest.toml", runstate.GitignorePath)
	if err := r.tag(tagged); err != nil {
		return err
	}
	r.report(publishSkip)
	if in.AutoCommit {
		if err := r.commitWritten(); err != nil {
			return err
		}
	} else {
		in.Say("Not committed (--no-auto-commit).")
	}
	if len(conflicted) > 0 {
		return fmt.Errorf("the three-way merge left conflicts, written with git's markers and not committed: %s. Resolve the marked regions (keep your changes, drop the markers), then run rlsbl scaffold again", strings.Join(conflicted, "; "))
	}
	if ws.IsWorkspace() {
		return syncWorkspace(e, ws, in)
	}
	return nil
}

// syncWorkspace regenerates a workspace's CI router and publish router once
// a member is scaffolded, because the member's workflows are their inputs.
// A sync that fails fails the scaffold: the routers would otherwise lag the
// member's workflows with nothing saying so.
func syncWorkspace(e *strictcli.Effects, ws *workspace.Workspace, in Inputs) error {
	if in.DryRun {
		// The routers are read from the member's workflows on disk, which a
		// dry run did not write, so a preview of the sync would describe the
		// workflows as they were.
		in.Say("Would regenerate the workspace's CI router and publish router from the member's workflows, as monorepo sync does.")
		return nil
	}
	actions, err := ActionVersions()
	if err != nil {
		return err
	}
	result, err := workflows.Sync(e, ws, workflows.SyncInputs{
		Actions: actions,
		RootPublishWorkflow: func(root declarations.Member) (string, error) {
			return MemberPublishWorkflow(e, ws, root, in.GitHub, in.Now)
		},
		AutoCommit: in.AutoCommit,
	})
	if err != nil {
		return fmt.Errorf("the member is scaffolded, and regenerating the workspace's routers from its workflows failed: %w", err)
	}
	for _, n := range result.ImportNames {
		in.Say(fmt.Sprintf("Declared import_name %q for %s", n.ImportName, n.Member))
	}
	for _, f := range result.Written {
		in.Say("Wrote " + f)
	}
	for _, f := range result.Removed {
		in.Say("Removed " + f)
	}
	if result.Committed {
		in.Say("Committed: " + workflows.SyncCommitMessage)
	}
	return nil
}

// MemberPublishWorkflow is the publish workflow scaffold renders for the
// member m. A workspace's publish router inlines the root member's jobs
// from it, since the root member's own publish.yml is the router itself. A
// member that publishes nothing from CI is refused, naming why.
func MemberPublishWorkflow(e *strictcli.Effects, ws *workspace.Workspace, m declarations.Member, gh github.Client, now time.Time) (string, error) {
	repo, err := git.Open(e, ws.Root)
	if err != nil {
		return "", err
	}
	c, err := newMemberContext(e, repo, ws, m, Inputs{Now: now, GitHub: gh})
	if err != nil {
		return "", err
	}
	f, skip, err := c.publishRender()
	if err != nil {
		return "", err
	}
	if skip != "" {
		return "", fmt.Errorf("the member %q has no publish workflow: %s", m.Name, skip)
	}
	return f.theirs, nil
}

// isSharedPath reports whether path is one of the shared renders.
func isSharedPath(renders []render, path string) bool {
	for _, f := range renders {
		if f.path == path {
			return f.shared
		}
	}
	return false
}

// replaceFile writes content to path through a temporary file renamed over
// it, so no reader sees a partly written file.
func replaceFile(e *strictcli.Effects, path string, content []byte) error {
	tmp := path + ".rlsbl-writing"
	if _, err := e.Write(tmp, string(content)); err != nil {
		return err
	}
	_, err := e.Rename(tmp, path)
	return err
}

// newMemberContext gathers what the member's renders are computed from.
func newMemberContext(e *strictcli.Effects, repo git.Repo, ws *workspace.Workspace, m declarations.Member, in Inputs) (*memberContext, error) {
	c := &memberContext{e: e, repo: repo, ws: ws, member: m, now: in.Now, github: in.GitHub}
	c.releasable, c.versioned = ws.ReleasableOf(m)
	found, err := targets.MemberTargets(ws.Root, m)
	if err != nil {
		return nil, err
	}
	if len(found) == 0 {
		return nil, fmt.Errorf("the member %q at %s has no target: it declares none in %s and holds no go.mod, package.json, or pyproject.toml with a [project] table. Create the project's manifest, or pass --target", m.Name, m.Path, declarations.ReleasablesFile)
	}
	for _, t := range found {
		rel := t.Path
		if rel == "" {
			rel = declarations.RootPath
		}
		c.targets = append(c.targets, memberTarget{name: t.Name, dir: rel, abs: filepath.Join(ws.Root, filepath.FromSlash(m.TargetDir(t)))})
	}
	sort.SliceStable(c.targets, func(i, j int) bool { return c.targets[i].name < c.targets[j].name })
	record, err := lifecycle.Load(ws.Root)
	if err != nil {
		return nil, err
	}
	c.record = record
	if c.versioned {
		if period, ok := record.LicenseOn(c.releasable.Name, in.Now); ok {
			c.license = period.License
		}
	}
	reg, err := options.Shipped()
	if err != nil {
		return nil, err
	}
	opts, err := options.Load(reg, ws.Root, ws.Declarations)
	if err != nil {
		return nil, err
	}
	c.options = opts
	runner, runnerFound, err := declarations.LoadTestRunner(ws.Root)
	if err != nil {
		return nil, err
	}
	problems, err := opts.SettingsProblems(ws.Declarations, runnerFound)
	if err != nil {
		return nil, err
	}
	if len(problems) > 0 {
		return nil, errors.New(strings.Join(problems, "\n"))
	}
	c.runner = runner
	if name, ok, err := gitAnswer(e, ws.Root, "config", "--get", "user.name"); err != nil {
		return nil, err
	} else if ok {
		c.author = name
	}
	if c.publishesFromCI() || c.declaresHomebrewTap() {
		f, err := c.workflowFeatures()
		if err != nil {
			return nil, err
		}
		c.features = f
	}
	return c, nil
}

// ciPipelines are the member's pipelines publishing from CI, when its
// releasable publishes from CI.
func (c *memberContext) ciPipelines() []declarations.Pipeline {
	if !c.versioned || c.releasable.PublishMode != declarations.PublishCI {
		return nil
	}
	var out []declarations.Pipeline
	for _, p := range c.member.Pipelines {
		if !p.Local {
			out = append(out, p)
		}
	}
	return out
}

func (c *memberContext) publishesFromCI() bool { return len(c.ciPipelines()) > 0 }

func (c *memberContext) declaresHomebrewTap() bool {
	for _, p := range c.member.Pipelines {
		if p.HomebrewTap != "" {
			return true
		}
	}
	return false
}

// githubRepository is the repository on GitHub: the declared one, else the
// one the origin remote names.
func (c *memberContext) githubRepository() (github.Repository, error) {
	if c.repoResolved {
		return c.repoName, nil
	}
	origin := ""
	if c.ws.Declarations.GitHubRepository == "" {
		url, err := c.repo.RemoteURL("origin")
		if err != nil {
			return github.Repository{}, fmt.Errorf("the repository's GitHub name comes from github_repository in %s or from the origin remote, and neither gives one: %w", declarations.ReleasablesFile, err)
		}
		origin = url
	}
	repo, err := github.ResolveRepository(c.ws.Declarations.GitHubRepository, origin)
	if err != nil {
		return github.Repository{}, err
	}
	c.repoName, c.repoResolved = repo, true
	return repo, nil
}

// workflowFeatures are the publishing features the repository may render,
// from the record and, unless the record makes the repository confidential,
// GitHub's visibility of it.
func (c *memberContext) workflowFeatures() (Features, error) {
	var source publishrules.VisibilitySource
	if !c.record.Confidential(c.now) {
		repo, err := c.githubRepository()
		if err != nil {
			return Features{}, err
		}
		source = publishrules.GitHubVisibility(c.github, repo)
	}
	f, err := publishrules.ScaffoldFeatures(c.record, source, c.now)
	if err != nil {
		return Features{}, err
	}
	return Features{BuildAttestations: f.BuildAttestations, GoProxyNotification: f.GoProxyNotification, RepositoryURLs: f.RepositoryURLs}, nil
}

// publishRender is the member's publish workflow, or why it gets none.
func (c *memberContext) publishRender() (render, string, error) {
	path := c.memberPath(workflows.PublishPath)
	switch {
	case !c.versioned:
		return render{}, "the member is versioned under no releasable", nil
	case c.releasable.PublishMode != declarations.PublishCI:
		return render{}, fmt.Sprintf("publish_mode %q: %q publishes nothing", c.releasable.PublishMode, c.releasable.Name), nil
	case !c.publishesFromCI():
		return render{}, "no pipeline of the member publishes from CI", nil
	}
	pattern, err := workflows.CheckPatternForTargets(c.targetNames())
	if err != nil {
		return render{}, "", err
	}
	wait, err := workflows.WaitForCIJob(pattern)
	if err != nil {
		return render{}, "", err
	}
	scheme, err := workspace.SchemeOf(c.releasable)
	if err != nil {
		return render{}, "", err
	}
	var jobs []PublishJob
	for _, p := range c.ciPipelines() {
		t, ok := c.target(p.Target)
		if !ok {
			return render{}, "", fmt.Errorf("the pipeline %q publishes the target %q, which the member %q does not have", p.Name, p.Target, c.member.Name)
		}
		pt := PublishTarget{Pipeline: p, Dir: t.dir, License: c.license, Tag: workflows.TagPartsOf(scheme), HomebrewTap: p.HomebrewTap != ""}
		switch {
		case p.Artifact == declarations.ArtifactGoBinary:
			if pt.BinaryName, err = c.packagedBinary(t.name); err != nil {
				return render{}, "", err
			}
			if p.Type == declarations.TargetNPM {
				name, found, err := npmPackageName(t.abs)
				if err != nil {
					return render{}, "", err
				}
				if !found {
					return render{}, "", fmt.Errorf("%s/package.json declares no name; the go-binary pipeline %q publishes the package it names", c.memberPath(t.dir), p.Name)
				}
				pt.PackageName = name
				if c.features.RepositoryURLs {
					repo, err := c.githubRepository()
					if err != nil {
						return render{}, "", err
					}
					pt.RepositoryURL = "https://github.com/" + repo.String()
				}
			}
		case p.Type == declarations.TargetGo:
			module, _, err := gomodule.ModulePath(t.abs)
			if err != nil {
				return render{}, "", err
			}
			pt.ModulePath = module
		case p.Type == declarations.TargetNPM:
			manager, found, err := targets.PackageManager(t.abs)
			if err != nil {
				return render{}, "", err
			}
			if !found {
				return render{}, "", fmt.Errorf("the npm pipeline %q publishes the package in %s, which has no lockfile, and its publish job installs from one (npm ci); run npm install there and commit package-lock.json (or pnpm-lock.yaml, or yarn.lock)", p.Name, c.memberPath(t.dir))
			}
			pt.PackageManager = manager
			registry, err := npmRegistry(t.abs)
			if err != nil {
				return render{}, "", err
			}
			pt.RegistryURL = registry
		}
		job, err := RenderPublishJob(pt, c.features)
		if err != nil {
			return render{}, "", err
		}
		jobs = append(jobs, job)
	}
	text, err := PublishWorkflow(wait, jobs)
	if err != nil {
		return render{}, "", err
	}
	return render{path: path, theirs: text}, "", nil
}
