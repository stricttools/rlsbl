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

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/saferm"
	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A retirement takes a releasable out of the declarations when the owner
// retires it (`rlsbl transition lifecycle --status retired`): the releasable
// and every member versioned under it leave releasables.toml, their release
// hook directories and the releasable's changelog validation cache are
// deleted, the scaffold state stops managing the members' files, and the
// routers and the roll-up are regenerated without them. Its changelog and
// release directories stay where they are: the retired lifecycle period
// marks them as closed history. The lifecycle-and-license record is the
// caller's to change, and the caller commits everything together.

// Retirement is a planned retirement, every refusal already made.
type Retirement struct {
	ws         *workspace.Workspace
	Releasable declarations.Releasable
	// Members are the members versioned under the releasable.
	Members []declarations.Member
	// Edited is the declarations without the releasable and its members.
	Edited     *declarations.Releasables
	editedText []byte
	remaining  *workspace.Workspace
	routers    workflows.SyncInputs
}

// RetireTouches are the repository-relative paths a retirement may write or
// delete besides the members' own published workflows: an uncommitted change
// to one of them is refused before anything is written, and the commit
// carries what changed under them.
var RetireTouches = []string{
	declarations.ReleasablesDir,
	declarations.ReleaseHooksRoot,
	declarations.ChangelogValidationDir,
	path.Dir(declarations.ScaffoldStateFile),
	declarations.ScaffoldBasesDir,
	workflows.Dir,
	"CHANGELOG.md",
}

// PlanRetirement checks that the releasable name can leave the workspace ws
// and builds its declarations without it, and derives the routers sync
// would regenerate from them, with the inputs routers gives for the
// workspace without the releasable (AutoCommit is ignored: the caller
// commits). It refuses a standalone repository's releasable (the
// repository is archived instead), a releasable holding the root member, a
// member that stays depending on one that leaves, options entries scoped to
// a leaving member, a release in progress, and routers the workspace
// without the releasable cannot regenerate.
func PlanRetirement(ws *workspace.Workspace, name string, inputs func(*workspace.Workspace) (workflows.SyncInputs, error)) (*Retirement, error) {
	rel, ok := ws.Declarations.Releasable(name)
	if !ok {
		return nil, fmt.Errorf("%q is not a releasable the declarations declare", name)
	}
	if !ws.IsWorkspace() {
		return nil, fmt.Errorf("%q is the releasable of this standalone repository, and a standalone repository is not left without its releasable: when its releasable is finished, the whole repository is archived (on GitHub, outside rlsbl) with its records as they are; nothing was written", name)
	}
	members := ws.MembersOf(name)
	for _, m := range members {
		if m.IsRoot() {
			return nil, fmt.Errorf("the releasable %q holds the root member %q (path \".\"), which owns every file no other member claims and which every repository declares, so retiring %q cannot take it out of the declarations. Make the root member a dev node (releasable = false and dev_only = true in its [[members]] table in %s), deleting the releasable's [[releasables]] table in the same edit when the root member is its only member, then run this again; nothing was written", name, m.Name, name, declarations.ReleasablesFile)
		}
	}
	if err := refuseReleaseInFlight(ws.Root); err != nil {
		return nil, err
	}
	g, err := completeGraph(ws, "the retirement")
	if err != nil {
		return nil, err
	}
	if problems := stayingDependents(ws, g, members); len(problems) > 0 {
		return nil, fmt.Errorf("members that stay depend on members of the releasable %q, which would leave the declarations:\n%s\nSever each dependency first (retiring never rewrites a manifest), then run this again; nothing was written", name, strings.Join(problems, "\n"))
	}
	var paths []string
	for _, m := range members {
		paths = append(paths, m.Path)
	}
	if scoped, err := scopedEntriesIn(ws.Root, paths); err != nil {
		return nil, err
	} else if len(scoped) > 0 {
		return nil, fmt.Errorf("options entries are scoped to a member of the releasable %q, and once the member is no longer declared every check run and release refuses them. Delete each entry from its document, then run this again; nothing was written:\n  %s", name, strings.Join(scoped, "\n  "))
	}

	data, err := os.ReadFile(filepath.Join(ws.Root, filepath.FromSlash(declarations.ReleasablesFile)))
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", declarations.ReleasablesFile, err)
	}
	ed, err := declarations.NewEditor(data)
	if err != nil {
		return nil, err
	}
	for _, m := range members {
		if err := ed.RemoveMember(m.Path); err != nil {
			return nil, err
		}
	}
	if err := ed.RemoveReleasable(name); err != nil {
		return nil, err
	}
	edited, parsed, err := ed.Result()
	if err != nil {
		return nil, fmt.Errorf("the declarations without the releasable %q would be refused, so nothing was written: %w", name, err)
	}
	remaining, err := workspace.New(ws.Root, parsed)
	if err != nil {
		return nil, err
	}
	routers, err := inputs(remaining)
	if err != nil {
		return nil, err
	}
	routers.AutoCommit = false
	if _, _, err := workflows.Expected(remaining, routers); err != nil {
		return nil, fmt.Errorf("retiring %q regenerates the workspace's routers without it, and they cannot be regenerated, so nothing was written: %w", name, err)
	}
	return &Retirement{ws: ws, Releasable: rel, Members: members, Edited: parsed, editedText: edited, remaining: remaining, routers: routers}, nil
}

// Subjects are the releasable's name and its members' names: the subjects
// of the lifecycle-and-license record the retirement retires.
func (r *Retirement) Subjects() []string {
	out := []string{r.Releasable.Name}
	for _, m := range r.Members {
		if m.Name != r.Releasable.Name {
			out = append(out, m.Name)
		}
	}
	return out
}

// DeclaredSubjects are the releasable and member names the declarations
// hold once the releasable left them.
func (r *Retirement) DeclaredSubjects() []string { return r.Edited.SubjectNames() }

// RetireResult is what a retirement wrote and deleted, repository-relative.
type RetireResult struct {
	Written []string
	Removed []string
}

// Apply writes the declarations without the releasable, deletes the release
// hook directories and the changelog validation cache, drops the members'
// files from the scaffold state with their merge bases, regenerates the
// routers through sync (which commits nothing) and the roll-up.
func (r *Retirement) Apply(e *strictcli.Effects) (RetireResult, error) {
	root := r.ws.Root
	var res RetireResult
	if _, err := declarations.Write(e, root, r.editedText); err != nil {
		return res, err
	}
	res.Written = append(res.Written, declarations.ReleasablesFile)
	why := fmt.Sprintf("rlsbl transition lifecycle: the releasable %q is retired and no longer declared", r.Releasable.Name)
	var remove []string
	for _, owner := range r.Subjects() {
		remove = append(remove, declarations.ReleaseHooksDir(owner))
	}
	remove = append(remove, declarations.ChangelogValidationFile(r.Releasable.Name))
	bases, err := r.dropScaffoldState(e, &res)
	if err != nil {
		return res, err
	}
	remove = append(remove, bases...)
	for _, rel := range remove {
		info, err := os.Lstat(filepath.Join(root, filepath.FromSlash(rel)))
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return res, err
		}
		if err := saferm.Delete(e, root, saferm.Request{Path: rel, Description: why, Recursive: info.IsDir()}); err != nil {
			return res, err
		}
		res.Removed = append(res.Removed, rel)
	}
	synced, err := workflows.Sync(e, r.remaining, r.routers)
	if err != nil {
		return res, fmt.Errorf("regenerating the routers: %w", err)
	}
	res.Written = append(res.Written, synced.Written...)
	res.Removed = append(res.Removed, synced.Removed...)
	rollUp, err := changelog.RegenerateRollUp(e, root, r.Edited)
	if err != nil {
		return res, err
	}
	res.Written = append(res.Written, rollUp...)
	return res, nil
}

// dropScaffoldState removes from the scaffold state every managed file a
// leaving member owns, writes the state when it changed, and returns the
// merge bases of those files, which the caller deletes.
func (r *Retirement) dropScaffoldState(e *strictcli.Effects, res *RetireResult) ([]string, error) {
	root := r.ws.Root
	state, found, err := scaffold.ReadState(root)
	if err != nil || !found {
		return nil, err
	}
	leaving := map[string]bool{}
	for _, m := range r.Members {
		leaving[m.Name] = true
	}
	var bases []string
	dropped := 0
	for _, p := range sortedKeys(state.Files) {
		owner, ok := r.ws.OwnerOf(p)
		if !ok || !leaving[owner.Name] {
			continue
		}
		delete(state.Files, p)
		dropped++
		base := scaffold.BasePath(p)
		if _, err := os.Lstat(filepath.Join(root, filepath.FromSlash(base))); err == nil {
			bases = append(bases, base)
		} else if !errors.Is(err, fs.ErrNotExist) {
			return nil, err
		}
	}
	if dropped == 0 {
		return nil, nil
	}
	if err := scaffold.WriteState(e, root, state); err != nil {
		return nil, err
	}
	res.Written = append(res.Written, declarations.ScaffoldStateFile)
	sort.Strings(bases)
	return bases, nil
}
