package monorepo

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/saferm"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The two repository conversions, `monorepo extract` and `monorepo
// absorb`, share what this file holds: the git-filter-repo run that rewrites
// a history and the commit map it leaves, the clone a rewrite works on, the
// move of release commits and changelog commit ids through that map, the
// deletions they make (saferm, or a plain removal the operator asked for),
// and the options entries a moved set of dependency floors needs.

// filterRepoProgram is the program both conversions rewrite history with.
const filterRepoProgram = "git-filter-repo"

// The bounds of a conversion's long git work: a clone or merge of a whole
// repository, and a rewrite of every commit of a history.
const (
	conversionGitTimeout = 10 * time.Minute
	filterRepoTimeout    = time.Hour
)

// DepFloorsReason is the reason of the rlsbl:dep-floors entries a
// conversion writes for the members whose internal_dep_floors it declared.
const DepFloorsReason = "a monorepo conversion declared this member's internal_dep_floors, and dep-floors polices those floors"

// requireFilterRepo refuses when git-filter-repo is not on PATH.
func requireFilterRepo() error {
	if _, err := exec.LookPath(filterRepoProgram); err != nil {
		return errors.New("git-filter-repo is not on PATH, and the conversion rewrites the history it carries with it. Install it (https://github.com/newren/git-filter-repo#how-do-i-install-it) and run this again")
	}
	return nil
}

// requireSaferm refuses when the conversion has paths to delete, saferm is
// not on PATH, and the operator did not ask for a plain removal.
func requireSaferm(deleteWithRm bool, what string) error {
	if deleteWithRm {
		return nil
	}
	if _, err := exec.LookPath("saferm"); err != nil {
		return fmt.Errorf("saferm is not on PATH, and %s has to be deleted. Install saferm (a deletion through it keeps an audit trail and can be undone), or pass --delete-with-rm to delete with a plain removal instead", what)
	}
	return nil
}

// deletePath deletes the path rel under root: through saferm, or with a
// plain recursive removal when deleteWithRm. A missing path is not an
// error.
func deletePath(e *strictcli.Effects, root, rel, description string, deleteWithRm bool) error {
	abs := filepath.Join(root, filepath.FromSlash(rel))
	info, err := os.Lstat(abs)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("reading %s: %w", rel, err)
	}
	if deleteWithRm {
		if _, err := e.Remove(abs); err != nil {
			return fmt.Errorf("removing %s: %w", rel, err)
		}
		return nil
	}
	return saferm.Delete(e, root, saferm.Request{Path: rel, Description: description, Recursive: info.IsDir(), SkipMissing: true})
}

// runFilterRepo runs git-filter-repo with args in the repository dir. A
// failure is an error carrying what git-filter-repo printed.
func runFilterRepo(e *strictcli.Effects, dir string, args ...string) error {
	argv := []interface{}{filterRepoProgram}
	for _, a := range args {
		argv = append(argv, a)
	}
	c, err := e.Run(argv, strictcli.Cwd(dir), strictcli.Check(false), strictcli.Timeout(filterRepoTimeout))
	if err != nil {
		return fmt.Errorf("git-filter-repo %s in %s: %w", strings.Join(args, " "), dir, err)
	}
	if c.ExitCode() != 0 {
		detail := strings.TrimSpace(c.Stderr())
		if detail == "" {
			detail = strings.TrimSpace(c.Stdout())
		}
		return fmt.Errorf("git-filter-repo %s in %s exited %d: %s", strings.Join(args, " "), dir, c.ExitCode(), detail)
	}
	return nil
}

// runGit runs a git command that writes, in dir, failing on a non-zero
// exit with what git printed.
func runGit(e *strictcli.Effects, dir string, args ...string) error {
	argv := []interface{}{"git"}
	for _, a := range args {
		argv = append(argv, a)
	}
	c, err := e.Run(argv, strictcli.Cwd(dir), strictcli.Check(false), strictcli.Timeout(conversionGitTimeout))
	if err != nil {
		return fmt.Errorf("git %s in %s: %w", strings.Join(args, " "), dir, err)
	}
	if c.ExitCode() != 0 {
		detail := strings.TrimSpace(c.Stderr())
		if detail == "" {
			detail = strings.TrimSpace(c.Stdout())
		}
		return fmt.Errorf("git %s in %s exited %d: %s", strings.Join(args, " "), dir, c.ExitCode(), detail)
	}
	return nil
}

// cloneForRewrite clones the repository at from into to (both absolute)
// without sharing objects, so the rewrite of the clone cannot reach the
// original, and gives the clone the committer identity from has, which a
// clone does not carry over.
func cloneForRewrite(e *strictcli.Effects, from, to string) error {
	if err := runGit(e, from, "clone", "--quiet", "--no-local", "--", from, to); err != nil {
		return err
	}
	for _, key := range []string{"user.name", "user.email"} {
		c, err := e.Run([]interface{}{"git", "config", "--get", key}, strictcli.Cwd(from), strictcli.Check(false), strictcli.Timeout(time.Minute))
		if err != nil {
			return err
		}
		value := strings.TrimSpace(c.Stdout())
		if c.ExitCode() != 0 || value == "" {
			continue
		}
		if err := runGit(e, to, "config", key, value); err != nil {
			return err
		}
	}
	return nil
}

// commitMapFile is where git-filter-repo leaves its commit map in the
// repository it rewrote.
const commitMapFile = ".git/filter-repo/commit-map"

// readCommitMap reads the commit map git-filter-repo left in the repository
// dir: every old commit's new one, and the old commits it pruned (mapped to
// the null id).
func readCommitMap(dir string) (map[string]string, []string, error) {
	data, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(commitMapFile)))
	if err != nil {
		return nil, nil, fmt.Errorf("reading git-filter-repo's commit map: %w", err)
	}
	return parseCommitMap(string(data))
}

// parseCommitMap reads a git-filter-repo commit map: a header line "old
// new", then one "<old> <new>" line per commit, the null id as new for a
// commit the rewrite pruned. Any other line is refused with its number.
func parseCommitMap(text string) (map[string]string, []string, error) {
	mapping := map[string]string{}
	var pruned []string
	first := true
	for i, line := range strings.Split(text, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		if first {
			first = false
			if len(fields) == 2 && fields[0] == "old" && fields[1] == "new" {
				continue
			}
		}
		if len(fields) != 2 || !git.IsObjectID(fields[0]) || !git.IsObjectID(fields[1]) {
			return nil, nil, fmt.Errorf("line %d of git-filter-repo's commit map is not \"<old commit id> <new commit id>\": %q", i+1, strings.TrimSpace(line))
		}
		if git.IsNullObjectID(fields[1]) {
			pruned = append(pruned, fields[0])
			continue
		}
		mapping[fields[0]] = fields[1]
	}
	sort.Strings(pruned)
	return mapping, pruned, nil
}

// dirtyPaths are the working tree's changes, the run state left out: the
// run-state directory is rlsbl's own, ignored by its own .gitignore, and
// taking the lock writes that .gitignore when it is missing.
func dirtyPaths(repo git.Repo) ([]string, error) {
	changed, err := repo.ChangedPaths(nil, git.UntrackedNormal)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, p := range changed {
		if declarations.IsInside(strings.TrimSuffix(p, "/"), declarations.ReleaseStateDir) {
			continue
		}
		out = append(out, p)
	}
	return out, nil
}

// copyFile copies the file at the absolute path from to the absolute path
// to, keeping its mode, through the effects handle.
func copyFile(e *strictcli.Effects, from, to string) error {
	info, err := os.Stat(from)
	if err != nil {
		return fmt.Errorf("reading %s: %w", from, err)
	}
	data, err := os.ReadFile(from)
	if err != nil {
		return fmt.Errorf("reading %s: %w", from, err)
	}
	if _, err := e.Mkdir(filepath.Dir(to)); err != nil {
		return fmt.Errorf("creating the directory of %s: %w", to, err)
	}
	if _, err := e.Write(to, data, strictcli.Mode(info.Mode().Perm())); err != nil {
		return fmt.Errorf("writing %s: %w", to, err)
	}
	return nil
}

// filesIn are the names of the regular files directly in the
// repository-relative dir, sorted; none when it does not exist.
func filesIn(root, dir string) ([]string, error) {
	entries, err := os.ReadDir(filepath.Join(root, filepath.FromSlash(dir)))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", dir, err)
	}
	var names []string
	for _, entry := range entries {
		if entry.Type().IsRegular() {
			names = append(names, entry.Name())
		} else if entry.IsDir() {
			return nil, fmt.Errorf("%s holds the directory %s, and a releasable's record directory holds files only; move it out before converting", dir, entry.Name())
		}
	}
	sort.Strings(names)
	return names, nil
}

// archiveMove is one archive's release commit moved through a rewrite.
type archiveMove struct {
	version semver.Version
	old     string
	next    string
	trees   map[string]string
}

// archiveRemap is what moving a directory's release commits found.
type archiveRemap struct {
	moves []archiveMove
	// left are the archives whose release commit stays as recorded, each
	// with why: the rewrite did not carry the commit, or a released path has
	// no tree at the new commit.
	left []string
}

// planArchiveRemap plans the move of every release commit the archives in
// dir (repository-relative, in repo) record through commitMap, each
// released path spelled as translate gives it at the new commit. Every
// recorded tree is computed again at the new commit and must be the tree
// recorded: a tree object is content-addressed, so a faithful rewrite
// reproduces it, and a difference means the content of a released version
// changed under the rewrite, which is refused naming both trees.
func planArchiveRemap(repo git.Repo, dir string, commitMap map[string]string, translate func(string) string) (archiveRemap, error) {
	var out archiveRemap
	root := repo.Dir()
	versions, err := releaserecord.ArchivedVersions(root, dir)
	if err != nil {
		return out, err
	}
	for _, v := range versions {
		a, err := releaserecord.ReadArchive(root, dir, v)
		if err != nil {
			return out, err
		}
		if a.Fate != releaserecord.FateRecorded {
			continue
		}
		old := a.ReleaseCommit.Commit
		next, ambiguous := changelog.MapCommit(old, commitMap)
		if ambiguous {
			return out, fmt.Errorf("the release commit %s of %s is the prefix of more than one commit the rewrite mapped, so which commit it moved to cannot be decided; record the full commit id in %s and run this again", old, v, a.Path)
		}
		if next == "" {
			out.left = append(out.left, fmt.Sprintf("%s: its release commit %s is not a commit the rewrite carried", v, short(old)))
			continue
		}
		move := archiveMove{version: v, old: old, next: next, trees: map[string]string{}}
		var paths []string
		for p := range a.ReleaseCommit.Trees {
			paths = append(paths, p)
		}
		sort.Strings(paths)
		missing := ""
		for _, p := range paths {
			np := translate(p)
			found, ok, err := repo.TreeAt(next, np)
			if err != nil {
				return out, err
			}
			if !ok {
				missing = p
				break
			}
			if recorded := a.ReleaseCommit.Trees[p]; found != recorded {
				return out, fmt.Errorf("the release commit of %s does not carry over: %s records the tree %s for %q, and %q at the rewritten commit %s is the tree %s. A tree object is content-addressed, so a faithful rewrite reproduces it; the content of that released version is not the content that moved. Nothing has been written to the repository the releasable came from; investigate before running this again", v, a.Path, recorded, p, np, next, found)
			}
			move.trees[np] = found
		}
		if missing != "" {
			out.left = append(out.left, fmt.Sprintf("%s: its released path %q has no tree at the rewritten commit %s", v, missing, short(next)))
			continue
		}
		out.moves = append(out.moves, move)
	}
	return out, nil
}

// writeArchiveRemap records each planned move into its archive and returns
// the commit correspondence for the transition record.
func writeArchiveRemap(e *strictcli.Effects, root, dir string, plan archiveRemap) ([]releaserecord.CommitMapping, error) {
	var mappings []releaserecord.CommitMapping
	for _, m := range plan.moves {
		if err := releaserecord.WriteReleaseCommit(e, root, dir, m.version, releaserecord.ReleaseCommit{Commit: m.next, Trees: m.trees}); err != nil {
			return nil, err
		}
		mappings = append(mappings, releaserecord.CommitMapping{OldSHA: m.old, NewSHA: m.next})
	}
	return mappings, nil
}

// entryNarrowing is what carrying one changelog file through a rewrite did
// to its entries.
type entryNarrowing struct {
	file string
	// dropped are entries none of whose commits the rewrite carried;
	// narrowed those that lost some.
	dropped, narrowed int
}

// carryEntries maps every commit of entries through mapCommit, which
// answers the commit's id in the repository the entries move to, or ""
// when the rewrite did not carry it. An entry keeps the commits that map;
// one none of whose commits map is dropped.
func carryEntries(entries []changelog.Entry, mapCommit func(string) (string, error)) ([]changelog.Entry, int, int, error) {
	var kept []changelog.Entry
	dropped, narrowed := 0, 0
	for _, entry := range entries {
		var commits []string
		for _, h := range entry.Commits {
			next, err := mapCommit(h)
			if err != nil {
				return nil, 0, 0, err
			}
			if next != "" {
				commits = append(commits, next)
			}
		}
		switch {
		case len(commits) == 0:
			dropped++
			continue
		case len(commits) < len(entry.Commits):
			narrowed++
		}
		entry.Commits = commits
		kept = append(kept, entry)
	}
	return kept, dropped, narrowed, nil
}

// pathsUnder are the tracked files of repo lying in one of the
// repository-relative dirs, for a commit naming the deletion of those dirs.
func pathsUnder(tracked []string, dirs ...string) []string {
	var out []string
	for _, f := range tracked {
		for _, d := range dirs {
			if declarations.IsInside(f, d) {
				out = append(out, f)
				break
			}
		}
	}
	return out
}

// scopedEntriesIn are the options entries of every tool, in the options
// directory at the repository root, whose scope lies in one of the
// repository-relative member paths: entries an option resolver would
// refuse once those members are no longer declared there.
func scopedEntriesIn(root string, memberPaths []string) ([]string, error) {
	loaded, err := strictspec.LoadOptionsEntries(root)
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", options.Dir, err)
	}
	var found []string
	for _, entry := range loaded.Entries {
		if !entry.HasScope {
			continue
		}
		for _, p := range memberPaths {
			if declarations.IsInside(entry.Scope, p) {
				found = append(found, fmt.Sprintf("%s/%s: entry[%d] %s (scope %q)", options.Dir, entry.File, entry.Index, entry.ID, entry.Scope))
				break
			}
		}
	}
	return found, nil
}

// depFloorsSwitch is one member whose rlsbl:dep-floors a conversion
// switches on, because it declared the member's internal_dep_floors.
type depFloorsSwitch struct {
	member string
	path   string
	scope  string
}

// depFloorsSwitches are the members of d declaring internal_dep_floors
// whose rlsbl:dep-floors is off in the repository at root (absolute).
func depFloorsSwitches(root string, d *declarations.Releasables) ([]depFloorsSwitch, error) {
	reg, err := options.Shipped()
	if err != nil {
		return nil, err
	}
	loaded, err := options.Load(reg, root, d)
	if err != nil {
		return nil, err
	}
	var out []depFloorsSwitch
	for _, m := range d.Members {
		if len(m.InternalDepFloors) == 0 {
			continue
		}
		off, err := loaded.IsOff(options.DepFloors, m.Path)
		if err != nil {
			return nil, err
		}
		if off {
			out = append(out, depFloorsSwitch{member: m.Name, path: m.Path, scope: options.MemberScope(d, m.Path)})
		}
	}
	return out, nil
}

// switchOnDepFloors writes an rlsbl:dep-floors entry (current and ideal
// error) for each member, against the declarations d now written at root.
func switchOnDepFloors(e *strictcli.Effects, root string, d *declarations.Releasables, switches []depFloorsSwitch, say func(string)) error {
	if len(switches) == 0 {
		return nil
	}
	reg, err := options.Shipped()
	if err != nil {
		return err
	}
	for _, s := range switches {
		result, err := options.Set(e, reg, root, d, options.SetRequest{
			ID: options.Prefix + options.DepFloors, Current: "error", Ideal: "error", Reason: DepFloorsReason, Scope: s.scope,
		})
		if err != nil {
			return err
		}
		say(fmt.Sprintf("  %s%s: switched on for %s in %s", options.Prefix, options.DepFloors, s.member, result.File))
	}
	return nil
}

// releasedVersions are the versions a releasable's record in the
// repository at root says it released or archived: its changelog's released
// files and its archives.
func releasedVersions(root, releasable string) (map[string]bool, error) {
	out := map[string]bool{}
	logged, err := changelog.Versions(root, declarations.ChangelogDir(releasable))
	if err != nil {
		return nil, err
	}
	for _, v := range logged {
		out[v.String()] = true
	}
	archived, err := releaserecord.ArchivedVersions(root, declarations.ReleasesDir(releasable))
	if err != nil {
		return nil, err
	}
	for _, v := range archived {
		out[v.String()] = true
	}
	return out, nil
}

// short is a commit id cut to 12 characters for a message.
func short(sha string) string {
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}

// relativeTo is the repository-relative path p moved from under the member
// path from to under the member path to ("." is the repository root).
func relativeTo(p, from, to string) string {
	var rest string
	switch {
	case from == declarations.RootPath:
		rest = p
	case p == from:
		rest = declarations.RootPath
	case strings.HasPrefix(p, from+"/"):
		rest = strings.TrimPrefix(p, from+"/")
	default:
		return p
	}
	if rest == declarations.RootPath || rest == "" {
		return to
	}
	return declarations.Join(to, rest)
}

// schemeTags are the tags of the map whose names the scheme owns, by
// version.
func schemeTags(tags map[string]string, scheme workspace.TagScheme) map[string]string {
	out := map[string]string{}
	for name := range tags {
		if v, ok := scheme.VersionOf(name); ok {
			out[v.String()] = name
		}
	}
	return out
}

// sortedKeys are a map's keys in order.
func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// joinOr is the list joined with ", ", or none when it is empty.
func joinOr(list []string, none string) string {
	if len(list) == 0 {
		return none
	}
	return strings.Join(list, ", ")
}

// slashJoin joins a repository-relative directory and a name.
func slashJoin(dir, name string) string { return path.Join(dir, name) }
