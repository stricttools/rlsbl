package workspace

import (
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The tool-owned paths: rlsbl's own bookkeeping, decided from the path alone
// (no declarations, no git). A tool-owned path needs no changelog owner and
// takes no part in attribution.
//
// The new layout's records sit at the repository root only. The old
// layout's are listed too, at the depths the old layout wrote them, because
// commits made before a repository's migration still carry them and the
// first release after it reads those commits.
var (
	// rootOnlyTrees are directory trees matched at the repository root.
	rootOnlyTrees = []string{
		declarations.ChangelogRoot + "/",
		declarations.ReleasesRoot + "/",
		declarations.BatchReleasesDir + "/",
		strings.TrimSuffix(declarations.TransitionsFile, "/transitions.jsonl") + "/",
		declarations.HistoryRewritesDir + "/",
		declarations.RetiredHistoriesRoot + "/",
		strings.TrimSuffix(declarations.ScaffoldStateFile, "/scaffold-state.toml") + "/",
		declarations.ScaffoldBasesDir + "/",
		declarations.ChangelogValidationDir + "/",
		declarations.ReleaseStateDir + "/",
		oldWorkspaceDir + "/",
	}
	// rootOnlyFiles are files matched at the repository root.
	rootOnlyFiles = []string{
		declarations.PrivateModuleFile,
		".github/workflows/ci-router.yml",
	}
	// anyDepthTrees are old-layout trees matched at any depth.
	anyDepthTrees = []string{
		oldStateDir + "/changes/",
		oldStateDir + "/releases/",
		oldStateDir + "/bases/",
		oldStateDir + "/lint/",
	}
	// anyDepthFiles are files matched at any depth. CHANGELOG.md matches a
	// hand-written file of that name too, knowingly: narrowing it would take
	// the declarations, and these rules are the path alone.
	anyDepthFiles = []string{
		oldStateDir + "/version",
		"CHANGELOG.md",
	}
)

// The old layout's directories: per-member state, and the workspace's.
const (
	oldStateDir     = ".rlsbl"
	oldWorkspaceDir = ".rlsbl-monorepo"
)

// cleanPath folds a git path to the form the rules compare: '/'-separated,
// no leading "./", no trailing '/'. The root is the empty string.
func cleanPath(p string) string {
	p = strings.TrimSpace(strings.ReplaceAll(p, "\\", "/"))
	p = strings.TrimPrefix(p, "./")
	if p == "." {
		return ""
	}
	return strings.TrimRight(p, "/")
}

// ToolOwnedRule is the rule that makes the repository-relative path p
// tool-owned ("CHANGELOG.md", ".strictmetadata/changelog/**"), for a message
// explaining why p needs no owner; empty when p is not tool-owned.
func ToolOwnedRule(p string) string {
	p = cleanPath(p)
	if p == "" {
		return ""
	}
	for _, tree := range rootOnlyTrees {
		if strings.HasPrefix(p, tree) {
			return tree + "**"
		}
	}
	for _, file := range rootOnlyFiles {
		if p == file {
			return file
		}
	}
	parts := strings.Split(p, "/")
	for i := range parts {
		tail := strings.Join(parts[i:], "/")
		for _, tree := range anyDepthTrees {
			if strings.HasPrefix(tail, tree) {
				return tree + "**"
			}
		}
		for _, file := range anyDepthFiles {
			if tail == file {
				return file
			}
		}
	}
	return ""
}

// IsToolOwned reports whether p is rlsbl's own bookkeeping.
func IsToolOwned(p string) bool { return ToolOwnedRule(p) != "" }

// OwnerOf is the one member owning the repository-relative file p, and false
// for a tool-owned path. The most specific member path claiming p wins; the
// root member owns what no other member claims.
func (w *Workspace) OwnerOf(p string) (declarations.Member, bool) {
	p = cleanPath(p)
	if p == "" || IsToolOwned(p) {
		return declarations.Member{}, false
	}
	return w.Declarations.MemberForPath(p)
}

// OwnerNames are the names of the members owning any of files.
func (w *Workspace) OwnerNames(files []string) map[string]bool {
	names := map[string]bool{}
	for _, f := range files {
		if m, ok := w.OwnerOf(f); ok {
			names[m.Name] = true
		}
	}
	return names
}

// AffectedMembers are the members owning at least one of changed, in
// declaration order. A file counts for one member only: a change under a
// nested member affects it and not its parent.
func (w *Workspace) AffectedMembers(changed []string) []declarations.Member {
	owners := w.OwnerNames(changed)
	var out []declarations.Member
	for _, m := range w.Members() {
		if owners[m.Name] {
			out = append(out, m)
		}
	}
	return out
}

// StateDirs are the repository-relative paths holding a releasable's own
// state: its changelog, its release directory, its validation cache, its run
// state, and the old layout's state directory. No member owns them (they are
// tool-owned), so the releasable's scope claims them.
func StateDirs(releasable string) []string {
	return []string{
		declarations.ChangelogDir(releasable),
		declarations.ReleasesDir(releasable),
		declarations.ChangelogValidationFile(releasable),
		declarations.RunStateDir(releasable),
		oldWorkspaceDir + "/releasables/" + releasable,
	}
}

// StateDirReleasable is the releasable whose state path p lies in, and false
// for any other path. It answers "whose is this?" for a path no member owns.
func StateDirReleasable(p string) (string, bool) {
	p = cleanPath(p)
	for _, root := range []string{
		declarations.ChangelogRoot,
		declarations.ReleasesRoot,
		oldWorkspaceDir + "/releasables",
	} {
		if rest, ok := strings.CutPrefix(p, root+"/"); ok {
			name, _, _ := strings.Cut(rest, "/")
			if name != "" {
				return name, true
			}
		}
	}
	// The run state directory also holds files of its own (the lock, the
	// batch plan), so only a path inside a releasable's directory there is
	// that releasable's.
	if rest, ok := strings.CutPrefix(p, declarations.ReleaseStateDir+"/"); ok {
		if name, _, inside := strings.Cut(rest, "/"); inside && name != "" {
			return name, true
		}
	}
	if rest, ok := strings.CutPrefix(p, declarations.ChangelogValidationDir+"/"); ok {
		if name, ok := strings.CutSuffix(rest, ".toml"); ok && name != "" && !strings.Contains(name, "/") {
			return name, true
		}
	}
	return "", false
}

// Scope is a question asked of attribution: which members' files, and which
// state paths, is the asker after? Attribution needs the whole member list
// to answer at all (a file under a nested member belongs to it even when the
// asker cares only about its parent), so a scope carries the workspace
// beside the members in scope.
type Scope struct {
	w         *Workspace
	owned     map[string]bool
	stateDirs []string
}

// ScopeOfMembers covers the named members' files and no state path.
func (w *Workspace) ScopeOfMembers(members []declarations.Member) Scope {
	owned := map[string]bool{}
	for _, m := range members {
		owned[m.Name] = true
	}
	return Scope{w: w, owned: owned}
}

// ScopeOfMember covers one member's files.
func (w *Workspace) ScopeOfMember(m declarations.Member) Scope {
	return w.ScopeOfMembers([]declarations.Member{m})
}

// ScopeOfReleasable covers a releasable: its members' files and its state
// paths.
func (w *Workspace) ScopeOfReleasable(releasable string) Scope {
	s := w.ScopeOfMembers(w.MembersOf(releasable))
	s.stateDirs = StateDirs(releasable)
	return s
}

// ClaimsStateDir reports whether p lies in a state path the scope claims.
func (s Scope) ClaimsStateDir(p string) bool {
	p = cleanPath(p)
	if p == "" {
		return false
	}
	for _, dir := range s.stateDirs {
		if p == dir || strings.HasPrefix(p, dir+"/") {
			return true
		}
	}
	return false
}

// Claims reports whether p is in scope: owned by a member in scope, or in a
// state path the scope claims. The two cannot disagree, since a state path
// is tool-owned and has no member owner. Being claimed says which scope a
// path belongs to, not whether it needs a changelog entry.
func (s Scope) Claims(p string) bool {
	if s.ClaimsStateDir(p) {
		return true
	}
	m, ok := s.w.OwnerOf(p)
	return ok && s.owned[m.Name]
}

// ClaimsAny reports whether any of files is in scope.
func (s Scope) ClaimsAny(files []string) bool {
	for _, f := range files {
		if s.Claims(f) {
			return true
		}
	}
	return false
}

// Members are the members in scope, in declaration order.
func (s Scope) Members() []declarations.Member {
	var out []declarations.Member
	for _, m := range s.w.Members() {
		if s.owned[m.Name] {
			out = append(out, m)
		}
	}
	return out
}

// Describe names the scope for a message: its members, sorted, and the
// state paths it claims, which belong to no member, so a description of the
// members alone would read as if they were outside it.
func (s Scope) Describe() string {
	names := make([]string, 0, len(s.owned))
	for name := range s.owned {
		names = append(names, name)
	}
	sort.Strings(names)
	described := "(no members)"
	if len(names) > 0 {
		described = strings.Join(names, ", ")
	}
	if len(s.stateDirs) > 0 {
		dirs := append([]string(nil), s.stateDirs...)
		sort.Strings(dirs)
		described += " (and " + strings.Join(dirs, ", ") + ")"
	}
	return described
}
