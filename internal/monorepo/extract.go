package monorepo

import (
	"errors"
	"fmt"
	"io/fs"
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
	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// `monorepo extract` moves a releasable out of a workspace into a
// repository of its own. Observation answers every question the apply acts
// on and makes every refusal, so --dry-run refuses what an apply would and
// neither has written anything when it does. The apply then performs the
// plan item by item, in the order the preview lists them:
//
//   - the members' history is rewritten by git-filter-repo on a fresh clone
//     (one releasable member is hoisted to the repository root);
//   - each member's tree must be the tree that left;
//   - the releasable's changelog and release records move, every commit id
//     and release commit mapped through git-filter-repo's commit map;
//   - its tags take the destination's scheme, the current version keeping
//     its old name beside the new one;
//   - the destination gets its declarations, its lifecycle-and-license
//     record (every entry of the departing subjects), and a transition
//     record explaining the conversion;
//   - the source loses the members, the releasable, and its records in one
//     commit, records the departure of its tag namespace, closes the
//     departing subjects in its lifecycle-and-license record, declares the
//     departing package names as dependency floors of the members that
//     stay, and regenerates its routers.
//
// Nothing is pushed, no remote is created, and no external system is
// touched: those are printed as next steps.

// The preview's items, in apply order.
const (
	extractItemReleasable = "releasable"
	extractItemDeps       = "dependencies"
	extractItemTrees      = "trees"
	extractItemState      = "state"
	extractItemTags       = "tags"
	extractItemDest       = "destination"
	extractItemRecord     = "transition-record"
	extractItemSource     = "source"
	extractItemNextSteps  = "next-steps"
)

// standaloneTagFormat is the tag format of a releasable extracted alone: a
// repository of its own tags bare versions.
const standaloneTagFormat = "v{version}"

// ExtractCommitMessage is the message of the source's commit.
func ExtractCommitMessage(releasable string) string {
	return "monorepo: extract releasable " + releasable
}

// ExtractRequest is one `monorepo extract`.
type ExtractRequest struct {
	Releasable string
	// Target is the absolute path the new repository is created at; it must
	// not exist.
	Target string
	// DeleteWithRm deletes the departing directories with a plain removal
	// instead of saferm.
	DeleteWithRm bool
	// Now is the date the lifecycle-and-license record changes on and the
	// transition record's events are stamped with.
	Now time.Time
	// Sync regenerates the source's routers from its declarations without
	// the releasable, without committing.
	Sync func(ws *workspace.Workspace) (workflows.SyncResult, error)
	Say  func(string)
}

// departure is everything observation resolved about one extraction.
type departure struct {
	req        ExtractRequest
	ws         *workspace.Workspace
	releasable declarations.Releasable
	members    []declarations.Member
	multi      bool
	ownScheme  workspace.TagScheme
	destScheme workspace.TagScheme
	// version is the releasable's version file, empty when it has none.
	version     string
	tags        tagTranslation
	sourceTrees map[string]string
	outbound    []string
	sourceURL   string
	publishers  []targets.Facts
	// The destination's documents, checked during observation.
	destDecls  *declarations.Releasables
	destRecord *lifecycle.Record
	carried    []releaserecord.Event
	// The source's edit: its declarations without the releasable, its
	// record with the departing subjects closed (nil when it keeps no
	// record), and the members whose dependency floors gain the departing
	// names.
	sourceDecls   []byte
	sourceEdited  *declarations.Releasables
	sourceRecord  *lifecycle.Record
	floorMembers  []string
	floorSwitches []depFloorsSwitch
	departing     []string
	changelogs    []string
	releaseFiles  []string
}

// tagTranslation is what the conversion does to the destination's tags.
type tagTranslation struct {
	// translations are old and new names of every own tag that changes
	// name.
	translations [][2]string
	// deletions are own tags whose old name is deleted after translating.
	deletions []string
	// pruned are another releasable's tags, deleted in the destination.
	pruned []string
	// alias is the new and old name kept side by side at the current
	// version; empty when there is none.
	alias [2]string
	// own are every tag the releasable's scheme owns in the source.
	own []string
	// commits are every source tag's commit.
	commits map[string]string
}

// applied is what the apply learned as it ran, passed between its steps.
type applied struct {
	lock      *runstate.Lock
	commitMap map[string]string
	pruned    []string
	tagMaps   []releaserecord.TagMapping
	remaps    []releaserecord.CommitMapping
	notes     []string
	commit    string
}

// Extract runs `monorepo extract` on the workspace ws.
func Extract(ctx *strictcli.Context, ws *workspace.Workspace, req ExtractRequest) error {
	if req.Say == nil || req.Sync == nil {
		return errors.New("monorepo extract needs somewhere to report to and a router regeneration")
	}
	var dep *departure
	run := &applied{}
	defer func() {
		if run.lock != nil {
			run.lock.Release()
		}
	}()
	_, err := previewapply.Reconcile(ctx, previewapply.Reconciler{
		Observe: func(o previewapply.Observer) (previewapply.Preview, error) {
			var err error
			dep, err = observeDeparture(o, ws, req)
			if err != nil {
				return previewapply.Preview{}, err
			}
			return dep.preview()
		},
		Apply: func(e *strictcli.Effects, it previewapply.Item) error {
			return dep.apply(e, it, run)
		},
		ShowKeys: true,
	})
	return err
}

// observeDeparture resolves and checks one extraction, reading only.
func observeDeparture(o previewapply.Observer, ws *workspace.Workspace, req ExtractRequest) (*departure, error) {
	if err := requireWorkspace(ws, "extract"); err != nil {
		return nil, err
	}
	d := ws.Declarations
	rel, ok := d.Releasable(req.Releasable)
	if !ok {
		var names []string
		for _, r := range ws.Releasables() {
			names = append(names, r.Name)
		}
		return nil, fmt.Errorf("no releasable is named %q; the declared releasables are %s", req.Releasable, joinOr(names, "none"))
	}
	members := ws.MembersOf(rel.Name)
	if len(members) == 0 {
		return nil, fmt.Errorf("the releasable %q has no member, so there is nothing to extract", rel.Name)
	}
	for _, m := range members {
		if m.IsRoot() {
			return nil, fmt.Errorf("the releasable %q holds the root member %q (path \".\"), which owns every file no other member claims, so extracting it would leave the workspace without a root. Version the root member under a releasable that stays, or make it a dev node (releasable = false), in %s before extracting", rel.Name, m.Name, declarations.ReleasablesFile)
		}
	}
	if err := refuseStayingNested(ws, rel.Name, members); err != nil {
		return nil, err
	}
	for _, m := range members {
		if err := checkMemberContents(o, ws.Root, m); err != nil {
			return nil, err
		}
	}
	if !filepath.IsAbs(req.Target) {
		return nil, fmt.Errorf("the target path %q is not absolute", req.Target)
	}
	target := filepath.Clean(req.Target)
	if _, err := os.Lstat(target); err == nil {
		return nil, fmt.Errorf("the target path %s exists already; the extracted repository is created there, so name a path that does not exist", target)
	} else if !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	if declarations.IsInside(filepath.ToSlash(target), filepath.ToSlash(ws.Root)) {
		return nil, fmt.Errorf("the target path %s lies inside the workspace %s; the extracted repository is a repository of its own, so name a path outside it", target, ws.Root)
	}
	req.Target = target
	if err := requireFilterRepo(); err != nil {
		return nil, err
	}
	if err := requireSaferm(req.DeleteWithRm, "the departing members' directories and the releasable's records"); err != nil {
		return nil, err
	}
	repo, err := git.Open(o, ws.Root)
	if err != nil {
		return nil, err
	}
	dirty, err := dirtyPaths(repo)
	if err != nil {
		return nil, err
	}
	if len(dirty) > 0 {
		return nil, fmt.Errorf("the working tree has uncommitted changes, and the extract rewrites committed history only, so they would be lost: commit or remove these first:\n  %s", strings.Join(dirty, "\n  "))
	}
	if err := refuseReleaseInFlight(ws.Root); err != nil {
		return nil, err
	}
	dep := &departure{req: req, ws: ws, releasable: rel, members: members, multi: len(members) > 1}
	for _, m := range members {
		dep.departing = append(dep.departing, m.Path)
	}
	if scoped, err := scopedEntriesIn(ws.Root, dep.departing); err != nil {
		return nil, err
	} else if len(scoped) > 0 {
		return nil, fmt.Errorf("options entries are scoped to a departing member, and once the member is no longer declared here every check run and release refuses them. Delete each entry from its document (state again in the new repository the ones it needs, with `rlsbl options set`), then run this again:\n  %s", strings.Join(scoped, "\n  "))
	}
	if err := dep.observeEdges(); err != nil {
		return nil, err
	}
	if err := dep.observeVersionAndTags(repo); err != nil {
		return nil, err
	}
	dep.sourceTrees = map[string]string{}
	for _, m := range members {
		tree, found, err := repo.TreeAt("HEAD", m.Path)
		if err != nil {
			return nil, err
		}
		if !found {
			return nil, fmt.Errorf("the member %q has no tree at %s in HEAD", m.Name, m.Path)
		}
		dep.sourceTrees[m.Name] = tree
	}
	if dep.sourceURL, err = originOrPath(repo); err != nil {
		return nil, err
	}
	if err := dep.observePublishers(); err != nil {
		return nil, err
	}
	if err := dep.observeState(); err != nil {
		return nil, err
	}
	if err := dep.observeDestination(); err != nil {
		return nil, err
	}
	if err := dep.observeSourceEdit(); err != nil {
		return nil, err
	}
	return dep, nil
}

// refuseStayingNested refuses a departing member with a member nested in it
// that stays: its files would leave with the departing directory and be
// deleted here.
func refuseStayingNested(ws *workspace.Workspace, releasable string, members []declarations.Member) error {
	departing := map[string]bool{}
	for _, m := range members {
		departing[m.Name] = true
	}
	for _, m := range members {
		for _, p := range ws.NestedMemberPaths(m) {
			stayer, _ := ws.Declarations.MemberAt(p)
			if departing[stayer.Name] {
				continue
			}
			where := "versioned under no releasable"
			wayOut := fmt.Sprintf("Move its directory out of %s, or version it under %q (its releasable field in %s)", m.Path, releasable, declarations.ReleasablesFile)
			if stayer.Versioned() {
				where = fmt.Sprintf("versioned under the releasable %q", stayer.Releasable)
				wayOut = fmt.Sprintf("Extract %q first, or version the member under %q (its releasable field in %s)", stayer.Releasable, releasable, declarations.ReleasablesFile)
			}
			return fmt.Errorf("the releasable %q cannot be extracted: the member %q (%s) lies inside the departing member %q and stays behind (%s), so its files would leave with %s and be deleted here. %s, then run this again", releasable, stayer.Name, p, m.Name, where, m.Path, wayOut)
		}
	}
	return nil
}

// gitlinkMode is the mode of a submodule's entry in a tree.
const gitlinkMode = "160000"

// checkMemberContents refuses a member with nothing tracked at its path
// (there is no tree to carry or to verify) and one holding a submodule (the
// source's commit could not record its removal).
func checkMemberContents(run git.Runner, root string, m declarations.Member) error {
	c, err := run.Run([]interface{}{"git", "ls-tree", "-r", "-z", "HEAD", "--", m.Path}, strictcli.Cwd(root), strictcli.Check(false), strictcli.Timeout(conversionGitTimeout))
	if err != nil {
		return err
	}
	if c.ExitCode() != 0 {
		return fmt.Errorf("git ls-tree in %s exited %d: %s", root, c.ExitCode(), strings.TrimSpace(c.Stderr()))
	}
	var gitlinks []string
	count := 0
	for _, record := range strings.Split(c.Stdout(), "\x00") {
		meta, entry, ok := strings.Cut(record, "\t")
		if !ok || entry == "" {
			continue
		}
		count++
		if fields := strings.Fields(meta); len(fields) > 0 && fields[0] == gitlinkMode {
			gitlinks = append(gitlinks, entry)
		}
	}
	if count == 0 {
		return fmt.Errorf("the member %q has nothing tracked at %s in HEAD, so there is no tree to carry or to verify. Commit the member's files, or take the member out of the releasable, before extracting", m.Name, m.Path)
	}
	if len(gitlinks) > 0 {
		sort.Strings(gitlinks)
		return fmt.Errorf("the member %q holds a submodule (%s), and the extract's commit in this repository cannot record the removal of one, so it would delete the member and fail to record it. Remove the submodule, or bring its content into this repository, then run this again", m.Name, strings.Join(gitlinks, ", "))
	}
	return nil
}

// refuseReleaseInFlight refuses while a release or batch release is in
// progress, or a batch release file is waiting: either would release from
// a workspace the extract changes under it.
func refuseReleaseInFlight(root string) error {
	inProgress, err := runstate.InProgressReleasables(root)
	if err != nil {
		return err
	}
	if len(inProgress) > 0 {
		return fmt.Errorf("a release is in progress for %s (%s); resume it with `"+runstate.ResumeInvocation+"` or abandon it with `rlsbl release abandon` first", strings.Join(inProgress, ", "), runstate.InProgressPath(inProgress[0]))
	}
	if _, found, err := runstate.LoadBatchPlan(root); err != nil {
		return err
	} else if found {
		return fmt.Errorf("a batch release is in progress (%s); finish it with `"+runstate.BatchRunInvocation+"` first", runstate.BatchPlanPath)
	}
	if exists, err := pathExists(root, releaserecord.BatchReleaseFilePath); err != nil {
		return err
	} else if exists {
		return fmt.Errorf("the batch release file %s is waiting to be released; release the batch, or delete the file, first", releaserecord.BatchReleaseFilePath)
	}
	return nil
}

// originOrPath is the URL of the repository's origin, or its path when it
// has no origin.
func originOrPath(repo git.Repo) (string, error) {
	configured, err := repo.RemoteConfigured("origin")
	if err != nil {
		return "", err
	}
	if !configured {
		return repo.Dir(), nil
	}
	return repo.RemoteURL("origin")
}

// observeEdges refuses a dependency of a member that stays on one that
// leaves, naming how to sever each, and records the dependencies of the
// departing members on members that stay.
func (dep *departure) observeEdges() error {
	ws := dep.ws
	g := workspace.NewGraph(ws)
	if len(g.ScanErrors) > 0 {
		var lines []string
		for _, s := range g.ScanErrors {
			lines = append(lines, "  - "+s.Error())
		}
		return fmt.Errorf("the workspace's dependency graph is incomplete: a manifest could not be read, so the dependencies into and out of the releasable cannot be established, and one the extract cannot see would be left pointing at nothing. Fix each manifest, then run this again:\n%s", strings.Join(lines, "\n"))
	}
	leaving := map[string]bool{}
	for _, m := range dep.members {
		leaving[m.Name] = true
	}
	var problems []string
	for _, m := range dep.members {
		for _, name := range g.Dependents(m.Name) {
			if leaving[name] {
				continue
			}
			stayer, _ := ws.Declarations.Member(name)
			var edge workspace.Dependency
			for _, d := range g.Dependencies(name) {
				if d.Name == m.Name {
					edge = d
				}
			}
			problems = append(problems, fmt.Sprintf("  - %q depends on %q (%s, scope %s)", name, m.Name, edge.Form, edge.Scope))
			problems = append(problems, severingEdits(ws, stayer, m, edge)...)
		}
		for _, d := range g.Dependencies(m.Name) {
			if !leaving[d.Name] {
				dep.outbound = append(dep.outbound, fmt.Sprintf("%s depends on %s (%s, scope %s), which stays: the new repository resolves it from a registry", m.Name, d.Name, d.Form, d.Scope))
			}
		}
	}
	if len(problems) > 0 {
		return fmt.Errorf("members that stay depend on members that would leave:\n%s\nSever each dependency first (the extract never rewrites a manifest itself), then run this again", strings.Join(problems, "\n"))
	}
	return nil
}

// spellings are every name the departing member m may be depended on by:
// its member name, its registry name, and the name its manifest declares
// for the target.
func spellings(ws *workspace.Workspace, m declarations.Member, target string) map[string]bool {
	names := map[string]bool{m.Name: true}
	if m.RegistryName != "" {
		names[m.RegistryName] = true
	}
	if t, err := targets.Get(target); err == nil {
		if name, found, err := t.ReadName(ws.MemberDir(m)); err == nil && found && name != "" {
			names[name] = true
		}
	}
	return names
}

// severingEdits are the edits that sever the dependency of stayer on the
// departing member m, read from the stayer's manifests: every one that
// applies, since a dependency may be declared in more than one place.
func severingEdits(ws *workspace.Workspace, stayer, m declarations.Member, edge workspace.Dependency) []string {
	var out []string
	if edge.Form == workspace.FormExplicit {
		out = append(out, fmt.Sprintf("    remove %q from the depends_on of the member %q in %s", m.Name, stayer.Name, declarations.ReleasablesFile))
	}
	dir := ws.MemberDir(stayer)
	if line := pythonEdit(ws, dir, stayer, m); line != "" {
		out = append(out, line)
	}
	if line := npmEdit(ws, dir, stayer, m); line != "" {
		out = append(out, line)
	}
	if line := goEdit(ws, dir, stayer, m); line != "" {
		out = append(out, line)
	}
	if len(out) == 0 {
		out = append(out, fmt.Sprintf("    no manifest of %s names %q (the dependency graph read this dependency as %s), so sever it wherever it is declared", stayer.Path, m.Name, edge.Form))
	}
	return out
}

func pythonEdit(ws *workspace.Workspace, dir string, stayer, m declarations.Member) string {
	path := filepath.Join(dir, "pyproject.toml")
	p, err := dependencies.ReadPyproject(path)
	if err != nil {
		return ""
	}
	names := map[string]bool{}
	for name := range spellings(ws, m, targets.PyPI) {
		names[dependencies.NormalizePypiName(name)] = true
	}
	entries, err := p.EntriesNaming(names, dependencies.AllFamilies)
	if err != nil {
		return ""
	}
	local, err := p.UvLocalSources()
	if err != nil {
		return ""
	}
	var sources []string
	for _, s := range local {
		if names[dependencies.NormalizePypiName(s.Name)] {
			sources = append(sources, "[tool.uv.sources]."+s.Name)
		}
	}
	if len(entries) == 0 && len(sources) == 0 {
		return ""
	}
	var edits []string
	if len(sources) > 0 {
		edits = append(edits, "delete "+strings.Join(sources, ", "))
	}
	if len(entries) > 0 {
		var floored []string
		for _, e := range entries {
			floored = append(floored, fmt.Sprintf("[%s] entry %q", e.Section, e.Original))
		}
		edits = append(edits, "floor "+strings.Join(floored, ", "))
	}
	line := fmt.Sprintf("    in %s/pyproject.toml: %s, so the package resolves from the registry at the version the lock resolves", stayer.Path, strings.Join(edits, " and "))
	if _, err := os.Stat(filepath.Join(dir, "uv.lock")); err == nil {
		return line + fmt.Sprintf("\n      `rlsbl rewrite uv-path-sources` makes that edit: (cd %s && rlsbl rewrite uv-path-sources --dry-run), then without --dry-run", shellQuote(stayer.Path))
	}
	return line + fmt.Sprintf("\n      %s has no uv.lock of its own, and `rlsbl rewrite uv-path-sources` reads the floor from one beside the manifest it rewrites, so make this edit by hand", stayer.Path)
}

func npmEdit(ws *workspace.Workspace, dir string, stayer, m declarations.Member) string {
	data, err := os.ReadFile(filepath.Join(dir, "package.json"))
	if err != nil {
		return ""
	}
	names := spellings(ws, m, targets.NPM)
	for _, section := range []string{"dependencies", "devDependencies", "peerDependencies", "optionalDependencies"} {
		for name := range names {
			needle := fmt.Sprintf("%q", name)
			if !strings.Contains(string(data), needle) {
				continue
			}
			if !strings.Contains(string(data), fmt.Sprintf("%q", section)) {
				continue
			}
			return fmt.Sprintf("    in %s/package.json: replace the %q entry in %q with the published range (\"^<the version it is developed against>\"), and drop %s from any \"workspaces\" array listing it; no rewrite command owns package.json, so this is a hand edit", stayer.Path, name, section, m.Path)
		}
	}
	return ""
}

func goEdit(ws *workspace.Workspace, dir string, stayer, m declarations.Member) string {
	module := moduleOf(ws.MemberDir(m))
	if module == "" {
		return ""
	}
	data, err := os.ReadFile(filepath.Join(dir, "go.mod"))
	if err != nil || !strings.Contains(string(data), module) {
		return ""
	}
	return fmt.Sprintf("    %s/go.mod requires %s, which leaves with the extract:\n      rlsbl rewrite go-module-path --from-module %s --to-module <its module path in the new repository>   (at the repository root, before extracting)", stayer.Path, module, module)
}

// moduleOf is the module path the go.mod in dir declares, or "".
func moduleOf(dir string) string {
	data, err := os.ReadFile(filepath.Join(dir, "go.mod"))
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(data), "\n") {
		if rest, ok := strings.CutPrefix(strings.TrimSpace(line), "module "); ok {
			return strings.Trim(strings.TrimSpace(rest), "\"")
		}
	}
	return ""
}

// observeVersionAndTags reads the releasable's version file and plans what
// happens to every tag of the source in the destination.
func (dep *departure) observeVersionAndTags(repo git.Repo) error {
	ws, rel := dep.ws, dep.releasable
	if text, found, err := releasableVersion(ws, rel.Name); err != nil {
		return err
	} else if found {
		dep.version = text
	}
	var err error
	if dep.ownScheme, err = workspace.SchemeOf(rel); err != nil {
		return err
	}
	destFormat := rel.TagFormat
	if !dep.multi {
		destFormat = standaloneTagFormat
	}
	if dep.destScheme, err = workspace.SchemeOf(declarations.Releasable{Name: rel.Name, TagFormat: destFormat}); err != nil {
		return err
	}
	foreign, err := dep.foreignSchemes()
	if err != nil {
		return err
	}
	tags, err := repo.TagCommits()
	if err != nil {
		return err
	}
	plan := tagTranslation{commits: tags}
	own := map[string]bool{}
	for _, name := range sortedKeys(tags) {
		if dep.ownScheme.Owns(name) {
			own[name] = true
			plan.own = append(plan.own, name)
		}
	}
	if dep.ownScheme.Pattern() != dep.destScheme.Pattern() {
		for _, old := range plan.own {
			v, _ := dep.ownScheme.VersionOf(old)
			next := dep.destScheme.Render(v)
			if at, exists := tags[next]; exists && !own[next] && at != tags[old] {
				return fmt.Errorf("the tag %s would be renamed %s in the new repository, and a tag %s exists already at another commit (%s, where %s names %s) that is not this releasable's. Resolve the tag %s before extracting", old, next, next, short(at), old, short(tags[old]), next)
			}
			plan.translations = append(plan.translations, [2]string{old, next})
			if v.String() == dep.version {
				plan.alias = [2]string{next, old}
			} else {
				plan.deletions = append(plan.deletions, old)
			}
		}
	}
	for _, name := range sortedKeys(tags) {
		if own[name] {
			continue
		}
		for _, s := range foreign {
			if s.Owns(name) {
				plan.pruned = append(plan.pruned, name)
				break
			}
		}
	}
	dep.tags = plan
	return nil
}

// foreignSchemes are the tag schemes of what stays: every other
// releasable's, and the module proxy's tags of every member that stays with
// a go target.
func (dep *departure) foreignSchemes() ([]workspace.TagScheme, error) {
	ws := dep.ws
	var out []workspace.TagScheme
	for _, r := range ws.Releasables() {
		if r.Name == dep.releasable.Name {
			continue
		}
		s, err := workspace.SchemeOf(r)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	for _, m := range ws.Members() {
		if m.Releasable == dep.releasable.Name {
			continue
		}
		list, err := targets.MemberTargets(ws.Root, m)
		if err != nil {
			return nil, fmt.Errorf("the tags of the member %q cannot be told apart from the departing releasable's: %w", m.Name, err)
		}
		for _, t := range list {
			if t.Name != targets.Go {
				continue
			}
			pattern := "v{version}"
			if dir := m.TargetDir(t); dir != declarations.RootPath {
				pattern = dir + "/v{version}"
			}
			s, err := workspace.NewTagScheme(pattern)
			if err != nil {
				return nil, err
			}
			out = append(out, s)
		}
	}
	return out, nil
}

// observePublishers records the departing targets whose registry
// authorizes publishing for a repository, which does not follow the code.
func (dep *departure) observePublishers() error {
	seen := map[string]bool{}
	for _, m := range dep.members {
		list, err := targets.MemberTargets(dep.ws.Root, m)
		if err != nil {
			return err
		}
		for _, t := range list {
			target, err := targets.Get(t.Name)
			if err != nil {
				return err
			}
			if f := target.Facts(); f.PublisherBindsToRepository && !seen[f.Name] {
				seen[f.Name] = true
				dep.publishers = append(dep.publishers, f)
			}
		}
	}
	sort.Slice(dep.publishers, func(i, j int) bool { return dep.publishers[i].Name < dep.publishers[j].Name })
	return nil
}

// observeState lists the records that move: the changelog's files (the
// generated CHANGELOG.md is regenerated, not carried) and the release
// directory's files (a standalone repository keeps no version file: its
// version is its manifests').
func (dep *departure) observeState() error {
	root, name := dep.ws.Root, dep.releasable.Name
	logs, err := filesIn(root, declarations.ChangelogDir(name))
	if err != nil {
		return err
	}
	for _, f := range logs {
		if f != changelog.MarkdownName {
			dep.changelogs = append(dep.changelogs, f)
		}
	}
	files, err := filesIn(root, declarations.ReleasesDir(name))
	if err != nil {
		return err
	}
	for _, f := range files {
		if f == "version" && !dep.multi {
			continue
		}
		dep.releaseFiles = append(dep.releaseFiles, f)
	}
	return nil
}

// destPath is a departing member's path in the destination.
func (dep *departure) destPath(m declarations.Member) string {
	if dep.multi {
		return m.Path
	}
	return declarations.RootPath
}

// translatePath is a path recorded in the source spelled as the destination
// spells it: a lone member is hoisted to the repository root.
func (dep *departure) translatePath(p string) string {
	if dep.multi {
		return p
	}
	return relativeTo(p, dep.members[0].Path, declarations.RootPath)
}

// departingNames are the names the departing members publish under.
func (dep *departure) departingNames() []string {
	seen := map[string]bool{}
	for _, m := range dep.members {
		name := m.RegistryName
		if name == "" {
			name = m.Name
		}
		seen[name] = true
	}
	return sortedKeys(seen)
}

// observeDestination builds the destination's declarations and
// lifecycle-and-license record and checks both, and picks the transition
// record's events about the releasable.
func (dep *departure) observeDestination() error {
	src := dep.ws.Declarations
	rel := dep.releasable
	destRel := rel
	destRel.PublishCICheckPattern = ""
	d := &declarations.Releasables{
		ReleaseBranches: src.ReleaseBranches,
		EnvironmentFile: src.EnvironmentFile,
		Timeouts:        src.Timeouts,
	}
	names := map[string]bool{}
	for _, m := range dep.members {
		names[m.Name] = true
	}
	if dep.multi {
		d.Layout = declarations.LayoutWorkspace
		d.Members = append(d.Members, declarations.Member{Path: declarations.RootPath, Name: declarations.RootName, DevOnly: true})
		for _, m := range dep.members {
			var kept []string
			for _, other := range m.DependsOn {
				if names[other] {
					kept = append(kept, other)
				}
			}
			m.DependsOn = kept
			d.Members = append(d.Members, m)
		}
	} else {
		d.Layout = declarations.LayoutStandalone
		destRel.TagFormat = standaloneTagFormat
		destRel.Hooks = hoistHooks(rel.Hooks, dep.members[0].Path)
		m := dep.members[0]
		m.Path, m.Name, m.DependsOn = declarations.RootPath, declarations.RootName, nil
		d.Members = []declarations.Member{m}
	}
	d.Releasables = []declarations.Releasable{destRel}
	parsed, err := declarations.Parse(declarations.Render(d))
	if err != nil {
		return fmt.Errorf("the new repository's declarations would be refused: %w", err)
	}
	dep.destDecls = parsed

	rec, err := lifecycle.Load(dep.ws.Root)
	if err != nil {
		return err
	}
	subjects := append([]string{rel.Name}, sortedKeys(names)...)
	if err := pendingIdentitiesOf(rec, subjects); err != nil {
		return err
	}
	// A lone member becomes the root member, named root; a member named as
	// its releasable is that subject, which keeps its name.
	rename := map[string]string{rel.Name: rel.Name}
	for _, m := range dep.members {
		if m.Name == rel.Name {
			continue
		}
		rename[m.Name] = m.Name
		if !dep.multi {
			rename[m.Name] = declarations.RootName
		}
	}
	moved := entriesOf(rec, rename)
	var unversioned []lifecycle.UnversionedTag
	for _, t := range rec.UnversionedTags() {
		if _, ok := dep.tags.commits[t.Tag]; ok {
			unversioned = append(unversioned, t)
		}
	}
	on := dep.req.Now
	destRecord, err := lifecycle.Parse(renderRecord(moved, nil, nil, unversioned))
	if err != nil {
		return fmt.Errorf("the new repository's lifecycle-and-license record would be refused: %w", err)
	}
	if destRecord.Confidential(on) && (len(rec.Codenames()) > 0 || len(rec.DistinctiveTerms()) > 0) {
		if destRecord, err = lifecycle.Parse(renderRecord(moved, rec.Codenames(), rec.DistinctiveTerms(), unversioned)); err != nil {
			return fmt.Errorf("the new repository's lifecycle-and-license record would be refused: %w", err)
		}
	}
	reason := fmt.Sprintf("extracted from %s with `rlsbl monorepo extract %s`", dep.ws.Root, rel.Name)
	if err := moveTagNamespace(destRecord, rel.Name, dep.ownScheme.ListGlob(), dep.destScheme.ListGlob(), on, reason); err != nil {
		return err
	}
	if err := destRecord.Validate(on, declaredSubjectsOf(parsed)); err != nil {
		return fmt.Errorf("the new repository's lifecycle-and-license record would be refused: %w", err)
	}
	dep.destRecord = destRecord

	events, err := releaserecord.ReadEvents(dep.ws.Root)
	if err != nil {
		return err
	}
	dep.carried = eventsOf(events, rel.Name)
	return nil
}

// hoistHooks are a releasable's hooks with each directory relative to the
// member hoisted to the repository root.
func hoistHooks(h declarations.Hooks, member string) declarations.Hooks {
	move := func(list []declarations.Hook) []declarations.Hook {
		var out []declarations.Hook
		for _, hook := range list {
			if hook.Dir != "" {
				hook.Dir = relativeTo(hook.Dir, member, declarations.RootPath)
				if hook.Dir == declarations.RootPath {
					hook.Dir = ""
				}
			}
			out = append(out, hook)
		}
		return out
	}
	return declarations.Hooks{PreChecks: move(h.PreChecks), PreRelease: move(h.PreRelease), PostRelease: move(h.PostRelease)}
}

// declaredSubjectsOf are every releasable and member name d holds.
func declaredSubjectsOf(d *declarations.Releasables) []string {
	var names []string
	for _, r := range d.Releasables {
		names = append(names, r.Name)
	}
	for _, m := range d.Members {
		names = append(names, m.Name)
	}
	return names
}

// observeSourceEdit builds the source's declarations without the
// releasable and its members, with the departing package names added to
// the dependency floors of every member of a releasable that stays, and
// its lifecycle-and-license record with the departing subjects closed, and
// checks both.
func (dep *departure) observeSourceEdit() error {
	root := dep.ws.Root
	data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(declarations.ReleasablesFile)))
	if err != nil {
		return fmt.Errorf("reading %s: %w", declarations.ReleasablesFile, err)
	}
	ed, err := declarations.NewEditor(data)
	if err != nil {
		return err
	}
	for _, m := range dep.members {
		if err := ed.RemoveMember(m.Path); err != nil {
			return err
		}
	}
	if err := ed.RemoveReleasable(dep.releasable.Name); err != nil {
		return err
	}
	names := dep.departingNames()
	for _, r := range dep.ws.Releasables() {
		if r.Name == dep.releasable.Name {
			continue
		}
		for _, m := range dep.ws.MembersOf(r.Name) {
			floors := union(m.InternalDepFloors, names)
			if len(floors) == len(m.InternalDepFloors) {
				continue
			}
			if err := ed.SetInternalDepFloors(m.Path, floors); err != nil {
				return err
			}
			dep.floorMembers = append(dep.floorMembers, m.Name)
		}
	}
	edited, parsed, err := ed.Result()
	if err != nil {
		return fmt.Errorf("the workspace's declarations without the releasable would be refused: %w", err)
	}
	dep.sourceDecls, dep.sourceEdited = edited, parsed
	if dep.floorSwitches, err = depFloorsSwitches(root, parsed); err != nil {
		return err
	}

	rec, err := lifecycle.Load(root)
	if err != nil {
		return err
	}
	if !rec.Present() {
		return nil
	}
	subjects := []string{dep.releasable.Name}
	for _, m := range dep.members {
		subjects = append(subjects, m.Name)
	}
	if err := closeSubjects(rec, subjects, dep.req.Now); err != nil {
		return err
	}
	if err := rec.Validate(dep.req.Now, declaredSubjectsOf(parsed)); err != nil {
		return fmt.Errorf("this repository's lifecycle-and-license record, with the departing subjects closed, would be refused: %w", err)
	}
	dep.sourceRecord = rec
	return nil
}

// union is list with every name of more added once, sorted.
func union(list, more []string) []string {
	seen := map[string]bool{}
	for _, n := range list {
		seen[n] = true
	}
	for _, n := range more {
		seen[n] = true
	}
	return sortedKeys(seen)
}

// preview is the plan, one item per apply step.
func (dep *departure) preview() (previewapply.Preview, error) {
	rel := dep.releasable
	var memberFacts, memberPaths, treeFacts []string
	for _, m := range dep.members {
		memberFacts = append(memberFacts, fmt.Sprintf("member: %s at %s", m.Name, m.Path))
		memberPaths = append(memberPaths, m.Path)
		to := dep.destPath(m)
		if to == declarations.RootPath {
			to = "the repository root"
		}
		treeFacts = append(treeFacts, fmt.Sprintf("%s: tree %s at %s -> %s", m.Name, short(dep.sourceTrees[m.Name]), m.Path, to))
	}
	shape, state := "a workspace", "extract_to_workspace"
	hoist := ""
	if !dep.multi {
		shape, state = "a standalone repository", "extract_to_standalone"
		hoist = fmt.Sprintf(", hoisting %s to the repository root", dep.members[0].Path)
	}
	version := dep.version
	if version == "" {
		version = "no version file"
	}
	items := []previewapply.Item{{
		Key:     extractItemReleasable,
		State:   state,
		Summary: fmt.Sprintf("the releasable %q (%s) becomes %s at %s.", rel.Name, version, shape, dep.req.Target),
		Facts:   append(memberFacts, fmt.Sprintf("tag format: %s -> %s", rel.TagFormat, dep.destDecls.Releasables[0].TagFormat)),
		Actions: []string{fmt.Sprintf("apply would clone this repository and run git-filter-repo keeping %s%s", strings.Join(memberPaths, ", "), hoist)},
	}}
	depState, depSummary := "no_edges_to_sever", "no member that stays depends on a departing member."
	if len(dep.outbound) > 0 {
		depState, depSummary = "outbound_edges", fmt.Sprintf("%d dependencies leave with the departing members.", len(dep.outbound))
	}
	items = append(items, previewapply.Item{Key: extractItemDeps, State: depState, Summary: depSummary, Facts: dep.outbound})
	items = append(items, previewapply.Item{
		Key:     extractItemTrees,
		State:   "verify_member_trees",
		Summary: "every member's tree must be the tree that left.",
		Facts:   treeFacts,
		Actions: []string{"apply would compare each tree with the rewritten history's and stop on a difference, naming both trees."},
	})
	stateFacts := []string{
		"changelog: " + joinOr(dep.changelogs, "nothing"),
		"release records: " + joinOr(dep.releaseFiles, "nothing"),
	}
	if !dep.multi {
		stateFacts = append(stateFacts, "not carried: the version file (a standalone repository's version is its manifests')")
	}
	items = append(items, previewapply.Item{
		Key:     extractItemState,
		State:   "transplant_state",
		Summary: fmt.Sprintf("the releasable's changelog and release records move to %s and %s in the new repository.", declarations.ChangelogDir(rel.Name), declarations.ReleasesDir(rel.Name)),
		Facts:   stateFacts,
		Actions: []string{"apply would map every changelog commit and every release commit through git-filter-repo's commit map, drop an entry none of whose commits carried over, and name what it could not map."},
	})
	items = append(items, dep.tagItem())
	destFacts := []string{
		fmt.Sprintf("%s: %s, the releasable %q with tag format %s", declarations.ReleasablesFile, dep.destDecls.Layout, rel.Name, dep.destDecls.Releasables[0].TagFormat),
		fmt.Sprintf("%s: every entry of %s", lifecycle.RecordFile, strings.Join(dep.subjectNames(), ", ")),
	}
	if len(dep.carried) > 0 {
		destFacts = append(destFacts, fmt.Sprintf("%s: the %d events this repository's transition record holds about the releasable", declarations.TransitionsFile, len(dep.carried)))
	}
	items = append(items, previewapply.Item{
		Key:     extractItemDest,
		State:   "write_destination",
		Summary: "the new repository gets its declarations, its lifecycle-and-license record, and its changelog, committed.",
		Facts:   destFacts,
		Actions: []string{"apply would commit them and read the new repository's declarations back, stopping when they do not load."},
	})
	events := []string{"conversion (direction extract)"}
	if len(dep.tags.translations) > 0 {
		events = append(events, "tag-map")
	}
	if len(dep.releaseFiles) > 0 {
		events = append(events, "release-commit-remap (when a release commit moved)")
	}
	if dep.tags.alias[0] != "" {
		events = append(events, "boundary-alias")
	}
	items = append(items, previewapply.Item{
		Key:     extractItemRecord,
		State:   "record_transition_record",
		Summary: "the new repository's transition record explains the conversion: " + strings.Join(events, ", ") + ".",
	})
	deleter := "saferm"
	if dep.req.DeleteWithRm {
		deleter = "a plain removal (--delete-with-rm)"
	}
	sourceFacts := []string{
		fmt.Sprintf("deleted through %s: %s, %s, %s", deleter, strings.Join(memberPaths, ", "), declarations.ChangelogDir(rel.Name), declarations.ReleasesDir(rel.Name)),
		fmt.Sprintf("%s loses the members and the releasable", declarations.ReleasablesFile),
		fmt.Sprintf("%s records the departed tag namespace %s", declarations.TransitionsFile, dep.ownScheme.ListGlob()),
		"the tags stay in this repository; the departed-globs event explains them",
	}
	if len(dep.floorMembers) > 0 {
		sourceFacts = append(sourceFacts, fmt.Sprintf("internal_dep_floors gains %s on %s", strings.Join(dep.departingNames(), ", "), strings.Join(dep.floorMembers, ", ")))
	}
	for _, s := range dep.floorSwitches {
		sourceFacts = append(sourceFacts, fmt.Sprintf("rlsbl:dep-floors is switched on for %s", s.member))
	}
	if dep.sourceRecord != nil {
		sourceFacts = append(sourceFacts, fmt.Sprintf("%s closes every open period and identity of %s", lifecycle.RecordFile, strings.Join(dep.departingSubjects(), ", ")))
	}
	items = append(items, previewapply.Item{
		Key:     extractItemSource,
		State:   "remove_members",
		Summary: fmt.Sprintf("this repository loses %d members and the releasable %q.", len(dep.members), rel.Name),
		Facts:   sourceFacts,
		Actions: []string{"apply would regenerate the routers and commit all of it as one commit: " + ExtractCommitMessage(rel.Name)},
	})
	items = append(items, previewapply.Item{
		Key:     extractItemNextSteps,
		State:   "operator_actions",
		Summary: "rlsbl administers no external system; these are yours.",
		Facts:   dep.nextSteps(),
	})
	return previewapply.NewPreview(items...)
}

// subjectNames are the departing subjects as the destination's record names
// them.
func (dep *departure) subjectNames() []string {
	names := []string{dep.releasable.Name}
	for _, m := range dep.members {
		switch {
		case m.Name == dep.releasable.Name:
		case dep.multi:
			names = append(names, m.Name)
		default:
			names = append(names, declarations.RootName)
		}
	}
	return names
}

// departingSubjects are the departing subjects as this repository names
// them.
func (dep *departure) departingSubjects() []string {
	names := []string{dep.releasable.Name}
	for _, m := range dep.members {
		names = append(names, m.Name)
	}
	return names
}

// tagItem is the tags' verdict: tags that change name, a releasable that
// owns no tag yet, and tags that keep their names are three answers.
func (dep *departure) tagItem() previewapply.Item {
	plan := dep.tags
	var facts []string
	for _, t := range plan.translations {
		facts = append(facts, t[0]+" -> "+t[1])
	}
	if plan.alias[0] != "" {
		facts = append(facts, fmt.Sprintf("boundary alias: %s is kept beside %s at version %s", plan.alias[1], plan.alias[0], dep.version))
	}
	if len(plan.pruned) > 0 {
		facts = append(facts, "deleted, another releasable's: "+strings.Join(plan.pruned, ", "))
	}
	item := previewapply.Item{Key: extractItemTags, Facts: facts}
	switch {
	case len(plan.translations) > 0:
		item.State, item.Summary = "translate_tags", fmt.Sprintf("%d tags take the scheme %s.", len(plan.translations), dep.destScheme.Pattern())
	case len(plan.own) == 0:
		item.State, item.Summary = "no_tag_to_translate", fmt.Sprintf("the releasable owns no tag here, so nothing is renamed; the new repository tags under %s from its first release there.", dep.destScheme.Pattern())
	default:
		item.State, item.Summary = "tags_unchanged", fmt.Sprintf("the new repository keeps the scheme %s, so no tag changes name.", dep.destScheme.Pattern())
	}
	return item
}

// nextSteps are what rlsbl leaves to the operator.
func (dep *departure) nextSteps() []string {
	steps := []string{
		fmt.Sprintf("create the remote repository and add it as origin in %s", dep.req.Target),
		fmt.Sprintf("(cd %s && rlsbl scaffold) for its CI, hooks, and workflows", shellQuote(dep.req.Target)),
		fmt.Sprintf("review the routers regenerated in %s before the next release there", dep.ws.Root),
	}
	for _, f := range dep.publishers {
		steps = append(steps, fmt.Sprintf("%s authorizes publishing for a repository, not for the package, so it does not follow the code: register the new repository at %s before its first release there (a publish that fails for want of it is recovered with `rlsbl release retry --watch`, not a new version)", f.RegistryDisplayName, f.PublisherSetupURL))
	}
	return steps
}

// apply performs one item's step.
func (dep *departure) apply(e *strictcli.Effects, it previewapply.Item, run *applied) error {
	switch it.Key {
	case extractItemReleasable:
		return dep.applyHistory(e, run)
	case extractItemDeps:
		return nil
	case extractItemTrees:
		return dep.applyTrees(e)
	case extractItemState:
		return dep.applyState(e, run)
	case extractItemTags:
		return dep.applyTags(e, run)
	case extractItemDest:
		return dep.applyDestination(e, run)
	case extractItemRecord:
		return dep.applyRecord(e, run)
	case extractItemSource:
		return dep.applySource(e, run)
	case extractItemNextSteps:
		return dep.applyNextSteps(run)
	}
	return fmt.Errorf("no step applies the item %q", it.Key)
}

// applyHistory takes the lock, clones this repository, and rewrites the
// clone down to the members.
func (dep *departure) applyHistory(e *strictcli.Effects, run *applied) error {
	root := dep.ws.Root
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
	dep.req.Say(fmt.Sprintf("Cloning %s to %s ...", root, dep.req.Target))
	if err := cloneForRewrite(e, root, dep.req.Target); err != nil {
		return err
	}
	var args []string
	for _, m := range dep.members {
		args = append(args, "--path", m.Path)
	}
	if !dep.multi {
		// One run carries the path filter and the hoist together, so the
		// commit map is keyed by this repository's commits.
		args = append(args, "--path-rename", dep.members[0].Path+"/:")
	}
	if err := runFilterRepo(e, dep.req.Target, append(args, "--force")...); err != nil {
		return err
	}
	if run.commitMap, run.pruned, err = readCommitMap(dep.req.Target); err != nil {
		return err
	}
	dep.req.Say(fmt.Sprintf("  git-filter-repo mapped %d commits; %d pruned.", len(run.commitMap), len(run.pruned)))
	return nil
}

// applyTrees verifies that each member's tree is the tree that left.
func (dep *departure) applyTrees(e *strictcli.Effects) error {
	dest, err := git.Open(e, dep.req.Target)
	if err != nil {
		return err
	}
	for _, m := range dep.members {
		want := dep.sourceTrees[m.Name]
		to := dep.destPath(m)
		got, found, err := dest.TreeAt("HEAD", to)
		if err != nil {
			return err
		}
		if !found || got != want {
			return fmt.Errorf("the tree of the member %q is %s at %s here, and %q in the rewritten history at %s is %q: the history that arrived is not the history that left, so nothing further was written. This repository is unchanged, and %s can be deleted", m.Name, want, m.Path, got, to, dep.req.Target, dep.req.Target)
		}
		dep.req.Say(fmt.Sprintf("  %s: tree %s verified.", m.Name, short(want)))
	}
	return nil
}

// applyState copies the releasable's records into the destination and
// moves every commit they name through the commit map.
func (dep *departure) applyState(e *strictcli.Effects, run *applied) error {
	root, target, name := dep.ws.Root, dep.req.Target, dep.releasable.Name
	for _, owned := range []string{declarations.ChangelogRoot, declarations.ReleasesRoot} {
		if err := declarations.EnsureOwnedDirectory(e, target, owned); err != nil {
			return err
		}
	}
	for _, f := range dep.changelogs {
		rel := slashJoin(declarations.ChangelogDir(name), f)
		if err := copyFile(e, filepath.Join(root, filepath.FromSlash(rel)), filepath.Join(target, filepath.FromSlash(rel))); err != nil {
			return err
		}
	}
	for _, f := range dep.releaseFiles {
		rel := slashJoin(declarations.ReleasesDir(name), f)
		if err := copyFile(e, filepath.Join(root, filepath.FromSlash(rel)), filepath.Join(target, filepath.FromSlash(rel))); err != nil {
			return err
		}
	}
	dest, err := git.Open(e, target)
	if err != nil {
		return err
	}
	files, err := changelog.ReadAll(target, declarations.ChangelogDir(name))
	if err != nil {
		return err
	}
	for _, f := range files {
		if len(f.Lines) == 0 {
			continue
		}
		kept, dropped, narrowed, err := carryEntries(f.Entries(), func(h string) (string, error) {
			next, _ := changelog.MapCommit(h, run.commitMap)
			return next, nil
		})
		if err != nil {
			return err
		}
		if err := changelog.ReplaceEntries(e, target, f, kept); err != nil {
			return err
		}
		if dropped > 0 || narrowed > 0 {
			run.notes = append(run.notes, fmt.Sprintf("the changelog of %s: %d entries dropped and %d narrowed, their commits not carried by the rewrite", f.Label(), dropped, narrowed))
		}
	}
	plan, err := planArchiveRemap(dest, declarations.ReleasesDir(name), run.commitMap, dep.translatePath)
	if err != nil {
		return err
	}
	if run.remaps, err = writeArchiveRemap(e, target, declarations.ReleasesDir(name), plan); err != nil {
		return err
	}
	for _, left := range plan.left {
		run.notes = append(run.notes, "release commit left as recorded: "+left)
	}
	dep.req.Say(fmt.Sprintf("  state: %d changelog files and %d release records carried; %d release commits moved.", len(dep.changelogs), len(dep.releaseFiles), len(run.remaps)))
	return nil
}

// applyTags gives the destination's tags the destination's scheme.
func (dep *departure) applyTags(e *strictcli.Effects, run *applied) error {
	dest, err := git.Open(e, dep.req.Target)
	if err != nil {
		return err
	}
	tags, err := dest.TagCommits()
	if err != nil {
		return err
	}
	refs, err := dest.LocalTagRefs()
	if err != nil {
		return err
	}
	for _, t := range dep.tags.translations {
		old, next := t[0], t[1]
		at, ok := tags[old]
		if !ok {
			run.notes = append(run.notes, fmt.Sprintf("the rewrite did not carry the tag %s, so %s was not created", old, next))
			continue
		}
		if existing, ok := tags[next]; ok {
			if existing != at {
				return fmt.Errorf("the tag %s exists at %s in the new repository, and the rename of %s puts it at %s; a tag this conversion did not create is never moved, so resolve it by hand", next, short(existing), old, short(at))
			}
		} else if err := dest.CreateRef("refs/tags/"+next, at); err != nil {
			return err
		}
		run.tagMaps = append(run.tagMaps, releaserecord.TagMapping{OldTag: old, NewTag: next, OldCommit: dep.tags.commits[old], NewCommit: at})
	}
	for _, list := range [][]string{dep.tags.deletions, dep.tags.pruned} {
		for _, tag := range list {
			ref := "refs/tags/" + tag
			if object, ok := refs[ref]; ok {
				if err := dest.DeleteRef(ref, object); err != nil {
					return err
				}
			}
		}
	}
	own := map[string]bool{}
	for _, t := range dep.tags.translations {
		own[t[0]], own[t[1]] = true, true
	}
	for _, tag := range dep.tags.own {
		own[tag] = true
	}
	deleted := map[string]bool{}
	for _, tag := range append(append([]string{}, dep.tags.deletions...), dep.tags.pruned...) {
		deleted[tag] = true
	}
	for _, tag := range sortedKeys(tags) {
		if own[tag] || deleted[tag] {
			continue
		}
		if _, _, ok := workspace.ParseVersionTag(tag); ok {
			run.notes = append(run.notes, fmt.Sprintf("the tag %s is kept: no releasable that stays owns it, so it is most likely this releasable's own history under an earlier name", tag))
		}
	}
	dep.req.Say(fmt.Sprintf("  tags: %d renamed, %d deleted.", len(run.tagMaps), len(dep.tags.deletions)+len(dep.tags.pruned)))
	return nil
}

// applyDestination writes the destination's declarations, records, and
// changelog, commits them, and reads the declarations back.
func (dep *departure) applyDestination(e *strictcli.Effects, run *applied) error {
	target, name := dep.req.Target, dep.releasable.Name
	if _, err := declarations.Write(e, target, declarations.Render(dep.destDecls)); err != nil {
		return err
	}
	if err := dep.destRecord.Write(recordWriter{e}, target); err != nil {
		return err
	}
	if err := releaserecord.AppendEvents(e, target, dep.carried, dep.req.Now); err != nil {
		return err
	}
	if _, err := changelog.Regenerate(e, target, dep.destDecls, name, nil); err != nil {
		return err
	}
	switches, err := depFloorsSwitches(target, dep.destDecls)
	if err != nil {
		return err
	}
	if err := switchOnDepFloors(e, target, dep.destDecls, switches, dep.req.Say); err != nil {
		return err
	}
	dest, err := git.Open(e, target)
	if err != nil {
		return err
	}
	paths, err := dest.ChangedPaths(nil, git.UntrackedNormal)
	if err != nil {
		return err
	}
	if len(paths) > 0 {
		if _, err := dest.Commit(git.CommitRequest{Message: fmt.Sprintf("monorepo: carry the releasable %s over from %s", name, dep.ws.Root), Paths: paths, Autogenerated: true}); err != nil {
			return err
		}
	}
	if left, err := dest.ChangedPaths(nil, git.UntrackedNormal); err != nil {
		return err
	} else if len(left) > 0 {
		return fmt.Errorf("the new repository has uncommitted changes after its commit: %s", strings.Join(left, ", "))
	}
	if run.commit, err = dest.Head(); err != nil {
		return err
	}
	back, err := workspace.Load(target)
	if err != nil {
		return fmt.Errorf("the new repository's declarations do not load: %w", err)
	}
	if _, ok := back.Declarations.Releasable(name); !ok {
		return fmt.Errorf("the new repository does not declare the releasable %q", name)
	}
	return nil
}

// applyRecord writes the destination's transition record: the conversion
// first, then what elaborates it.
func (dep *departure) applyRecord(e *strictcli.Effects, run *applied) error {
	target, rel := dep.req.Target, dep.releasable
	conversion := &releaserecord.ConversionEvent{
		Direction:   "extract",
		Source:      releaserecord.Endpoint{Repo: dep.sourceURL, Releasable: rel.Name, TagFormat: rel.TagFormat},
		Destination: releaserecord.Endpoint{Repo: ".", Releasable: rel.Name, TagFormat: dep.destDecls.Releasables[0].TagFormat},
		Commit:      run.commit,
	}
	if !dep.multi {
		conversion.Source.Path, conversion.Source.Project = dep.members[0].Path, dep.members[0].Name
	}
	if err := releaserecord.AppendEvents(e, target, []releaserecord.Event{conversion}, dep.req.Now); err != nil {
		return err
	}
	related := releaserecord.EventHeader{RelatedTo: conversion.ID}
	var followers []releaserecord.Event
	if len(run.tagMaps) > 0 {
		followers = append(followers, &releaserecord.TagMapEvent{EventHeader: related, Releasable: rel.Name, Mappings: run.tagMaps})
	}
	if len(run.remaps) > 0 {
		followers = append(followers, &releaserecord.ReleaseCommitRemapEvent{EventHeader: related, Releasable: rel.Name, Rewrite: "git-filter-repo --path (monorepo extract)", Mappings: run.remaps})
	}
	if alias := dep.tags.alias; alias[0] != "" {
		dest, err := git.Open(e, target)
		if err != nil {
			return err
		}
		commit, found, err := dest.TagCommit(alias[0])
		if err != nil {
			return err
		}
		if found {
			followers = append(followers, &releaserecord.BoundaryAliasEvent{EventHeader: related, Releasable: rel.Name, Aliases: []releaserecord.BoundaryAlias{{AliasTag: alias[0], AliasedTag: alias[1], Commit: commit}}})
		}
	}
	if err := releaserecord.AppendEvents(e, target, followers, dep.req.Now); err != nil {
		return err
	}
	dest, err := git.Open(e, target)
	if err != nil {
		return err
	}
	if _, err := dest.Commit(git.CommitRequest{Message: "monorepo: record the extract of " + rel.Name, Paths: []string{path.Dir(declarations.TransitionsFile)}, Autogenerated: true, RequireChange: true}); err != nil {
		return err
	}
	dep.req.Say(fmt.Sprintf("  transition record: %d events recorded.", len(followers)+1))
	return nil
}

// applySource removes the releasable from this repository and commits the
// whole edit. A failure before the commit puts every path the step changed
// back as it was.
func (dep *departure) applySource(e *strictcli.Effects, run *applied) error {
	root := dep.ws.Root
	repo, err := git.Open(e, root)
	if err != nil {
		return err
	}
	if dirty, err := dirtyPaths(repo); err != nil {
		return err
	} else if len(dirty) > 0 {
		return fmt.Errorf("the working tree changed while the new repository was built (%s); nothing was written here. %s is complete, but the extract did not finish: commit or remove the changes, delete %s, and run this extract again", strings.Join(dirty, ", "), dep.req.Target, dep.req.Target)
	}
	before, err := snapshotWorkingTree(repo, root)
	if err != nil {
		return err
	}
	if err := dep.editSource(e, repo); err != nil {
		if restoreErr := restoreWorkingTree(e, repo, root, before); restoreErr != nil {
			return fmt.Errorf("%w; putting this repository back failed too: %v", err, restoreErr)
		}
		return fmt.Errorf("%w\nThis repository is back as its last commit holds it: the members, the releasable, and its records are all still here, and nothing was committed. %s is complete, but the extract did not finish: fix what failed, delete %s, and run this extract again", err, dep.req.Target, dep.req.Target)
	}
	if left, err := dirtyPaths(repo); err != nil {
		return err
	} else if len(left) > 0 {
		return fmt.Errorf("this repository has uncommitted changes after the extract's commit: %s. %s is complete; commit or revert them by hand", strings.Join(left, ", "), dep.req.Target)
	}
	dep.req.Say(fmt.Sprintf("  this repository: removed %s and the releasable %q.", strings.Join(dep.departing, ", "), dep.releasable.Name))
	return nil
}

// editSource writes the source's edit, deletes what left, and commits.
func (dep *departure) editSource(e *strictcli.Effects, repo git.Repo) error {
	root, rel := dep.ws.Root, dep.releasable
	event := &releaserecord.DepartedGlobsEvent{
		Globs:       []string{dep.ownScheme.ListGlob()},
		Destination: releaserecord.Endpoint{Repo: dep.req.Target, Releasable: rel.Name, TagFormat: dep.destDecls.Releasables[0].TagFormat},
	}
	if err := releaserecord.AppendEvents(e, root, []releaserecord.Event{event}, dep.req.Now); err != nil {
		return err
	}
	edited, err := declarations.Write(e, root, dep.sourceDecls)
	if err != nil {
		return err
	}
	if err := switchOnDepFloors(e, root, edited, dep.floorSwitches, dep.req.Say); err != nil {
		return err
	}
	if dep.sourceRecord != nil {
		if err := dep.sourceRecord.Write(recordWriter{e}, root); err != nil {
			return err
		}
	}
	remaining, err := workspace.New(root, edited)
	if err != nil {
		return err
	}
	if _, err := dep.req.Sync(remaining); err != nil {
		return fmt.Errorf("regenerating the routers: %w", err)
	}
	if _, err := changelog.RegenerateRollUp(e, root, edited); err != nil {
		return err
	}
	description := fmt.Sprintf("the releasable %q left this repository with `rlsbl monorepo extract`", rel.Name)
	for _, m := range dep.members {
		if err := deletePath(e, root, m.Path, description, dep.req.DeleteWithRm); err != nil {
			return err
		}
	}
	for _, p := range []string{declarations.ChangelogDir(rel.Name), declarations.ReleasesDir(rel.Name), declarations.ChangelogValidationFile(rel.Name), declarations.RunStateDir(rel.Name)} {
		if err := deletePath(e, root, p, description, dep.req.DeleteWithRm); err != nil {
			return err
		}
	}
	paths, err := repo.ChangedPaths(nil, git.UntrackedNormal)
	if err != nil {
		return err
	}
	_, err = repo.Commit(git.CommitRequest{Message: ExtractCommitMessage(rel.Name), Paths: paths, RequireChange: true})
	return err
}

// applyNextSteps prints what is left to the operator.
func (dep *departure) applyNextSteps(run *applied) error {
	for _, note := range run.notes {
		dep.req.Say("Note: " + note)
	}
	dep.req.Say(fmt.Sprintf("Extracted the releasable %q to %s.", dep.releasable.Name, dep.req.Target))
	dep.req.Say("Next steps (rlsbl administers no external system):")
	for _, step := range dep.nextSteps() {
		dep.req.Say("  - " + step)
	}
	return nil
}
