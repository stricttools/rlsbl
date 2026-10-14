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

// A retirement takes releasables out of the declarations when the owner
// retires them (`rlsbl transition lifecycle --status retired`): each
// releasable and every member versioned under it leave releasables.toml,
// their release hook directories and the releasables' changelog validation
// caches are deleted, the scaffold state stops managing the members' files,
// and the routers and the roll-up are regenerated once, without all of
// them. Their changelog and release directories stay where they are: the
// retired lifecycle periods mark them as closed history. The lifecycle-and-license record is the
// caller's to change, and the caller commits everything together.

// Retirement is a planned retirement, every refusal already made.
type Retirement struct {
	ws *workspace.Workspace
	// Releasables are the releasables retired, in the order named.
	Releasables []declarations.Releasable
	// Members are the members versioned under the releasables.
	Members []declarations.Member
	// Edited is the declarations without the releasables and their members.
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

// PlanRetirement checks that the releasables names can leave the workspace
// ws together and builds its declarations without them, and derives the
// routers sync would regenerate from them, with the inputs routers gives
// for the workspace without the releasables (AutoCommit is ignored: the
// caller commits). It refuses no names or a repeated name, a name the
// declarations declare no releasable of, a standalone repository's
// releasable (the repository is archived instead), a releasable holding
// the root member, a member that stays depending on one that leaves,
// options entries scoped to a leaving member, a release in progress, and
// routers the workspace without the releasables cannot regenerate.
func PlanRetirement(ws *workspace.Workspace, names []string, inputs func(*workspace.Workspace) (workflows.SyncInputs, error)) (*Retirement, error) {
	if len(names) == 0 {
		return nil, fmt.Errorf("a retirement names at least one releasable")
	}
	named := map[string]bool{}
	var rels []declarations.Releasable
	var members []declarations.Member
	for _, name := range names {
		if named[name] {
			return nil, fmt.Errorf("the releasable %q is named more than once; name each releasable once", name)
		}
		named[name] = true
		rel, ok := ws.Declarations.Releasable(name)
		if !ok {
			var declared []string
			for _, r := range ws.Releasables() {
				declared = append(declared, r.Name)
			}
			sort.Strings(declared)
			return nil, fmt.Errorf("%q is not a releasable the declarations declare; the releasables are %s", name, strings.Join(declared, ", "))
		}
		if !ws.IsWorkspace() {
			return nil, fmt.Errorf("%q is the releasable of this standalone repository, and a standalone repository is not left without its releasable: when its releasable is finished, the whole repository is archived (on GitHub, outside rlsbl) with its records as they are; nothing was written", name)
		}
		for _, m := range ws.MembersOf(name) {
			if m.IsRoot() {
				return nil, fmt.Errorf("the releasable %q holds the root member %q (path \".\"), which owns every file no other member claims and which every repository declares, so retiring %q cannot take it out of the declarations. Make the root member a dev node (releasable = false and dev_only = true in its [[members]] table in %s), deleting the releasable's [[releasables]] table in the same edit when the root member is its only member, then run this again; nothing was written", name, m.Name, name, declarations.ReleasablesFile)
			}
			members = append(members, m)
		}
		rels = append(rels, rel)
	}
	what := retiredPhrase(names)
	if err := refuseReleaseInFlight(ws.Root); err != nil {
		return nil, err
	}
	g, err := completeGraph(ws, "the retirement")
	if err != nil {
		return nil, err
	}
	if problems := stayingDependents(ws, g, members); len(problems) > 0 {
		return nil, fmt.Errorf("members that stay depend on members of %s, which would leave the declarations:\n%s\nSever each dependency first (retiring never rewrites a manifest), then run this again; nothing was written", what, strings.Join(problems, "\n"))
	}
	var paths []string
	for _, m := range members {
		paths = append(paths, m.Path)
	}
	if scoped, err := scopedEntriesIn(ws.Root, paths); err != nil {
		return nil, err
	} else if len(scoped) > 0 {
		return nil, fmt.Errorf("options entries are scoped to a member of %s, and once the member is no longer declared every check run and release refuses them. Delete each entry from its document, then run this again; nothing was written:\n  %s", what, strings.Join(scoped, "\n  "))
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
	for _, name := range names {
		if err := ed.RemoveReleasable(name); err != nil {
			return nil, err
		}
	}
	edited, parsed, err := ed.Result()
	if err != nil {
		return nil, fmt.Errorf("the declarations without %s would be refused, so nothing was written: %w", what, err)
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
		return nil, fmt.Errorf("retiring %s regenerates the workspace's routers without %s, and they cannot be regenerated, so nothing was written: %w", what, pronoun(names), err)
	}
	return &Retirement{ws: ws, Releasables: rels, Members: members, Edited: parsed, editedText: edited, remaining: remaining, routers: routers}, nil
}

// retiredPhrase names the releasables names in a sentence: "the
// releasable \"a\"" for one, "the releasables \"a\" and \"b\"" for more.
func retiredPhrase(names []string) string {
	quoted := make([]string, len(names))
	for i, n := range names {
		quoted[i] = fmt.Sprintf("%q", n)
	}
	if len(names) == 1 {
		return "the releasable " + quoted[0]
	}
	return "the releasables " + JoinWords(quoted)
}

// pronoun is "it" for one name and "them" for more.
func pronoun(names []string) string {
	if len(names) == 1 {
		return "it"
	}
	return "them"
}

// JoinWords joins words as an English list: "a", "a and b", "a, b, and c".
func JoinWords(words []string) string {
	switch len(words) {
	case 0:
		return ""
	case 1:
		return words[0]
	case 2:
		return words[0] + " and " + words[1]
	}
	return strings.Join(words[:len(words)-1], ", ") + ", and " + words[len(words)-1]
}

// Names are the retired releasables' names, in the order named.
func (r *Retirement) Names() []string {
	out := make([]string, len(r.Releasables))
	for i, rel := range r.Releasables {
		out[i] = rel.Name
	}
	return out
}

// Subjects are the releasables' names and their members' names: the
// subjects of the lifecycle-and-license record the retirement retires.
func (r *Retirement) Subjects() []string {
	out := r.Names()
	seen := map[string]bool{}
	for _, n := range out {
		seen[n] = true
	}
	for _, m := range r.Members {
		if !seen[m.Name] {
			seen[m.Name] = true
			out = append(out, m.Name)
		}
	}
	return out
}

// DeclaredSubjects are the releasable and member names the declarations
// hold once the releasables left them.
func (r *Retirement) DeclaredSubjects() []string { return r.Edited.SubjectNames() }

// RetireResult is what a retirement wrote and deleted, repository-relative.
type RetireResult struct {
	Written []string
	Removed []string
}

// Apply writes the declarations without the releasables, deletes the release
// hook directories and the changelog validation caches, drops the members'
// files from the scaffold state with their merge bases, regenerates the
// routers through sync (which commits nothing) and the roll-up.
func (r *Retirement) Apply(e *strictcli.Effects) (RetireResult, error) {
	root := r.ws.Root
	var res RetireResult
	if _, err := declarations.Write(e, root, r.editedText); err != nil {
		return res, err
	}
	res.Written = append(res.Written, declarations.ReleasablesFile)
	why := fmt.Sprintf("rlsbl transition lifecycle: %s retired and no longer declared", isAre(retiredPhrase(r.Names()), len(r.Releasables)))
	var remove []string
	for _, owner := range r.Subjects() {
		remove = append(remove, declarations.ReleaseHooksDir(owner))
	}
	for _, name := range r.Names() {
		remove = append(remove, declarations.ChangelogValidationFile(name))
	}
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

// isAre is subject followed by "is" for one and "are" for more.
func isAre(subject string, n int) string {
	if n == 1 {
		return subject + " is"
	}
	return subject + " are"
}
