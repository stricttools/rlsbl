package monorepo

import (
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// `monorepo absorb` brings an external repository into a workspace as a
// member: its history is rewritten under the member's path by
// git-filter-repo and merged in, its version tags are created under the
// releasable's scheme at the rewritten commits (the fetch carries no tag, so
// a tag the workspace owns is never moved), and its records move into the
// new layout: its changelog and release archives into the releasable's
// directories, every commit id and release commit mapped through the
// rewrite's commit map, its lifecycle-and-license entries into the
// workspace's record under the member's name, and its transition record's
// events into the workspace's, scoped to the releasable. The arriving
// repository may be in the new layout already, or declare nothing at all;
// one still in the old layout is refused.
//
// Every refusal is made by the observation, before anything is written. A
// run that stopped part-way is completed by running it again: the merge is
// found by its trailers, and every later step does only what is left.

// The preview's items, in apply order.
const (
	absorbItemSource     = "source"
	absorbItemReleasable = "releasable"
	absorbItemHistory    = "history"
	absorbItemTags       = "tags"
	absorbItemState      = "state"
	absorbItemWorkspace  = "workspace"
	absorbItemRecord     = "transition-record"
	absorbItemNextSteps  = "next-steps"
)

// The trailers of an absorb's merge commit, which a run completing an
// interrupted absorb finds it by: what was absorbed where, the arriving
// repository's root commit (which no rename or move of it changes), and the
// releasable it joined.
const (
	AbsorbTrailer           = "Rlsbl-Absorb"
	AbsorbSourceTrailer     = "Rlsbl-Absorb-Source"
	AbsorbReleasableTrailer = "Rlsbl-Absorb-Releasable"
)

// AbsorbCommitMessage is the message of the commit declaring the absorbed
// member.
func AbsorbCommitMessage(name string) string { return "monorepo: absorb " + name }

// AbsorbRequest is one `monorepo absorb`.
type AbsorbRequest struct {
	// Source is the absolute path of the repository absorbed.
	Source string
	// Dest is the member's path, relative to the workspace root.
	Dest string
	// Name is the member's name; empty takes the path's last element.
	Name         string
	RegistryName string
	// Releasable names a declared releasable the member joins; empty
	// creates a releasable named after the member.
	Releasable string
	// TagFormat and PublishMode declare the releasable the absorb creates.
	TagFormat   string
	PublishMode string
	// License is the license the releasable the absorb creates is created
	// under: an SPDX identifier or proprietary. Required when the absorb
	// creates a releasable, refused with Releasable.
	License      string
	DeleteWithRm bool
	Now          time.Time
	// Version is this rlsbl's version, which the member's scaffold records.
	Version string
	GitHub  github.Client
	Say     func(string)
}

// tagImport is one arriving tag's name under the releasable's scheme.
type tagImport struct {
	old, next string
	version   string
	commit    string
}

// arrival is everything observation resolved about one absorb.
type arrival struct {
	req        AbsorbRequest
	ws         *workspace.Workspace
	name       string
	releasable string
	creates    bool
	// created is the releasable the absorb creates; nil when it joins one,
	// or an earlier run created it.
	created      *declarations.Releasable
	tagFormat    string
	scheme       workspace.TagScheme
	sourceScheme *workspace.TagScheme
	// sourceDecls are the arriving repository's declarations; nil when it
	// declares nothing.
	sourceDecls *declarations.Releasables
	sourceName  string
	sourceRoot  string
	sourceURL   string
	targetNames []string
	version     string
	imports     []tagImport
	skipped     []string
	alias       *tagImport
	already     []string
	merge       string
	present     bool
	// edited are the workspace's declarations with the member, and the
	// releasable the absorb creates; nil when the member is declared
	// already.
	edited        []byte
	editedDecls   *declarations.Releasables
	floorSwitches []depFloorsSwitch
	record        *lifecycle.Record
	events        []releaserecord.Event
	changelogs    []string
	archives      []string
	unreleased    int
	publishers    []targets.Facts
}

// absorbRun is what the apply learned as it ran.
type absorbRun struct {
	lock      *runstate.Lock
	clone     string
	commitMap map[string]string
	tagMaps   []releaserecord.TagMapping
	remaps    []releaserecord.CommitMapping
	aliasAt   string
	migrated  int
	present   int
	notes     []string
	commit    string
}

// Absorb runs `monorepo absorb` on the workspace ws.
func Absorb(ctx *strictcli.Context, ws *workspace.Workspace, req AbsorbRequest) error {
	if req.Say == nil {
		return errors.New("monorepo absorb needs somewhere to report to")
	}
	var arr *arrival
	run := &absorbRun{}
	defer func() {
		if run.clone != "" {
			if _, err := ctx.Effects().Remove(run.clone); err != nil {
				req.Say(fmt.Sprintf("Note: the working clone %s could not be removed: %v", run.clone, err))
			}
		}
		if run.lock != nil {
			run.lock.Release()
		}
	}()
	_, err := previewapply.Reconcile(ctx, previewapply.Reconciler{
		Observe: func(o previewapply.Observer) (previewapply.Preview, error) {
			var err error
			arr, err = observeArrival(o, ws, req)
			if err != nil {
				return previewapply.Preview{}, err
			}
			return arr.preview()
		},
		Apply: func(e *strictcli.Effects, it previewapply.Item) error {
			return arr.apply(e, it, run)
		},
		ShowKeys: true,
	})
	return err
}

// observeArrival resolves and checks one absorb, reading only.
func observeArrival(o previewapply.Observer, ws *workspace.Workspace, req AbsorbRequest) (*arrival, error) {
	if err := requireWorkspace(ws, "absorb"); err != nil {
		return nil, err
	}
	if problem := declarations.PathProblem(req.Dest); problem != "" {
		return nil, fmt.Errorf("the destination: %s", problem)
	}
	if req.Dest == declarations.RootPath {
		return nil, fmt.Errorf("the destination is the repository root, which the root member owns; absorb into a directory below it")
	}
	name := req.Name
	if name == "" {
		name = path.Base(req.Dest)
	}
	if problem := declarations.NameProblem(name); problem != "" {
		return nil, fmt.Errorf("the member's name: %s", problem)
	}
	if name == declarations.RootName {
		return nil, fmt.Errorf("the name %q is reserved for the root member (path \".\"); name the absorbed member otherwise with --name", declarations.RootName)
	}
	if !filepath.IsAbs(req.Source) {
		return nil, fmt.Errorf("the source repository path %q is not absolute", req.Source)
	}
	source := filepath.Clean(req.Source)
	req.Source = source
	if info, err := os.Stat(source); err != nil || !info.IsDir() {
		return nil, fmt.Errorf("the source repository %s does not exist or is not a directory", source)
	}
	if !git.IsRepositoryRoot(source) {
		return nil, fmt.Errorf("the source %s is not the root of a git repository", source)
	}
	src, err := git.Open(o, source)
	if err != nil {
		return nil, err
	}
	if changed, err := src.ChangedPaths(nil, git.UntrackedNormal); err != nil {
		return nil, err
	} else if len(changed) > 0 {
		return nil, fmt.Errorf("the source repository %s has uncommitted changes, and the absorb rewrites committed history only, so they would be lost: commit or remove these there first:\n  %s", source, strings.Join(changed, "\n  "))
	}
	if err := requireFilterRepo(); err != nil {
		return nil, err
	}
	if err := requireSaferm(req.DeleteWithRm, "the arriving changelog and release records, once moved into the releasable,"); err != nil {
		return nil, err
	}
	arr := &arrival{req: req, ws: ws, name: name}
	if err := arr.observeSourceLayout(); err != nil {
		return nil, err
	}
	roots, err := rootCommits(o, source)
	if err != nil {
		return nil, err
	}
	arr.sourceRoot = roots[0]
	if arr.sourceURL, err = originOrPath(src); err != nil {
		return nil, err
	}
	arr.releasable = req.Releasable
	if arr.releasable == "" {
		arr.releasable = name
	}
	repo, err := git.Open(o, ws.Root)
	if err != nil {
		return nil, err
	}
	if arr.merge, err = findAbsorbMerge(o, repo, name, req.Dest, roots, arr.releasable); err != nil {
		return nil, err
	}
	healing := arr.merge != ""
	for _, m := range ws.Members() {
		switch {
		case m.Path == req.Dest && m.Name == name:
			if !healing {
				return nil, fmt.Errorf("the member %q is declared at %s already", name, req.Dest)
			}
			arr.present = true
		case m.Path == req.Dest:
			return nil, fmt.Errorf("the path %s is the member %q's already", req.Dest, m.Name)
		case m.Name == name:
			return nil, fmt.Errorf("the member at %s is named %q already; name the absorbed member otherwise with --name", m.Path, name)
		}
	}
	if !healing {
		if exists, err := pathExists(ws.Root, req.Dest); err != nil {
			return nil, err
		} else if exists {
			return nil, fmt.Errorf("%s exists on disk already, and the absorbed history arrives under that path; absorb into a path this repository does not use", req.Dest)
		}
	}
	if dirty, err := dirtyPaths(repo); err != nil {
		return nil, err
	} else if len(dirty) > 0 {
		return nil, fmt.Errorf("the working tree has uncommitted changes, and the absorb merges a history into this repository and commits its own changes: commit or remove these first:\n  %s", strings.Join(dirty, "\n  "))
	}
	if err := arr.observeReleasable(); err != nil {
		return nil, err
	}
	if err := requireLicenseForCreated(arr.creates, req.License, fmt.Sprintf("the member joins the declared releasable %q", arr.releasable)); err != nil {
		return nil, err
	}
	if arr.creates {
		if err := requirePrivateForProprietary(req.License, req.GitHub, repo, ws.Declarations.GitHubRepository); err != nil {
			return nil, err
		}
	}
	sourceTags, err := src.TagCommits()
	if err != nil {
		return nil, err
	}
	if err := arr.observeVersion(src, sourceTags); err != nil {
		return nil, err
	}
	if err := arr.observeState(); err != nil {
		return nil, err
	}
	if !healing {
		if err := arr.refuseVersionOverlap(sourceTags); err != nil {
			return nil, err
		}
	}
	if err := arr.observeTags(repo, sourceTags); err != nil {
		return nil, err
	}
	if err := arr.observeDeclarations(); err != nil {
		return nil, err
	}
	if err := arr.observeRecords(src); err != nil {
		return nil, err
	}
	return arr, nil
}

// rootCommits are the arriving repository's parentless commits: its
// identity, which no rename, move, or clone of it changes.
func rootCommits(run git.Runner, dir string) ([]string, error) {
	c, err := run.Run([]interface{}{"git", "rev-list", "--max-parents=0", "HEAD"}, strictcli.Cwd(dir), strictcli.Check(false), strictcli.Timeout(conversionGitTimeout))
	if err != nil {
		return nil, err
	}
	if c.ExitCode() != 0 {
		return nil, fmt.Errorf("git rev-list in %s exited %d: %s; the source repository needs a commit", dir, c.ExitCode(), strings.TrimSpace(c.Stderr()))
	}
	roots := strings.Fields(c.Stdout())
	if len(roots) == 0 {
		return nil, fmt.Errorf("the source repository %s has no commit", dir)
	}
	sort.Strings(roots)
	return roots, nil
}

// absorbMarker is the trailer line naming what an absorb's merge absorbed.
func absorbMarker(name, dest string) string {
	return AbsorbTrailer + ": " + name + " " + dest
}

// findAbsorbMerge is the merge of an earlier absorb of the same name and
// path, and "" when there is none. A merge from another repository (its
// recorded root commit is not the source's), or one that joined another
// releasable, is refused: completing it would skip the version checks on
// the ground that the run is the same absorb.
func findAbsorbMerge(run git.Runner, repo git.Repo, name, dest string, roots []string, releasable string) (string, error) {
	marker := absorbMarker(name, dest)
	c, err := run.Run([]interface{}{"git", "log", "--fixed-strings", "--grep=" + marker, "--format=%H"}, strictcli.Cwd(repo.Dir()), strictcli.Check(false), strictcli.Timeout(conversionGitTimeout))
	if err != nil {
		return "", err
	}
	if c.ExitCode() != 0 {
		return "", fmt.Errorf("git log in %s exited %d: %s", repo.Dir(), c.ExitCode(), strings.TrimSpace(c.Stderr()))
	}
	shas := strings.Fields(c.Stdout())
	for _, sha := range shas {
		recorded, err := repo.TrailerValues(sha, AbsorbSourceTrailer)
		if err != nil {
			return "", err
		}
		same := false
		for _, r := range recorded {
			for _, root := range roots {
				if r == root {
					same = true
				}
			}
		}
		if !same {
			return "", fmt.Errorf("%s was absorbed as %q already by the commit %s, from another repository (its root commit is %s, this source's is %s). Two repositories cannot share one member path: absorb this one under another name and path, or remove the member first", dest, name, short(sha), short(strings.Join(recorded, ", ")), short(roots[0]))
		}
		joined, err := repo.TrailerValues(sha, AbsorbReleasableTrailer)
		if err != nil {
			return "", err
		}
		if len(joined) == 0 {
			return "", fmt.Errorf("%s was absorbed as %q already by the commit %s, which records no %s trailer, so the releasable it joined is unknown. Finish that absorb by hand, or reset this repository to before %s and absorb again", dest, name, short(sha), AbsorbReleasableTrailer, short(sha))
		}
		if joined[0] != releasable {
			how := "pass --releasable " + joined[0] + " to complete it"
			if joined[0] == name {
				how = "drop --releasable to complete it: that run created the releasable named after the member"
			}
			return "", fmt.Errorf("%s was absorbed as %q already by the commit %s, versioned under the releasable %q, and this run names %q. A run completes the same absorb, so %s", dest, name, short(sha), joined[0], releasable, how)
		}
		return sha, nil
	}
	return "", nil
}

// observeSourceLayout reads what the arriving repository declares: a
// standalone project's declarations, or nothing. A workspace, and a
// repository in the old layout, are refused.
func (arr *arrival) observeSourceLayout() error {
	source := arr.req.Source
	found, err := pathExists(source, declarations.ReleasablesFile)
	if err != nil {
		return err
	}
	if !found {
		for _, old := range []string{".rlsbl", ".rlsbl-monorepo"} {
			if exists, err := pathExists(source, old); err != nil {
				return err
			} else if exists {
				return fmt.Errorf("the source repository %s keeps its records in the old layout (%s/), which rlsbl no longer reads. Convert them first: run `rlsbl migrate records` in %s and commit what it writes, then run this again", source, old, source)
			}
		}
		return nil
	}
	d, err := declarations.Load(source)
	if err != nil {
		return fmt.Errorf("the source repository's declarations are refused: %w", err)
	}
	if d.IsWorkspace() {
		return fmt.Errorf("the source repository %s is a workspace, and an absorb brings one standalone project in as a member. Extract each of its releasables with `rlsbl monorepo extract` first, then absorb those", source)
	}
	arr.sourceDecls = d
	arr.sourceName = d.Releasables[0].Name
	scheme, err := workspace.SchemeOf(d.Releasables[0])
	if err != nil {
		return err
	}
	arr.sourceScheme = &scheme
	return nil
}

// sourceRoot is the arriving repository's root member; a bare member for
// one that declares nothing.
func (arr *arrival) sourceRootMember() declarations.Member {
	if arr.sourceDecls == nil {
		return declarations.Member{Path: declarations.RootPath, Name: declarations.RootName}
	}
	return arr.sourceDecls.RootMember()
}

// observeReleasable resolves the releasable the member is versioned under:
// the declared one it joins, or the one it creates, whose tag format and
// publish mode are stated.
func (arr *arrival) observeReleasable() error {
	req, d := arr.req, arr.ws.Declarations
	healing := arr.merge != ""
	var sourceMode declarations.PublishMode
	if arr.sourceDecls != nil {
		sourceMode = arr.sourceDecls.Releasables[0].PublishMode
	}
	if req.Releasable != "" {
		r, ok := d.Releasable(req.Releasable)
		if !ok {
			var names []string
			for _, r := range arr.ws.Releasables() {
				names = append(names, r.Name)
			}
			return fmt.Errorf("--releasable names %q, which no releasable is declared as; the declared releasables are %s. Drop --releasable to create a releasable named after the member", req.Releasable, joinOr(names, "none"))
		}
		if req.TagFormat != "" || req.PublishMode != "" {
			return fmt.Errorf("--tag-format and --publish-mode declare the releasable the absorb creates, and the member joins the declared releasable %q (tag format %s, publish mode %s): drop them, or change the releasable in %s", r.Name, r.TagFormat, r.PublishMode, declarations.ReleasablesFile)
		}
		if arr.sourceDecls != nil {
			src := arr.sourceDecls.Releasables[0]
			if !src.Hooks.IsEmpty() || len(src.DeployCommand) > 0 {
				return fmt.Errorf("the source repository's releasable %q declares hooks or a deploy command in %s, and a joined releasable keeps its own. Move them onto the root member's hooks there (or drop them), commit, and run this again; or drop --releasable to create a releasable that takes them", src.Name, declarations.ReleasablesFile)
			}
		}
		arr.creates = false
		return arr.setScheme(r)
	}
	arr.creates = true
	if r, declared := d.Releasable(arr.releasable); declared {
		ours := false
		if healing {
			if m, ok := d.MemberAt(req.Dest); ok && m.Name == arr.name && m.Releasable == arr.releasable {
				ours = true
			}
		}
		if !ours {
			return fmt.Errorf("a releasable named %q is declared already, and an absorb without --releasable creates one named after the member: pass --releasable %s to join it, or --name to name the member otherwise", arr.releasable, arr.releasable)
		}
		if req.TagFormat != "" && req.TagFormat != r.TagFormat {
			return fmt.Errorf("the releasable %q was created by the absorb this run completes, with the tag format %s, and --tag-format says %s; its tags are written under the declared format, so change %s by hand if it is wrong", r.Name, r.TagFormat, req.TagFormat, declarations.ReleasablesFile)
		}
		return arr.setScheme(r)
	}
	var missing []string
	if req.TagFormat == "" {
		missing = append(missing, "--tag-format (the releasable's tag scheme, for example \"{name}@v{version}\")")
	}
	mode := declarations.PublishMode(req.PublishMode)
	switch {
	case sourceMode != "" && mode != "" && mode != sourceMode:
		return fmt.Errorf("--publish-mode says %s, and the source repository declares the publish mode %s; drop --publish-mode to take the declared one, or change it in the source and commit first", mode, sourceMode)
	case sourceMode != "":
		mode = sourceMode
	case mode == "":
		missing = append(missing, "--publish-mode (ci publishes from CI, none publishes nothing), since the source repository declares none")
	}
	if len(missing) > 0 {
		return fmt.Errorf("the absorb creates the releasable %q, and a releasable's tag format and publish mode are stated, never derived: pass %s", arr.releasable, strings.Join(missing, " and "))
	}
	if problem := declarations.TagFormatProblem(req.TagFormat, arr.releasable); problem != "" {
		return fmt.Errorf("--tag-format: %s", problem)
	}
	created := declarations.Releasable{Name: arr.releasable, TagFormat: req.TagFormat, PublishMode: mode}
	if arr.sourceDecls != nil {
		src := arr.sourceDecls.Releasables[0]
		created.DeployCommand = src.DeployCommand
		created.Hooks = arriveHooks(src.Hooks, req.Dest)
	}
	arr.created = &created
	return arr.setScheme(created)
}

// setScheme records the scheme of the releasable the member is versioned
// under.
func (arr *arrival) setScheme(r declarations.Releasable) error {
	s, err := workspace.SchemeOf(r)
	if err != nil {
		return err
	}
	arr.scheme, arr.tagFormat = s, r.TagFormat
	return nil
}

// arriveHooks are a standalone releasable's hooks with each directory moved
// under the member's path: the repository root they ran from is the member
// now.
func arriveHooks(h declarations.Hooks, dest string) declarations.Hooks {
	move := func(list []declarations.Hook) []declarations.Hook {
		var out []declarations.Hook
		for _, hook := range list {
			if hook.Dir == "" || hook.Dir == declarations.RootPath {
				hook.Dir = dest
			} else {
				hook.Dir = declarations.Join(dest, hook.Dir)
			}
			out = append(out, hook)
		}
		return out
	}
	return declarations.Hooks{PreChecks: move(h.PreChecks), PreRelease: move(h.PreRelease), PostRelease: move(h.PostRelease)}
}

// observeVersion decides the version the member arrives at: the version
// file this absorb wrote on an earlier run that created the releasable, or
// the arriving manifests' version, or its highest version tag. A source
// answering none is refused: a version file is never invented.
func (arr *arrival) observeVersion(src git.Repo, tags map[string]string) error {
	root := arr.sourceRootMember()
	list, err := targets.MemberTargets(arr.req.Source, root)
	if err != nil {
		return fmt.Errorf("the source repository's targets cannot be read: %w", err)
	}
	for _, t := range list {
		arr.targetNames = append(arr.targetNames, t.Name)
		target, err := targets.Get(t.Name)
		if err != nil {
			return err
		}
		if f := target.Facts(); f.PublisherBindsToRepository {
			arr.publishers = append(arr.publishers, f)
		}
	}
	if arr.merge != "" && arr.creates {
		if text, found, err := releasableVersion(arr.ws, arr.releasable); err != nil {
			return err
		} else if found {
			arr.version = text
			return nil
		}
	}
	for _, t := range list {
		target, err := targets.Get(t.Name)
		if err != nil {
			return err
		}
		v, err := target.ReadVersion(filepath.Join(arr.req.Source, filepath.FromSlash(root.TargetDir(t))))
		if err == nil {
			arr.version = v.String()
			return nil
		}
	}
	var highest *semver.Version
	for name := range tags {
		v, ok := arr.tagVersion(name)
		if ok && (highest == nil || semver.Compare(v, *highest) > 0) {
			highest = &v
		}
	}
	if highest != nil {
		arr.version = highest.String()
		return nil
	}
	return fmt.Errorf("the version of %s cannot be decided: no manifest of it states one rlsbl reads, and it carries no version tag. A releasable's version file is what its releases bump from, so it is never invented: state the version in the source's manifest, or tag its current release, commit, and run this again", arr.req.Source)
}

// tagVersion is the version an arriving tag carries: under the source's
// declared scheme when it declares one, and in any version tag shape when
// it declares nothing.
func (arr *arrival) tagVersion(tag string) (semver.Version, bool) {
	if arr.sourceScheme != nil {
		return arr.sourceScheme.VersionOf(tag)
	}
	v, _, ok := workspace.ParseVersionTag(tag)
	return v, ok
}

// observeState lists the arriving records.
func (arr *arrival) observeState() error {
	if arr.sourceName == "" {
		return nil
	}
	source := arr.req.Source
	files, err := changelog.ReadAll(source, declarations.ChangelogDir(arr.sourceName))
	if err != nil {
		return fmt.Errorf("the arriving changelog cannot be read: %w", err)
	}
	for _, f := range files {
		if !f.Released {
			arr.unreleased = len(f.Lines)
			continue
		}
		arr.changelogs = append(arr.changelogs, f.Version.String())
	}
	versions, err := releaserecord.ArchivedVersions(source, declarations.ReleasesDir(arr.sourceName))
	if err != nil {
		return err
	}
	for _, v := range versions {
		if _, err := releaserecord.ReadArchive(source, declarations.ReleasesDir(arr.sourceName), v); err != nil {
			return fmt.Errorf("an arriving release archive cannot be read: %w", err)
		}
		arr.archives = append(arr.archives, v.String())
	}
	return nil
}

// refuseVersionOverlap refuses a version the releasable joined has released
// already and the arriving repository carries too: one version is one
// release.
func (arr *arrival) refuseVersionOverlap(tags map[string]string) error {
	present, err := releasedVersions(arr.ws.Root, arr.releasable)
	if err != nil {
		return err
	}
	incoming := map[string]bool{}
	for _, v := range arr.changelogs {
		incoming[v] = true
	}
	for _, v := range arr.archives {
		incoming[v] = true
	}
	for name := range tags {
		if v, ok := arr.tagVersion(name); ok {
			incoming[v.String()] = true
		}
	}
	var overlap []string
	for v := range incoming {
		if present[v] {
			overlap = append(overlap, v)
		}
	}
	if len(overlap) == 0 {
		return nil
	}
	sort.Strings(overlap)
	return fmt.Errorf("the releasable %q has released %s already, and %s carries the same versions; one version is one release, so the two records cannot both be that version's. Absorb into a releasable that has not released them, or reconcile the histories first", arr.releasable, strings.Join(overlap, ", "), arr.req.Source)
}

// observeTags plans the tags the absorb creates: each arriving version tag
// under the releasable's scheme, and the arriving tag of the current version
// beside it under its own name. A name the workspace holds already is
// refused, unless an earlier run of this absorb created it.
func (arr *arrival) observeTags(repo git.Repo, sourceTags map[string]string) error {
	present, err := repo.TagCommits()
	if err != nil {
		return err
	}
	healing := arr.merge != ""
	byVersion := map[string]string{}
	for _, tag := range sortedKeys(sourceTags) {
		v, ok := arr.tagVersion(tag)
		if !ok {
			arr.skipped = append(arr.skipped, tag)
			continue
		}
		if other, dup := byVersion[v.String()]; dup {
			return fmt.Errorf("the source repository's tags %s and %s both carry the version %s, and the releasable's scheme gives one version one tag; delete the one that did not ship that version in the source, then run this again", other, tag, v)
		}
		byVersion[v.String()] = tag
		next := arr.scheme.Render(v)
		if _, exists := present[next]; exists {
			if !healing {
				return fmt.Errorf("the source's tag %s arrives as %s, and a tag %s exists in this repository already; a tag here is never moved or deleted by an absorb, so resolve it before absorbing", tag, next, next)
			}
			arr.already = append(arr.already, next)
		}
		arr.imports = append(arr.imports, tagImport{old: tag, next: next, version: v.String(), commit: sourceTags[tag]})
	}
	for i := range arr.imports {
		imp := arr.imports[i]
		if imp.version != arr.version || imp.old == imp.next {
			continue
		}
		if _, exists := present[imp.old]; exists {
			if !healing {
				return fmt.Errorf("the source's own tag %s is kept beside %s so its name still resolves, and a tag %s exists in this repository already; resolve it before absorbing", imp.old, imp.next, imp.old)
			}
			arr.already = append(arr.already, imp.old)
		}
		arr.alias = &arr.imports[i]
		break
	}
	return nil
}

// observeDeclarations builds the workspace's declarations with the member
// and, when the absorb creates it, its releasable, and checks them.
func (arr *arrival) observeDeclarations() error {
	if arr.present {
		return nil
	}
	created := arr.created
	m := arr.sourceRootMember()
	m.Path, m.Name, m.Releasable, m.DependsOn = arr.req.Dest, arr.name, arr.releasable, nil
	if arr.req.RegistryName != "" {
		m.RegistryName = arr.req.RegistryName
	}
	data, err := os.ReadFile(filepath.Join(arr.ws.Root, filepath.FromSlash(declarations.ReleasablesFile)))
	if err != nil {
		return fmt.Errorf("reading %s: %w", declarations.ReleasablesFile, err)
	}
	ed, err := declarations.NewEditor(data)
	if err != nil {
		return err
	}
	if created != nil {
		if err := ed.AddReleasable(*created); err != nil {
			return err
		}
	}
	if err := ed.AddMember(m); err != nil {
		return err
	}
	edited, parsed, err := ed.Result()
	if err != nil {
		return fmt.Errorf("the workspace's declarations with the absorbed member would be refused: %w", err)
	}
	arr.edited, arr.editedDecls = edited, parsed
	if len(m.InternalDepFloors) > 0 {
		arr.floorSwitches = []depFloorsSwitch{{member: m.Name, path: m.Path, scope: m.Path}}
	}
	return nil
}

// declaredAfter are the subjects the workspace declares once the member
// is in.
func (arr *arrival) declaredAfter() []string {
	if arr.editedDecls != nil {
		return declaredSubjectsOf(arr.editedDecls)
	}
	return declaredSubjectsOf(arr.ws.Declarations)
}

// observeRecords merges the arriving lifecycle-and-license entries into the
// workspace's record under the member's name, records the arriving
// repository as the member's closed repository URL, and picks the arriving
// transition record's events, each checked before anything is written.
func (arr *arrival) observeRecords(src git.Repo) error {
	ws, on := arr.ws, arr.req.Now
	arriving, err := lifecycle.Load(arr.req.Source)
	if err != nil {
		return fmt.Errorf("the source repository's lifecycle-and-license record: %w", err)
	}
	if len(arriving.Codenames()) > 0 || len(arriving.DistinctiveTerms()) > 0 {
		return fmt.Errorf("the source repository's %s holds codenames or distinctive terms, which have no place the absorb can carry them to. Remove them from the source's record and commit, absorb, then add them to this repository's record by hand", lifecycle.RecordFile)
	}
	rename := map[string]string{declarations.RootName: arr.name}
	if arr.sourceName != "" {
		rename[arr.sourceName] = arr.name
	}
	rec, err := lifecycle.Load(ws.Root)
	if err != nil {
		return err
	}
	if err := addEntries(rec, entriesOf(arriving, rename)); err != nil {
		return fmt.Errorf("the arriving lifecycle-and-license entries cannot join this repository's record: %w", err)
	}
	reason := fmt.Sprintf("absorbed from %s with `rlsbl monorepo absorb`", arr.sourceURL)
	if arr.sourceScheme != nil {
		if err := moveTagNamespace(rec, arr.name, arr.sourceScheme.ListGlob(), arr.scheme.ListGlob(), on, reason); err != nil {
			return err
		}
	}
	started, err := src.CommitterDate(arr.sourceRoot)
	if err != nil {
		return err
	}
	if err := closeRepositoryURL(rec, arr.name, arr.sourceURL, started, on, reason); err != nil {
		return err
	}
	if arr.creates {
		r, _ := arr.ws.Declarations.Releasable(arr.releasable)
		if arr.created != nil {
			r = *arr.created
		}
		if err := recordCreatedReleasable(rec, r, arr.req.License, on, reason); err != nil {
			return err
		}
	}
	if err := rec.Validate(on, arr.declaredAfter()); err != nil {
		return fmt.Errorf("this repository's lifecycle-and-license record with the arriving entries would be refused: %w", err)
	}
	arr.record = rec
	if arr.sourceDecls == nil {
		return nil
	}
	arrivingEvents, err := releaserecord.ReadEvents(arr.req.Source)
	if err != nil {
		return fmt.Errorf("the source repository's transition record: %w", err)
	}
	present, err := releaserecord.ReadEvents(ws.Root)
	if err != nil {
		return err
	}
	arr.events = scopedTo(arrivingEvents, arr.releasable, eventIDs(present))
	return nil
}

// preview is the plan, one item per apply step.
func (arr *arrival) preview() (previewapply.Preview, error) {
	req := arr.req
	items := []previewapply.Item{{
		Key:     absorbItemSource,
		State:   "rewrite_history",
		Summary: fmt.Sprintf("%s (version %s) becomes the member %q at %s.", req.Source, arr.version, arr.name, req.Dest),
		Facts: []string{
			fmt.Sprintf("source: %s (root commit %s)", req.Source, short(arr.sourceRoot)),
			"targets: " + joinOr(arr.targetNames, "none detected"),
		},
		Actions: []string{"apply would clone the source inside this repository's git directory and run git-filter-repo --to-subdirectory-filter " + req.Dest},
	}}
	rel := previewapply.Item{Key: absorbItemReleasable}
	if arr.creates {
		rel.State, rel.Summary = "create_releasable", fmt.Sprintf("the releasable %q is created for the member.", arr.releasable)
		rel.Facts = []string{"tag format: " + arr.scheme.Pattern(), "version file: " + arr.version}
	} else {
		rel.State, rel.Summary = "join_releasable", fmt.Sprintf("the member joins the releasable %q.", arr.releasable)
		rel.Facts = []string{"tag format: " + arr.scheme.Pattern(), "version: the releasable's own, untouched"}
	}
	items = append(items, rel)
	history := previewapply.Item{Key: absorbItemHistory, Facts: []string{
		absorbMarker(arr.name, req.Dest),
		AbsorbSourceTrailer + ": " + arr.sourceRoot,
		AbsorbReleasableTrailer + ": " + arr.releasable,
	}}
	if arr.merge == "" {
		history.State, history.Summary = "merge_history", "the rewritten history is fetched without tags and merged."
	} else {
		history.State, history.Summary = "history_already_merged", fmt.Sprintf("the commit %s merged this source already; this run completes what follows it.", short(arr.merge))
	}
	items = append(items, history)
	var tagFacts []string
	for _, imp := range arr.imports {
		tagFacts = append(tagFacts, imp.old+" -> "+imp.next)
	}
	if arr.alias != nil {
		tagFacts = append(tagFacts, fmt.Sprintf("boundary alias: %s is created beside %s at version %s", arr.alias.old, arr.alias.next, arr.alias.version))
	}
	if len(arr.skipped) > 0 {
		tagFacts = append(tagFacts, "not version tags of the source, not imported: "+strings.Join(arr.skipped, ", "))
	}
	if len(arr.already) > 0 {
		tagFacts = append(tagFacts, "already created by an earlier run: "+strings.Join(arr.already, ", "))
	}
	tags := previewapply.Item{Key: absorbItemTags, Facts: tagFacts, Actions: []string{"apply would create each tag at the rewritten commit; the fetch carries no tag, so no tag here is moved or deleted."}}
	if len(arr.imports) > 0 {
		tags.State, tags.Summary = "import_tags", fmt.Sprintf("%d version tags are created under %s.", len(arr.imports), arr.scheme.Pattern())
	} else {
		tags.State, tags.Summary = "no_tags_to_import", "the source carries no version tag, so none is created."
	}
	items = append(items, tags)
	deleter := "saferm"
	if req.DeleteWithRm {
		deleter = "a plain removal (--delete-with-rm)"
	}
	stateFacts := []string{
		fmt.Sprintf("unreleased entries: %d", arr.unreleased),
		"released changelogs: " + joinOr(arr.changelogs, "none"),
		"release archives: " + joinOr(arr.archives, "none"),
		fmt.Sprintf("%s: the arriving entries under the name %q, and the source as the member's closed repository-url identity", lifecycle.RecordFile, arr.name),
	}
	if len(arr.events) > 0 {
		stateFacts = append(stateFacts, fmt.Sprintf("%s: the %d events of the source's transition record, scoped to %q", declarations.TransitionsFile, len(arr.events), arr.releasable))
	}
	items = append(items, previewapply.Item{
		Key:     absorbItemState,
		State:   "migrate_state",
		Summary: fmt.Sprintf("the arriving records move into %s and %s.", declarations.ChangelogDir(arr.releasable), declarations.ReleasesDir(arr.releasable)),
		Facts:   stateFacts,
		Actions: []string{
			"apply would map every changelog commit and every release commit through git-filter-repo's commit map, check each recorded tree against the rewritten history, and name what it could not map.",
			fmt.Sprintf("apply would then delete the arriving changelog and release directories and CHANGELOG.md under %s through %s; the rest of its %s is residue `rlsbl monorepo cleanup` removes.", req.Dest, deleter, declarations.MetadataDir),
		},
	})
	ws := previewapply.Item{Key: absorbItemWorkspace}
	if arr.present {
		ws.State, ws.Summary = "member_present", fmt.Sprintf("%s declares %q at %s already; the declaration is left as it is.", declarations.ReleasablesFile, arr.name, req.Dest)
	} else {
		ws.State, ws.Summary = "register_member", fmt.Sprintf("%s gains the member %q at %s, versioned under %q.", declarations.ReleasablesFile, arr.name, req.Dest, arr.releasable)
	}
	ws.Actions = []string{"apply would commit the absorb's records and declarations (" + AbsorbCommitMessage(arr.name) + "), then scaffold the member, which regenerates the routers, and commit that."}
	items = append(items, ws)
	events := []string{"conversion (direction absorb)"}
	if len(arr.imports) > 0 {
		events = append(events, "tag-map")
	}
	if len(arr.archives) > 0 {
		events = append(events, "release-commit-remap (when a release commit moved)")
	}
	if arr.alias != nil {
		events = append(events, "boundary-alias")
	}
	items = append(items, previewapply.Item{
		Key:     absorbItemRecord,
		State:   "record_transition_record",
		Summary: "the transition record explains the absorb: " + strings.Join(events, ", ") + ".",
	})
	items = append(items, previewapply.Item{
		Key:     absorbItemNextSteps,
		State:   "operator_actions",
		Summary: "rlsbl administers no external system; these are yours.",
		Facts:   arr.nextSteps(),
	})
	return previewapply.NewPreview(items...)
}

// nextSteps are what rlsbl leaves to the operator.
func (arr *arrival) nextSteps() []string {
	steps := []string{
		fmt.Sprintf("review %s against this workspace's conventions (the absorbed repository was scaffolded as a project of its own)", arr.req.Dest),
		fmt.Sprintf("review the routers regenerated in %s before the next release", arr.ws.Root),
		fmt.Sprintf("the source repository %s is untouched: archive it once what arrived is right (old-repo-archived asks for it)", arr.req.Source),
		fmt.Sprintf("run `rlsbl monorepo cleanup` to remove the arriving repository's own records under %s", arr.req.Dest),
	}
	for _, f := range arr.publishers {
		steps = append(steps, fmt.Sprintf("%s authorizes publishing for a repository, not for the package, so it did not follow the code: register this repository at %s before the next release (a publish that fails for want of it is recovered with `rlsbl release retry --watch`, not a new version)", f.RegistryDisplayName, f.PublisherSetupURL))
	}
	return steps
}

// apply performs one item's step.
func (arr *arrival) apply(e *strictcli.Effects, it previewapply.Item, run *absorbRun) error {
	switch it.Key {
	case absorbItemSource:
		return arr.applySource(e, run)
	case absorbItemReleasable:
		return nil
	case absorbItemHistory:
		return arr.applyHistory(e, run)
	case absorbItemTags:
		return arr.applyTags(e, run)
	case absorbItemState:
		return arr.applyState(e, run)
	case absorbItemWorkspace:
		return arr.applyWorkspace(e, run)
	case absorbItemRecord:
		return arr.applyRecord(e, run)
	case absorbItemNextSteps:
		return arr.applyNextSteps(e, run)
	}
	return fmt.Errorf("no step applies the item %q", it.Key)
}

// applySource takes the lock, clones the source inside this repository's
// git directory, and rewrites the clone under the member's path.
func (arr *arrival) applySource(e *strictcli.Effects, run *absorbRun) error {
	root := arr.ws.Root
	lock, err := runstate.Acquire(e, root, runstate.AcquireOptions{Wait: runstate.RefuseWhenHeld})
	if err != nil {
		return err
	}
	run.lock = lock
	repo, err := git.Open(e, root)
	if err != nil {
		return err
	}
	if dirty, err := dirtyPaths(repo); err != nil {
		return err
	} else if len(dirty) > 0 {
		return fmt.Errorf("the working tree changed after the plan was made (%s); nothing was written. Commit or remove the changes and run this again", strings.Join(dirty, ", "))
	}
	src, err := git.Open(e, arr.req.Source)
	if err != nil {
		return err
	}
	if changed, err := src.ChangedPaths(nil, git.UntrackedNormal); err != nil {
		return err
	} else if len(changed) > 0 {
		return fmt.Errorf("the source repository changed after the plan was made (%s); nothing was written. Commit there and run this again", strings.Join(changed, ", "))
	}
	common, err := repo.CommonDir()
	if err != nil {
		return err
	}
	clone := filepath.Join(common, "rlsbl", "absorb-"+arr.name)
	// A leftover of an interrupted run is rlsbl's own scratch, replaced.
	if _, err := e.Remove(clone); err != nil {
		return err
	}
	if _, err := e.Mkdir(filepath.Dir(clone)); err != nil {
		return err
	}
	run.clone = clone
	arr.req.Say(fmt.Sprintf("Cloning %s to %s ...", arr.req.Source, clone))
	if err := cloneForRewrite(e, arr.req.Source, clone); err != nil {
		return err
	}
	if err := runFilterRepo(e, clone, "--to-subdirectory-filter", arr.req.Dest, "--force"); err != nil {
		return err
	}
	var pruned []string
	if run.commitMap, pruned, err = readCommitMap(clone); err != nil {
		return err
	}
	arr.req.Say(fmt.Sprintf("  git-filter-repo mapped %d commits; %d pruned.", len(run.commitMap), len(pruned)))
	return nil
}

// AbsorbMergeMessage is the message of the absorb's merge commit, carrying
// the trailers a run completing it finds it by.
func AbsorbMergeMessage(name, dest, sourceRoot, releasable string) string {
	return fmt.Sprintf("monorepo: absorb the history of %s\n\n%s: true\n%s\n%s: %s\n%s: %s\n",
		name, git.AutogeneratedTrailer, absorbMarker(name, dest), AbsorbSourceTrailer, sourceRoot, AbsorbReleasableTrailer, releasable)
}

// applyHistory fetches the rewritten history without its tags and merges
// it, unless an earlier run merged it.
func (arr *arrival) applyHistory(e *strictcli.Effects, run *absorbRun) error {
	if arr.merge != "" {
		arr.req.Say(fmt.Sprintf("  history: merged by %s already.", short(arr.merge)))
		return nil
	}
	root := arr.ws.Root
	// No tag is fetched: every tag here afterwards was created by the absorb
	// at a commit it mapped, so none the workspace owns can be overwritten.
	if err := runGit(e, root, "fetch", "--quiet", "--no-tags", run.clone); err != nil {
		return err
	}
	if err := runGit(e, root, "merge", "--quiet", "--allow-unrelated-histories", "-m", AbsorbMergeMessage(arr.name, arr.req.Dest, arr.sourceRoot, arr.releasable), "FETCH_HEAD"); err != nil {
		return err
	}
	repo, err := git.Open(e, root)
	if err != nil {
		return err
	}
	if arr.merge, err = repo.Head(); err != nil {
		return err
	}
	arr.req.Say(fmt.Sprintf("  history: merged as %s.", short(arr.merge)))
	return nil
}

// applyTags creates the tags at the rewritten commits.
func (arr *arrival) applyTags(e *strictcli.Effects, run *absorbRun) error {
	repo, err := git.Open(e, arr.ws.Root)
	if err != nil {
		return err
	}
	create := func(tag, commit, what string) error {
		at, found, err := repo.TagCommit(tag)
		if err != nil {
			return err
		}
		if found {
			if at == commit {
				return nil
			}
			return fmt.Errorf("the tag %s exists at %s, and the absorb's %s puts it at %s; a tag the absorb did not create is never moved or deleted, so resolve it by hand and run this again", tag, short(at), what, short(commit))
		}
		return repo.CreateRef("refs/tags/"+tag, commit)
	}
	for _, imp := range arr.imports {
		next := run.commitMap[imp.commit]
		ok := false
		if next != "" {
			if ok, err = repo.HasObject(next); err != nil {
				return err
			}
		}
		if !ok {
			run.notes = append(run.notes, fmt.Sprintf("the tag %s names a commit the rewrite did not carry, so %s was not created", imp.old, imp.next))
			continue
		}
		if err := create(imp.next, next, "import of "+imp.old); err != nil {
			return err
		}
		run.tagMaps = append(run.tagMaps, releaserecord.TagMapping{OldTag: imp.old, NewTag: imp.next, OldCommit: imp.commit, NewCommit: next})
		if arr.alias != nil && imp.version == arr.alias.version {
			if err := create(arr.alias.old, next, "boundary alias of "+imp.next); err != nil {
				return err
			}
			run.aliasAt = next
		}
	}
	arr.req.Say(fmt.Sprintf("  tags: %d created under %s.", len(run.tagMaps), arr.scheme.Pattern()))
	return nil
}

// mapper answers an arriving commit id's commit in this repository, or ""
// when the rewrite did not carry it.
func (arr *arrival) mapper(repo git.Repo, commitMap map[string]string) func(string) (string, error) {
	return func(h string) (string, error) {
		next, _ := changelog.MapCommit(h, commitMap)
		if next == "" {
			return "", nil
		}
		ok, err := repo.HasObject(next)
		if err != nil || !ok {
			return "", err
		}
		return next, nil
	}
}

// applyState moves the arriving records into the releasable's directories,
// the version file and the lifecycle-and-license record included, and
// deletes the arriving copies.
func (arr *arrival) applyState(e *strictcli.Effects, run *absorbRun) error {
	root := arr.ws.Root
	repo, err := git.Open(e, root)
	if err != nil {
		return err
	}
	if arr.sourceName != "" {
		if err := arr.migrateChangelog(e, repo, run); err != nil {
			return err
		}
		if err := arr.migrateArchives(e, repo, run); err != nil {
			return err
		}
	}
	if arr.creates {
		if err := arr.writeVersion(e); err != nil {
			return err
		}
	}
	if err := arr.record.Write(recordWriter{e}, root); err != nil {
		return err
	}
	if err := releaserecord.AppendEvents(e, root, arr.events, arr.req.Now); err != nil {
		return err
	}
	if arr.sourceName != "" {
		description := fmt.Sprintf("the records of %q, moved into the releasable %q by `rlsbl monorepo absorb`", arr.name, arr.releasable)
		for _, p := range []string{
			declarations.Join(arr.req.Dest, declarations.ChangelogDir(arr.sourceName)),
			declarations.Join(arr.req.Dest, declarations.ReleasesDir(arr.sourceName)),
			declarations.Join(arr.req.Dest, changelog.MarkdownName),
		} {
			if err := deletePath(e, root, p, description, arr.req.DeleteWithRm); err != nil {
				return err
			}
		}
	}
	arr.req.Say(fmt.Sprintf("  state: %d unreleased entries migrated, %d already present; %d release commits moved.", run.migrated, run.present, len(run.remaps)))
	return nil
}

// migrateChangelog appends the arriving unreleased entries this repository
// does not hold yet (by id) and writes each arriving released version's
// file, refusing one that exists here with other entries.
func (arr *arrival) migrateChangelog(e *strictcli.Effects, repo git.Repo, run *absorbRun) error {
	root := arr.ws.Root
	member := filepath.Join(root, filepath.FromSlash(arr.req.Dest))
	files, err := changelog.ReadAll(member, declarations.ChangelogDir(arr.sourceName))
	if err != nil {
		return err
	}
	target := declarations.ChangelogDir(arr.releasable)
	mapCommit := arr.mapper(repo, run.commitMap)
	for _, f := range files {
		kept, dropped, narrowed, err := carryEntries(f.Entries(), mapCommit)
		if err != nil {
			return err
		}
		if dropped > 0 || narrowed > 0 {
			run.notes = append(run.notes, fmt.Sprintf("the arriving changelog of %s: %d entries dropped and %d narrowed, their commits not carried by the rewrite", f.Label(), dropped, narrowed))
		}
		if !f.Released {
			here, err := changelog.ReadUnreleased(root, target)
			if err != nil {
				return err
			}
			ids := map[string]bool{}
			for _, entry := range here.Entries() {
				ids[entry.ID] = true
			}
			for _, entry := range kept {
				if ids[entry.ID] {
					run.present++
					continue
				}
				if err := changelog.AppendEntry(e, root, target, entry); err != nil {
					return err
				}
				run.migrated++
			}
			continue
		}
		existing, err := changelog.ReadVersion(root, target, f.Version)
		if err == nil {
			if !sameEntries(existing.Entries(), kept) {
				return fmt.Errorf("the releasable %q has a changelog of %s already (%s), and the arriving one differs; a released changelog is a record, so neither replaces the other: resolve the overlap, then run this again", arr.releasable, f.Version, existing.Path)
			}
			continue
		}
		if exists, statErr := pathExists(root, path.Join(target, changelog.VersionName(f.Version))); statErr != nil {
			return statErr
		} else if exists {
			return err
		}
		if err := changelog.WriteVersionFile(e, root, target, f.Version, kept); err != nil {
			return err
		}
	}
	return nil
}

// sameEntries reports whether two lists hold the same entries in the same
// order.
func sameEntries(a, b []changelog.Entry) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if changelog.Serialize(a[i]) != changelog.Serialize(b[i]) {
			return false
		}
	}
	return true
}

// migrateArchives copies each arriving archive into the releasable's
// release directory with its release commit moved through the commit map
// and every recorded tree checked at the new commit. An archive the
// releasable holds already must say what the moved one says.
func (arr *arrival) migrateArchives(e *strictcli.Effects, repo git.Repo, run *absorbRun) error {
	root := arr.ws.Root
	arriving := declarations.Join(arr.req.Dest, declarations.ReleasesDir(arr.sourceName))
	plan, err := planArchiveRemap(repo, arriving, run.commitMap, func(p string) string {
		return relativeTo(p, declarations.RootPath, arr.req.Dest)
	})
	if err != nil {
		return err
	}
	moved := map[string]archiveMove{}
	for _, m := range plan.moves {
		moved[m.version.String()] = m
	}
	for _, left := range plan.left {
		run.notes = append(run.notes, "release commit left as recorded: "+left)
	}
	target := declarations.ReleasesDir(arr.releasable)
	versions, err := releaserecord.ArchivedVersions(root, arriving)
	if err != nil {
		return err
	}
	if err := declarations.EnsureOwnedDirectory(e, root, declarations.ReleasesRoot); err != nil {
		return err
	}
	for _, v := range versions {
		a, err := releaserecord.ReadArchive(root, arriving, v)
		if err != nil {
			return err
		}
		want := a.ReleaseCommit
		if m, ok := moved[v.String()]; ok {
			want = releaserecord.ReleaseCommit{Commit: m.next, Trees: m.trees}
		}
		rel := releaserecord.ArchivePath(target, v)
		if exists, err := pathExists(root, rel); err != nil {
			return err
		} else if exists {
			held, err := releaserecord.ReadArchive(root, target, v)
			if err != nil {
				return err
			}
			if !sameArchive(held, a, want) {
				return fmt.Errorf("the releasable %q has an archive of %s already (%s), and the arriving one says otherwise; an archive is the record of what shipped, so neither replaces the other: resolve the overlap, then run this again", arr.releasable, v, rel)
			}
			continue
		}
		if err := copyFile(e, filepath.Join(root, filepath.FromSlash(a.Path)), filepath.Join(root, filepath.FromSlash(rel))); err != nil {
			return err
		}
		if m, ok := moved[v.String()]; ok {
			if err := releaserecord.WriteReleaseCommit(e, root, target, v, want); err != nil {
				return err
			}
			run.remaps = append(run.remaps, releaserecord.CommitMapping{OldSHA: m.old, NewSHA: m.next})
		}
	}
	return nil
}

// sameArchive reports whether the archive held here says what the arriving
// one says, its release commit as the move gives it.
func sameArchive(held, arriving releaserecord.Archive, want releaserecord.ReleaseCommit) bool {
	if held.Fate != arriving.Fate || held.ShippedAs != arriving.ShippedAs || held.Bump != arriving.Bump ||
		held.Description != arriving.Description || held.Context != arriving.Context ||
		strings.Join(held.Include, "\x00") != strings.Join(arriving.Include, "\x00") ||
		strings.Join(held.Exclude, "\x00") != strings.Join(arriving.Exclude, "\x00") ||
		strings.Join(held.ReleaseNotices, "\x00") != strings.Join(arriving.ReleaseNotices, "\x00") {
		return false
	}
	if held.ReleaseCommit.Commit != want.Commit || len(held.ReleaseCommit.Trees) != len(want.Trees) {
		return false
	}
	for p, tree := range want.Trees {
		if held.ReleaseCommit.Trees[p] != tree {
			return false
		}
	}
	return true
}

// writeVersion writes the created releasable's version file, unless an
// earlier run wrote it.
func (arr *arrival) writeVersion(e *strictcli.Effects) error {
	root := arr.ws.Root
	rel := declarations.VersionFile(arr.releasable)
	if exists, err := pathExists(root, rel); err != nil || exists {
		return err
	}
	if err := declarations.EnsureOwnedDirectory(e, root, declarations.ReleasesRoot); err != nil {
		return err
	}
	abs := filepath.Join(root, filepath.FromSlash(rel))
	if _, err := e.Mkdir(filepath.Dir(abs)); err != nil {
		return err
	}
	if _, err := e.Write(abs, arr.version+"\n"); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	return nil
}

// applyWorkspace declares the member, commits the absorb's records and
// declarations, scaffolds the member (which regenerates the routers), and
// commits what the scaffold wrote.
func (arr *arrival) applyWorkspace(e *strictcli.Effects, run *absorbRun) error {
	root := arr.ws.Root
	d := arr.ws.Declarations
	if arr.edited != nil {
		var err error
		if d, err = declarations.Write(e, root, arr.edited); err != nil {
			return err
		}
		if err := switchOnDepFloors(e, root, d, arr.floorSwitches, arr.req.Say); err != nil {
			return err
		}
	}
	if _, err := changelog.Regenerate(e, root, d, arr.releasable, nil); err != nil {
		return err
	}
	repo, err := git.Open(e, root)
	if err != nil {
		return err
	}
	if err := commitAll(repo, AbsorbCommitMessage(arr.name), false); err != nil {
		return err
	}
	if run.commit, err = repo.Head(); err != nil {
		return err
	}
	say := func(line string) {
		if line != scaffoldNotCommitted {
			arr.req.Say(line)
		}
	}
	if err := scaffold.Run(e, scaffold.Inputs{
		Dir:     filepath.Join(root, filepath.FromSlash(arr.req.Dest)),
		Version: arr.req.Version,
		Now:     arr.req.Now,
		Say:     say,
		GitHub:  arr.req.GitHub,
	}); err != nil {
		return fmt.Errorf("scaffolding the member failed: %w. The history, the tags, and the records are in this repository already: fix what the scaffold reports and run this absorb again, which completes what is left", err)
	}
	if err := commitAll(repo, "monorepo: scaffold "+arr.name+" as a member", true); err != nil {
		return err
	}
	back, err := workspace.Load(root)
	if err != nil {
		return fmt.Errorf("the workspace's declarations do not load after the absorb: %w", err)
	}
	if _, ok := back.Declarations.Member(arr.name); !ok {
		return fmt.Errorf("the workspace does not declare the member %q after the absorb", arr.name)
	}
	if _, ok := back.Declarations.Releasable(arr.releasable); !ok {
		return fmt.Errorf("the workspace does not declare the releasable %q after the absorb", arr.releasable)
	}
	return nil
}

// commitAll commits every change of the working tree, and refuses one left
// over. The tree was clean when the absorb started, so every change is the
// step's own.
func commitAll(repo git.Repo, message string, autogenerated bool) error {
	paths, err := dirtyPaths(repo)
	if err != nil {
		return err
	}
	if len(paths) == 0 {
		return nil
	}
	if _, err := repo.Commit(git.CommitRequest{Message: message, Paths: paths, Autogenerated: autogenerated}); err != nil {
		return err
	}
	if left, err := dirtyPaths(repo); err != nil {
		return err
	} else if len(left) > 0 {
		return fmt.Errorf("the working tree has uncommitted changes after %q, which named every file the step wrote: %s", message, strings.Join(left, ", "))
	}
	return nil
}

// applyRecord writes the transition record's events of the absorb, unless
// an earlier run recorded its conversion.
func (arr *arrival) applyRecord(e *strictcli.Effects, run *absorbRun) error {
	root := arr.ws.Root
	events, err := releaserecord.ReadEvents(root)
	if err != nil {
		return err
	}
	for _, ev := range events {
		if c, ok := ev.(*releaserecord.ConversionEvent); ok && c.Direction == "absorb" && c.Destination.Path == arr.req.Dest && c.Source.Repo == arr.sourceURL {
			arr.req.Say("  transition record: the absorb is recorded already.")
			return nil
		}
	}
	commit := run.commit
	if commit == "" {
		commit = arr.merge
	}
	var sourceFormat string
	if arr.sourceScheme != nil {
		sourceFormat = arr.sourceDecls.Releasables[0].TagFormat
	}
	conversion := &releaserecord.ConversionEvent{
		Direction:   "absorb",
		Source:      releaserecord.Endpoint{Repo: arr.sourceURL, Project: arr.name, Releasable: arr.sourceName, TagFormat: sourceFormat},
		Destination: releaserecord.Endpoint{Repo: ".", Path: arr.req.Dest, Project: arr.name, Releasable: arr.releasable, TagFormat: arr.tagFormat},
		Commit:      commit,
	}
	if err := releaserecord.AppendEvents(e, root, []releaserecord.Event{conversion}, arr.req.Now); err != nil {
		return err
	}
	related := releaserecord.EventHeader{RelatedTo: conversion.ID}
	var followers []releaserecord.Event
	if len(run.tagMaps) > 0 {
		followers = append(followers, &releaserecord.TagMapEvent{EventHeader: related, Releasable: arr.releasable, Mappings: run.tagMaps})
	}
	if len(run.remaps) > 0 {
		followers = append(followers, &releaserecord.ReleaseCommitRemapEvent{EventHeader: related, Releasable: arr.releasable, Rewrite: "git-filter-repo --to-subdirectory-filter (monorepo absorb)", Mappings: run.remaps})
	}
	if arr.alias != nil && run.aliasAt != "" {
		followers = append(followers, &releaserecord.BoundaryAliasEvent{EventHeader: related, Releasable: arr.releasable, Aliases: []releaserecord.BoundaryAlias{{AliasTag: arr.alias.next, AliasedTag: arr.alias.old, Commit: run.aliasAt}}})
	}
	if err := releaserecord.AppendEvents(e, root, followers, arr.req.Now); err != nil {
		return err
	}
	repo, err := git.Open(e, root)
	if err != nil {
		return err
	}
	if _, err := repo.Commit(git.CommitRequest{Message: "monorepo: record the absorb of " + arr.name, Paths: []string{path.Dir(declarations.TransitionsFile)}, Autogenerated: true, RequireChange: true}); err != nil {
		return err
	}
	arr.req.Say(fmt.Sprintf("  transition record: %d events recorded.", len(followers)+1))
	return nil
}

// applyNextSteps refuses changes left uncommitted, and prints what is left
// to the operator.
func (arr *arrival) applyNextSteps(e *strictcli.Effects, run *absorbRun) error {
	repo, err := git.Open(e, arr.ws.Root)
	if err != nil {
		return err
	}
	if left, err := dirtyPaths(repo); err != nil {
		return err
	} else if len(left) > 0 {
		return fmt.Errorf("the working tree has uncommitted changes after the absorb: %s. The history, the tags, and the records are in place; commit or revert these by hand", strings.Join(left, ", "))
	}
	for _, note := range run.notes {
		arr.req.Say("Note: " + note)
	}
	var created []string
	for _, m := range run.tagMaps {
		created = append(created, m.NewTag)
	}
	arr.req.Say(fmt.Sprintf("Absorbed %s into %s as the member %q.", arr.req.Source, arr.req.Dest, arr.name))
	arr.req.Say("  Tags: " + joinOr(created, "none created"))
	arr.req.Say("Next steps (rlsbl administers no external system):")
	for _, step := range arr.nextSteps() {
		arr.req.Say("  - " + step)
	}
	return nil
}
