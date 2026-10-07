package release

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A release re-locks the lockfiles beside every version it writes, so the
// release commit carries lockfiles that match it.

// lockfileSpec is one lockfile a release re-locks: its name, the tool, the
// command, and the file that must sit beside it for the spec to apply (a
// go.work.sum belongs to a workspace's go.work, not to a module).
type lockfileSpec struct {
	name  string
	tool  string
	argv  []string
	guard string
}

// The re-lock commands of Go modules and workspaces.
var (
	goTidy     = []string{"go", "mod", "tidy"}
	goTidyDiff = []string{"go", "mod", "tidy", "-diff"}
	goWorkSync = []string{"go", "work", "sync"}
)

var lockfileSpecs = []lockfileSpec{
	{name: dependencies.UvLockFile, tool: "uv", argv: []string{"uv", "lock"}},
	{name: dependencies.PackageLockFile, tool: "npm", argv: dependencies.NpmRelockArgv},
	{name: "go.sum", tool: "go", argv: goTidy},
	{name: "go.work.sum", tool: "go", argv: goWorkSync, guard: gomodule.WorkFileName},
}

// lockfileSyncTimeout bounds one re-lock.
const lockfileSyncTimeout = 30 * time.Second

// LockfileSync is one re-lock a release owes.
type LockfileSync struct {
	// Dir is where it runs, repository-relative ("." for the root).
	Dir string
	// Lockfile is the lockfile, repository-relative.
	Lockfile string
	Argv     []string
	Timeout  time.Duration
}

// lockfileDirs are the directories whose lockfiles a release of the
// releasable refreshes: the target directories of every member whose
// versions it writes, and the repository root in a workspace.
func lockfileDirs(root string, w *workspace.Workspace, releasable string) ([]string, error) {
	seen := map[string]bool{}
	var dirs []string
	add := func(d string) {
		if !seen[d] {
			seen[d] = true
			dirs = append(dirs, d)
		}
	}
	members, err := versionedMembers(w, releasable)
	if err != nil {
		return nil, err
	}
	for _, m := range members {
		ts, err := targets.MemberTargets(root, m)
		if err != nil {
			return nil, err
		}
		for _, t := range ts {
			add(m.TargetDir(t))
		}
	}
	if w.IsWorkspace() {
		add(declarations.RootPath)
	}
	return dirs, nil
}

// versionedMembers are the members whose manifests a release of the
// releasable writes its version into: in a standalone repository the root
// member; in a workspace every member of the releasable unless it publishes
// nothing, whose version lives in its version file alone.
func versionedMembers(w *workspace.Workspace, releasable string) ([]declarations.Member, error) {
	r, ok := w.Declarations.Releasable(releasable)
	if !ok {
		return nil, fmt.Errorf("no releasable %q is declared in %s", releasable, declarations.ReleasablesFile)
	}
	if !w.IsWorkspace() {
		return []declarations.Member{w.RootMember()}, nil
	}
	if r.PublishMode == declarations.PublishNone {
		return nil, nil
	}
	return w.MembersOf(releasable), nil
}

// LockfileToolMissingError refuses a release owing a re-lock whose tool is
// not on PATH: skipping it would put a stale lockfile in the release commit.
type LockfileToolMissingError struct {
	Tool, Lockfile, Command, Dir string
}

func (e *LockfileToolMissingError) Error() string {
	return fmt.Sprintf("`%s` is not on PATH, so the release cannot re-lock %s after the version bump (`%s` in %s), and the release commit would carry it stale. Install %s (or put it on PATH), then run the release again", e.Tool, e.Lockfile, e.Command, e.Dir, e.Tool)
}

// OwedLockfileSyncs are the re-locks a release of the releasable owes in the
// repository at root: each lockfile that exists beside a directory whose
// version the release writes (lockfileDirs), its guard file present, and
// that git does not ignore; and, in a workspace, the uv.lock of every member
// versioned under no releasable that locks one of those directories as a uv
// path source, which the bump makes stale and nothing else refreshes before
// CI reads it. A lockfile whose tool is missing refuses the release
// (*LockfileToolMissingError). Every question is asked here, before the
// release writes anything.
func OwedLockfileSyncs(repo git.Repo, w *workspace.Workspace, releasable string) ([]LockfileSync, error) {
	root := repo.Dir()
	dirs, err := lockfileDirs(root, w, releasable)
	if err != nil {
		return nil, err
	}
	var syncs []LockfileSync
	for _, dir := range dirs {
		owed, err := owedIn(repo, dir, lockfileSpecs)
		if err != nil {
			return nil, err
		}
		syncs = append(syncs, owed...)
	}
	if !w.IsWorkspace() {
		return syncs, nil
	}
	bumped := map[string]bool{}
	for _, d := range dirs {
		bumped[filepath.Join(w.Root, filepath.FromSlash(d))] = true
	}
	owedAlready := map[string]bool{}
	for _, s := range syncs {
		owedAlready[s.Lockfile] = true
	}
	for _, m := range w.Members() {
		if m.Versioned() {
			continue
		}
		lock := filepath.Join(w.MemberDir(m), dependencies.UvLockFile)
		sources, found, err := uvLockPathSources(lock)
		if err != nil {
			return nil, err
		}
		if !found {
			continue
		}
		stale := false
		for _, s := range sources {
			if bumped[s] {
				stale = true
			}
		}
		if !stale {
			continue
		}
		owed, err := owedIn(repo, m.Path, specsNamed(dependencies.UvLockFile))
		if err != nil {
			return nil, err
		}
		for _, s := range owed {
			if !owedAlready[s.Lockfile] {
				owedAlready[s.Lockfile] = true
				syncs = append(syncs, s)
			}
		}
	}
	return syncs, nil
}

// specsNamed are the lockfile specs of the named lockfiles.
func specsNamed(names ...string) []lockfileSpec {
	var out []lockfileSpec
	for _, s := range lockfileSpecs {
		for _, n := range names {
			if s.name == n {
				out = append(out, s)
			}
		}
	}
	return out
}

// owedIn are the re-locks the specs owe in the repository-relative dir.
func owedIn(repo git.Repo, dir string, specs []lockfileSpec) ([]LockfileSync, error) {
	abs := filepath.Join(repo.Dir(), filepath.FromSlash(dir))
	var out []LockfileSync
	for _, s := range specs {
		if s.guard != "" {
			if found, err := exists(filepath.Join(abs, s.guard)); err != nil || !found {
				if err != nil {
					return nil, err
				}
				continue
			}
		}
		lockfile := declarations.Join(dir, s.name)
		found, err := exists(filepath.Join(abs, s.name))
		if err != nil {
			return nil, err
		}
		if !found {
			continue
		}
		ignored, err := repo.Ignored(lockfile)
		if err != nil {
			return nil, err
		}
		if ignored {
			continue
		}
		if _, err := exec.LookPath(s.tool); err != nil {
			return nil, &LockfileToolMissingError{Tool: s.tool, Lockfile: lockfile, Command: strings.Join(s.argv, " "), Dir: abs}
		}
		out = append(out, LockfileSync{Dir: dir, Lockfile: lockfile, Argv: append([]string(nil), s.argv...), Timeout: lockfileSyncTimeout})
	}
	return out, nil
}

// exists reports whether the path exists; any other failure is an error.
func exists(path string) (bool, error) {
	_, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

// uvLockPathSources are the directories (absolute) a uv.lock resolves
// editable and directory sources into: uv records a path dependency as
// source = { editable = "../sibling" } or { directory = "../sibling" },
// relative to the lock's own directory. found is false when there is no
// lock; a lock that does not parse is an error naming it.
func uvLockPathSources(lock string) (dirs []string, found bool, err error) {
	data, err := os.ReadFile(lock)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	doc, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return nil, true, fmt.Errorf("%s does not parse as a uv lock: %w; run `uv lock` there", lock, err)
	}
	packages, _ := (*doc)["package"].([]any)
	base := filepath.Dir(lock)
	for _, p := range packages {
		pkg, _ := p.(map[string]any)
		source, _ := pkg["source"].(map[string]any)
		for _, key := range []string{"editable", "directory"} {
			if rel, ok := source[key].(string); ok && rel != "" {
				dirs = append(dirs, filepath.Clean(filepath.Join(base, rel)))
			}
		}
	}
	return dirs, true, nil
}

// The tools' words for a registry they could not reach, and for one that
// answered without the required version.
var (
	registryNetworkMarkers = []string{"ECONNREFUSED", "ENOTFOUND", "EAI_AGAIN", "ETIMEDOUT", "ECONNRESET", "ENETUNREACH", "Failed to fetch", "error sending request", "tcp connect error", "dns error"}
	registryUnpublished    = []*regexp.Regexp{
		regexp.MustCompile(`No matching version found for (?P<name>\S+)@(?P<version>[^\s@]+?)\.?(?:\s|$)`),
		regexp.MustCompile(`'(?P<name>\S+)@(?P<version>[^\s@']+)' is not in this registry`),
		regexp.MustCompile(`there is no version of (?P<name>[^\s=]+)==(?P<version>\S+?)[,.]?\s`),
	}
	uvNotFound = regexp.MustCompile(`Because (?P<name>\S+) was not found in the (?:package registry|provided package locations)`)
)

// unpublishedRequirement is the name and version of a required version the
// registry lacks, named in a tool's output.
func unpublishedRequirement(detail string) (name, version string, ok bool) {
	flat := strings.Join(strings.Fields(detail), " ") + " "
	for _, re := range registryUnpublished {
		if m := re.FindStringSubmatch(flat); m != nil {
			return m[re.SubexpIndex("name")], m[re.SubexpIndex("version")], true
		}
	}
	if m := uvNotFound.FindStringSubmatch(flat); m != nil {
		name = m[uvNotFound.SubexpIndex("name")]
		pinned := regexp.MustCompile(`depends on ` + regexp.QuoteMeta(name) + `==(\S+?)[,.]?\s`).FindStringSubmatch(flat)
		if pinned != nil {
			return name, pinned[1], true
		}
		return name, "(any version)", true
	}
	return "", "", false
}

// LockfileSyncFailure is the refusal of a re-lock that could not run or
// failed, naming the command, where it ran, and the fix for the cause the
// tool's own output shows: an unreachable registry, or a required version
// the registry never published. where is the directory in the working
// tree, where the fix is made; rerun is how the release continues.
func LockfileSyncFailure(s LockfileSync, where, output string, cause error, rerun string) string {
	command := strings.Join(s.Argv, " ")
	what := "re-locking " + s.Lockfile + " after the version bump"
	network := "Restore this machine's network access to the registry it is configured for (`npm config get registry` prints npm's; uv's is the index pyproject.toml or UV_DEFAULT_INDEX names)"
	head := fmt.Sprintf("`%s` failed in %s (%s): %v\n%s\n", command, where, what, cause, strings.TrimSpace(output))
	if name, version, ok := unpublishedRequirement(output); ok {
		return head + fmt.Sprintf("%s %s is required but the registry does not have it: that version was never published. When %s is a sibling package, release it first (its release publishes %s), then %s.", name, version, name, version, rerun)
	}
	for _, m := range registryNetworkMarkers {
		if strings.Contains(output, m) {
			return head + fmt.Sprintf("The package registry could not be reached. %s, then %s.", network, rerun)
		}
	}
	return head + fmt.Sprintf("Fix the error %s printed, then %s.", s.Argv[0], rerun)
}

// SyncLockfile runs one re-lock in the repository at root. Its output is
// captured, so a failure can name the cause it shows; where is the
// directory in the working tree a refusal names. Under --dry-run (preview)
// the re-lock is recorded and nothing is read back.
func SyncLockfile(r HookRunner, root string, s LockfileSync, environment map[string]string, where, rerun string, preview bool) error {
	argv := make([]interface{}, len(s.Argv))
	for i, a := range s.Argv {
		argv[i] = a
	}
	opts := []strictcli.EffectOption{strictcli.Cwd(filepath.Join(root, filepath.FromSlash(s.Dir))), strictcli.EffectEnv(environment), strictcli.Timeout(s.Timeout)}
	if preview {
		_, err := r.Run(argv, opts...)
		return err
	}
	c, err := r.Run(argv, append(opts, strictcli.Check(false))...)
	if err != nil {
		return errors.New(LockfileSyncFailure(s, where, "", err, rerun))
	}
	if c.ExitCode() != 0 {
		return errors.New(LockfileSyncFailure(s, where, c.Stderr()+"\n"+c.Stdout(), fmt.Errorf("exit %d", c.ExitCode()), rerun))
	}
	return nil
}

// The go command's words for a module proxy it could not reach, for a
// version the proxy never published, and for a module it cannot fetch at all
// (a private repository), which is no unpublished version.
var (
	goNetworkMarkers = []string{"dial tcp", "no such host", "network is unreachable", "connection refused", "i/o timeout", "TLS handshake timeout", "proxyconnect"}
	goUnpublished    = regexp.MustCompile(`(?P<module>[^\s:]+)@(?P<version>v[^\s:]+): (?:reading \S+: (?:404 Not Found|410 Gone|no such file or directory)|invalid version: unknown revision)`)
	goPrivateMarkers = []string{"could not read Username", "terminal prompts disabled", "Repository not found"}
)

// goGuard names what the release's Go checks are asked for.
const goGuard = "the release's check that the Go modules are tidy"

// goCommandFailure is the refusal of a Go command a release check ran,
// naming the command, where (in the working tree), and the fix for the
// cause go's output shows.
func goCommandFailure(argv []string, where string, code int, stderr string) string {
	cmd := strings.Join(argv, " ")
	rerun := "then run the release again"
	warm := fmt.Sprintf("`go mod download all` in %s", where)
	detail := strings.TrimSpace(stderr)
	head := fmt.Sprintf("`%s` failed in %s (%s, exit %d):\n%s\n", cmd, where, goGuard, code, detail)
	switch {
	case strings.Contains(detail, "parsing $GOFLAGS"):
		return head + fmt.Sprintf("GOFLAGS holds a flag go does not accept: correct or unset GOFLAGS (`go env GOFLAGS` prints it), %s.", rerun)
	case strings.Contains(detail, "module lookup disabled by GOPROXY=off"):
		return head + fmt.Sprintf("GOPROXY=off forbids downloads and the module cache lacks a module this needs. Warm the cache with %s under a GOPROXY this machine reaches, %s.", warm, rerun)
	}
	if m := goUnpublished.FindStringSubmatch(detail); m != nil {
		private := false
		for _, p := range goPrivateMarkers {
			if strings.Contains(detail, p) {
				private = true
			}
		}
		if !private {
			module, version := m[goUnpublished.SubexpIndex("module")], m[goUnpublished.SubexpIndex("version")]
			return head + fmt.Sprintf("%s %s is required but the module proxy does not have it: that version was never published. When %s is a sibling module, release it first (its release publishes %s), %s.", module, version, module, version, rerun)
		}
	}
	for _, m := range goNetworkMarkers {
		if strings.Contains(detail, m) {
			return head + fmt.Sprintf("The module proxy (GOPROXY) could not be reached and the module cache lacks a module this needs. Warm the cache with %s while the proxy is reachable, or set GOPROXY to a proxy this machine reaches, %s.", warm, rerun)
		}
	}
	return head + fmt.Sprintf("Run `%s` in %s to reproduce it, fix the error it prints, %s.", cmd, where, rerun)
}

// UntidyGoModuleError refuses a release whose Go re-lock would change what
// it commits beyond its own edits.
type UntidyGoModuleError struct{ Message string }

func (e *UntidyGoModuleError) Error() string { return e.Message }

// runGo runs a read-only go command in dir and returns its stdout; a
// non-zero exit is an *UntidyGoModuleError naming the cause.
func runGo(r HookRunner, dir, where string, argv []string, environment map[string]string, timeout time.Duration) (stdout string, code int, stderr string, err error) {
	args := make([]interface{}, len(argv))
	for i, a := range argv {
		args[i] = a
	}
	c, err := r.Run(args, strictcli.Cwd(dir), strictcli.EffectEnv(environment), strictcli.Timeout(timeout), strictcli.Check(false))
	if err != nil {
		return "", 0, "", &UntidyGoModuleError{Message: fmt.Sprintf("`%s` could not run in %s (%s): %v. Put a working Go toolchain first on PATH (`go version` must run), then run the release again", strings.Join(argv, " "), where, goGuard, err)}
	}
	return c.Stdout(), c.ExitCode(), c.Stderr(), nil
}

// RefuseUntidyGoModules refuses, before the release writes anything, a Go
// module whose owed `go mod tidy` would change go.mod or go.sum (nothing a
// release writes changes a module's requirements, so on a tidy module the
// re-lock changes nothing), and a Go workspace whose owed `go work sync`
// would raise a module's requirements. root is the repository the release
// reads; liveRoot the working tree, where the fix is made.
func RefuseUntidyGoModules(r HookRunner, root, liveRoot string, syncs []LockfileSync, environment map[string]string) error {
	for _, s := range syncs {
		dir := filepath.Join(root, filepath.FromSlash(s.Dir))
		where := filepath.Join(liveRoot, filepath.FromSlash(s.Dir))
		switch strings.Join(s.Argv, " ") {
		case strings.Join(goTidy, " "):
			out, code, stderr, err := runGo(r, dir, where, goTidyDiff, environment, s.Timeout)
			if err != nil {
				return err
			}
			if code == 0 {
				continue
			}
			if strings.TrimSpace(out) != "" {
				return &UntidyGoModuleError{Message: fmt.Sprintf("the Go module at %s is not tidy. The release runs `go mod tidy` there to refresh go.sum, and it would change what the release commits beyond the release's own edits. `go mod tidy -diff` reports:\n%s\nRun `go mod tidy` in %s, commit go.mod and go.sum, and run the release again", where, strings.TrimRight(out, "\n"), where)}
			}
			return &UntidyGoModuleError{Message: goCommandFailure(goTidyDiff, where, code, stderr)}
		case strings.Join(goWorkSync, " "):
			changes, err := goWorkSyncChanges(r, dir, where, environment, s.Timeout)
			if err != nil {
				return err
			}
			if len(changes) == 0 {
				continue
			}
			lines := make([]string, len(changes))
			for i, c := range changes {
				lines[i] = "  " + c
			}
			return &UntidyGoModuleError{Message: fmt.Sprintf("the Go workspace at %s is not in sync with its modules. The release runs `go work sync` there to refresh go.work.sum, and it would raise these requirements to the versions the workspace selects, changing what the release commits beyond the release's own edits:\n%s\nRun `go work sync` in %s, commit the go.mod and go.sum files it changes, and run the release again", where, strings.Join(lines, "\n"), where)}
		}
	}
	return nil
}

// goListedModule is one module of `go list -m -json all`.
type goListedModule struct {
	Path    string
	Version string
	Main    bool
	Replace *struct{ Version string }
}

// goWorkSyncChanges are the requirement raises `go work sync` in dir would
// write: a module the workspace uses requiring a dependency below the
// version the workspace's build list selects. The build list comes from `go
// list -m -json all`; each module's requirements from its go.mod.
func goWorkSyncChanges(r HookRunner, dir, where string, environment map[string]string, timeout time.Duration) ([]string, error) {
	argv := []string{"go", "list", "-m", "-json", "all"}
	out, code, stderr, err := runGo(r, dir, where, argv, environment, timeout)
	if err != nil {
		return nil, err
	}
	if code != 0 {
		return nil, &UntidyGoModuleError{Message: goCommandFailure(argv, where, code, stderr)}
	}
	main := map[string]bool{}
	selected := map[string]string{}
	dec := json.NewDecoder(strings.NewReader(out))
	for {
		var m goListedModule
		if err := dec.Decode(&m); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			return nil, fmt.Errorf("`go list -m -json all` in %s printed what is not module JSON: %w", where, err)
		}
		if m.Main {
			main[m.Path] = true
			continue
		}
		v := m.Version
		if m.Replace != nil && m.Replace.Version != "" {
			v = m.Replace.Version
		}
		selected[m.Path] = v
	}
	uses, _, err := gomodule.WorkUses(dir)
	if err != nil {
		return nil, err
	}
	var changes []string
	for _, use := range uses {
		moduleDir := filepath.Clean(filepath.Join(dir, use))
		f, found, err := gomodule.Read(moduleDir)
		if err != nil {
			return nil, err
		}
		if !found {
			continue
		}
		requires, _ := gomodule.Directives(f)
		for _, req := range requires {
			if main[req.Path] {
				continue
			}
			if chosen := selected[req.Path]; chosen != "" && chosen != req.Version {
				rel, _ := filepath.Rel(dir, moduleDir)
				changes = append(changes, fmt.Sprintf("%s/go.mod: %s %s -> %s", filepath.Join(where, rel), req.Path, req.Version, chosen))
			}
		}
	}
	sort.Strings(changes)
	return changes, nil
}
