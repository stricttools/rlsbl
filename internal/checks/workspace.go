package checks

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The workspace family: the members a workspace declares against the
// directories and manifests on disk, the boundaries between versioned,
// unversioned, and dev-only members, whether its Python members build,
// release state left where nothing reads it, and the pytest configuration
// a nested layout needs.
func workspaceChecks() []check {
	return []check{
		errorCheck("workspace-targets", checkWorkspaceTargets),
		errorCheck("workspace-unregistered", checkWorkspaceUnregistered),
		errorCheck("workspace-stale-entries", checkWorkspaceStaleEntries),
		errorCheck("dev-only-boundary", checkDevOnlyBoundary),
		errorCheck("unversioned-boundary", checkUnversionedBoundary),
		errorCheck("workspace-unbuildable", checkWorkspaceUnbuildable),
		errorCheck("releasable-residue", checkReleasableResidue),
		errorCheck("member-pytest-config", checkMemberPytestConfig),
	}
}

// manifestFix is how a member gains a target.
const manifestFix = "declare its targets under its [[members]] entry in " + declarations.ReleasablesFile + ", or create its manifest (go.mod, package.json, or a pyproject.toml with a [project] table)"

func checkWorkspaceTargets(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	var problems []string
	checked, skipped := 0, 0
	byReleasable := map[string]int{}
	for _, m := range c.Members() {
		if m.DevOnly || !m.Versioned() {
			skipped++
			continue
		}
		declared, err := targets.MemberTargets(c.Root(), m)
		if err != nil {
			problems = append(problems, fmt.Sprintf("%s: %v", m.Name, err))
			continue
		}
		byReleasable[m.Releasable] += len(declared)
		if len(declared) == 0 && m.IsRoot() {
			// The root member owns what no other member claims and need
			// not be a package itself.
			skipped++
			continue
		}
		checked++
		if len(declared) == 0 {
			problems = append(problems, fmt.Sprintf("%s: no release target is declared or detected in %s: %s", m.Name, m.Path, manifestFix))
		}
	}
	for _, rel := range c.Workspace().Releasables() {
		if n, ok := byReleasable[rel.Name]; ok && n == 0 {
			problems = append(problems, fmt.Sprintf("the releasable %q has no target across its members, so a release would publish and version nothing: give one of its members a target (%s)", rel.Name, manifestFix))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d member(s) or releasable(s) without a target", len(problems)), fmt.Sprintf("all %d versioned member(s) have a target (%d skipped), and every releasable has one", checked, skipped))
}

// testInputDirs hold manifests that are test inputs, never projects (Go
// leaves testdata/ out by the same convention).
var testInputDirs = map[string]bool{"tests": true, "testdata": true, "fixtures": true}

// manifestNames are the files whose presence makes a directory a candidate
// project: every target's detection files.
func manifestNames() map[string]bool {
	names := map[string]bool{}
	for _, t := range targets.All() {
		for _, f := range t.Facts().DetectionFiles {
			names[f] = true
		}
	}
	return names
}

func checkWorkspaceUnregistered(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	ws := c.Workspace()
	known := map[string]bool{}
	for _, m := range ws.Members() {
		known[m.Path] = true
		declared, err := targets.MemberTargets(c.Root(), m)
		if err != nil {
			// workspace-targets reports a declaration naming no directory.
			continue
		}
		for _, t := range declared {
			known[m.TargetDir(t)] = true
		}
	}
	listed, err := c.Repo().TrackedAndUntrackedFiles()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	manifests := manifestNames()
	candidates := map[string]bool{}
	for _, f := range listed {
		if workspace.IsToolOwned(f) || !manifests[path.Base(f)] {
			continue
		}
		dir := path.Dir(f)
		if dir == "." || known[dir] || candidates[dir] {
			continue
		}
		if excludedProjectDir(ws, dir) {
			continue
		}
		candidates[dir] = true
	}
	var problems []string
	for _, dir := range sortedKeys(candidates) {
		found, err := targets.Detect(filepath.Join(c.Root(), filepath.FromSlash(dir)))
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if len(found) == 0 {
			continue
		}
		problems = append(problems, fmt.Sprintf("%s holds a %s project that no member declares, so it releases with whichever member's territory it lies in: declare it as a member with a [[members]] entry in %s (`rlsbl monorepo add %s --releasable <releasable, or false>` writes one), or move it out of the repository", dir, found[0].Name, declarations.ReleasablesFile, dir))
	}
	return reportErrors(r, problems, fmt.Sprintf("%d undeclared project(s)", len(problems)), "every project directory is a declared member or one of its targets")
}

// excludedProjectDir reports whether a directory holding a manifest is
// never a project: a hidden directory, a test input, or a scratch or
// private directory at a member's root.
func excludedProjectDir(ws *workspace.Workspace, dir string) bool {
	parts := strings.Split(dir, "/")
	for _, p := range parts {
		if strings.HasPrefix(p, ".") || testInputDirs[p] {
			return true
		}
	}
	owner, ok := ws.Declarations.MemberForPath(dir)
	if !ok {
		return false
	}
	owned := dir
	if !owner.IsRoot() {
		owned = strings.TrimPrefix(strings.TrimPrefix(dir, owner.Path), "/")
	}
	first, _, _ := strings.Cut(owned, "/")
	return owned != "" && (slices.Contains(scaffold.ScratchDirs, first) || slices.Contains(targets.PrivateRootDirs, first))
}

func sortedKeys(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// hasManifest reports whether dir (absolute) holds any target's detection
// file.
func hasManifest(dir string) bool {
	for name := range manifestNames() {
		if info, err := os.Stat(filepath.Join(dir, name)); err == nil && !info.IsDir() {
			return true
		}
	}
	return false
}

func checkWorkspaceStaleEntries(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	var problems []string
	for _, m := range c.Members() {
		dir := c.Workspace().MemberDir(m)
		info, err := os.Stat(dir)
		if errors.Is(err, os.ErrNotExist) || (err == nil && !info.IsDir()) {
			problems = append(problems, fmt.Sprintf("%s: its directory %s does not exist: remove the member from %s (`rlsbl monorepo remove %s`), or restore the directory", m.Name, m.Path, declarations.ReleasablesFile, m.Path))
			continue
		}
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if m.IsRoot() || m.Targets != nil || hasManifest(dir) {
			continue
		}
		problems = append(problems, fmt.Sprintf("%s: %s holds no manifest and the member declares no targets: remove the member from %s (`rlsbl monorepo remove %s`), or %s", m.Name, m.Path, declarations.ReleasablesFile, m.Path, manifestFix))
	}
	return reportErrors(r, problems, fmt.Sprintf("%d stale member entry/entries", len(problems)), "every member's directory exists and holds its project")
}

// runtimeDependents are the members depending on member at runtime or by a
// declared depends_on, directly or not.
func runtimeDependents(g *workspace.Graph, member string) []string {
	seen := map[string]bool{}
	for _, scope := range []string{workspace.ScopeRuntime, workspace.ScopeExplicit} {
		names, err := g.TransitiveDependents(member, -1, scope)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		for _, n := range names {
			seen[n] = true
		}
	}
	return sortedKeys(seen)
}

// memberGraph is the workspace's dependency graph; a manifest it could not
// read leaves the boundary unknown, which is the check's finding.
func memberGraph(c *Context) (*workspace.Graph, []string) {
	g := workspace.NewGraph(c.Workspace())
	var problems []string
	for _, e := range g.ScanErrors {
		problems = append(problems, fmt.Sprintf("%v: the member's dependencies cannot be read, so the boundary cannot be judged; fix the manifest", e))
	}
	return g, problems
}

// boundaryCheck reports every member of from that a member outside it
// depends on at runtime.
func boundaryCheck(c *Context, r *strictcli.ErrorReporter, from func(declarations.Member) bool, crosses func(declarations.Member) bool, describe func(dependent, dependency string) string, none, clean string) strictcli.CheckOutcome {
	var sources []declarations.Member
	for _, m := range c.Workspace().Members() {
		if from(m) {
			sources = append(sources, m)
		}
	}
	if len(sources) == 0 {
		return r.Passed(none)
	}
	g, problems := memberGraph(c)
	if len(problems) > 0 {
		return reportErrors(r, problems, "the dependency graph cannot be read", "")
	}
	var violations []string
	for _, s := range sources {
		for _, name := range runtimeDependents(g, s.Name) {
			if dep, ok := c.Declarations().Member(name); ok && crosses(dep) {
				violations = append(violations, describe(name, s.Name))
			}
		}
	}
	return reportErrors(r, violations, fmt.Sprintf("%d boundary violation(s)", len(violations)), clean)
}

func checkDevOnlyBoundary(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	return boundaryCheck(c, r,
		func(m declarations.Member) bool { return m.DevOnly },
		func(m declarations.Member) bool { return !m.DevOnly },
		func(dependent, dependency string) string {
			return fmt.Sprintf("the member %q, which is not dev-only, has a runtime dependency on the dev-only member %q, so a fix in %q ships in %q and appears in no changelog: make it a dev dependency, or declare %q without dev_only", dependent, dependency, dependency, dependent, dependency)
		},
		"no dev-only member", "no member that is not dev-only depends at runtime on a dev-only one")
}

func checkUnversionedBoundary(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	return boundaryCheck(c, r,
		func(m declarations.Member) bool { return !m.Versioned() && !m.DevOnly },
		func(m declarations.Member) bool { return m.Versioned() },
		func(dependent, dependency string) string {
			return fmt.Sprintf("the versioned member %q has a runtime dependency on the member %q, which is versioned under no releasable (releasable = false), so changes in %q ship inside releases of %q with no changelog coverage: version %q under a releasable, or make it a dev dependency", dependent, dependency, dependency, dependent, dependency)
		},
		"no unversioned member", "no versioned member depends at runtime on an unversioned one")
}

func checkWorkspaceUnbuildable(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	type pypiMember struct{ name, dir string }
	var members []pypiMember
	for _, m := range c.Members() {
		for _, t := range targetsOf(c, m) {
			if t.target.Facts().SharesWorkspaceEnvironment {
				members = append(members, pypiMember{m.Name, t.dir})
				break
			}
		}
	}
	if len(members) == 0 {
		return r.Skipped("no member has a target whose members share one environment (pypi)")
	}
	root := resolvedPath(c.Root())
	rootIsUV, err := dependencies.IsVirtualUvRoot(c.Root())
	if err != nil {
		panic(unanswered(err.Error()))
	}
	for _, m := range members {
		if rootIsUV {
			break
		}
		uvRoot, found, err := dependencies.FindUvWorkspaceRoot(m.dir)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		rootIsUV = found && resolvedPath(uvRoot) == root
	}
	type syncRun struct {
		label string
		dir   string
		argv  []string
	}
	var runs []syncRun
	if rootIsUV {
		runs = []syncRun{{"", c.Root(), []string{"uv", "sync", "--all-packages", "--dry-run"}}}
	} else {
		for _, m := range members {
			runs = append(runs, syncRun{m.name + ": ", m.dir, []string{"uv", "sync", "--dry-run"}})
		}
	}
	if _, err := exec.LookPath("uv"); err != nil {
		return reportErrors(r, []string{"uv is not on PATH: install uv (https://docs.astral.sh/uv/) to verify the workspace's members build"}, "uv is not on PATH", "")
	}
	var problems []string
	for _, run := range runs {
		argv := make([]interface{}, len(run.argv))
		for i, a := range run.argv {
			argv[i] = a
		}
		// A dry-run sync resolves and installs nothing, which is what lets
		// the read-only check command start it.
		done, err := c.Effects().Run(argv, strictcli.Cwd(run.dir), strictcli.Check(false), strictcli.Timeout(c.CheckTimeout()), strictcli.Observe())
		if err != nil {
			problems = append(problems, fmt.Sprintf("%s`%s` did not finish: %v %s", run.label, strings.Join(run.argv, " "), err, targets.TimeoutHint))
			continue
		}
		if done.ExitCode() == 0 {
			continue
		}
		lines := reportableLines(done.Stderr(), externalOutputLines)
		if len(lines) == 0 {
			lines = []string{fmt.Sprintf("`%s` exited %d", strings.Join(run.argv, " "), done.ExitCode())}
		}
		for _, l := range lines {
			problems = append(problems, run.label+l)
		}
	}
	passed := "every member of the uv workspace builds"
	if !rootIsUV {
		passed = fmt.Sprintf("all %d pypi member(s) build (the repository root is no uv workspace)", len(members))
	}
	found := ""
	if len(problems) > 0 {
		found = problems[0]
	}
	return reportErrors(r, problems, found, passed)
}

func checkReleasableResidue(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	tracked, err := c.Repo().TrackedFiles()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	residue, err := c.Workspace().DetectResidue(tracked)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var problems []string
	removable := 0
	for _, res := range residue {
		shown := res.Path
		if res.Directory {
			shown += "/"
		}
		how := "`rlsbl monorepo cleanup` removes it"
		if res.CleanupRemoves {
			removable++
		} else {
			how = "`rlsbl monorepo cleanup` keeps it: move it by hand where this item says"
		}
		problems = append(problems, fmt.Sprintf("%s: %s; %s", shown, res.Reason, how))
	}
	found := fmt.Sprintf("%d release-state item(s) where nothing reads them", len(problems))
	if removable > 0 {
		found += fmt.Sprintf("; `rlsbl monorepo cleanup` removes %d of them", removable)
	}
	return reportErrors(r, problems, found, "no release state is left where nothing reads it")
}

// pytestRootdirPinned reports whether the pyproject.toml at path declares
// [tool.pytest.ini_options]; a file that does not parse pins nothing.
func pytestRootdirPinned(path string) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	doc, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return false
	}
	tool, _ := (*doc)["tool"].(map[string]any)
	pytest, _ := tool["pytest"].(map[string]any)
	_, ok := pytest["ini_options"]
	return ok
}

func checkMemberPytestConfig(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	root := c.Root()
	hasConftest := func(rel string) bool {
		info, err := os.Stat(filepath.Join(root, filepath.FromSlash(rel), "conftest.py"))
		return err == nil && !info.IsDir()
	}
	conftestDirs := map[string]bool{}
	if hasConftest(declarations.RootPath) {
		conftestDirs[declarations.RootPath] = true
	}
	for _, m := range c.Members() {
		if hasConftest(m.Path) {
			conftestDirs[m.Path] = true
		}
	}
	if len(conftestDirs) == 0 {
		return r.Skipped("no member has a conftest.py, so no pytest rootdir can escape into one")
	}
	var problems []string
	for _, m := range c.Members() {
		if m.IsRoot() {
			continue
		}
		var enclosing []string
		for d := range conftestDirs {
			if d != m.Path && declarations.IsInside(m.Path, d) {
				enclosing = append(enclosing, d)
			}
		}
		if len(enclosing) == 0 {
			continue
		}
		dir := c.Workspace().MemberDir(m)
		pyproject := filepath.Join(dir, targets.Pyproject)
		if info, err := os.Stat(pyproject); err != nil || info.IsDir() {
			continue
		}
		if info, err := os.Stat(filepath.Join(dir, "tests")); err != nil || !info.IsDir() {
			continue
		}
		if pytestRootdirPinned(pyproject) {
			continue
		}
		// The nearest enclosing conftest.py is the one pytest loads first.
		sort.Slice(enclosing, func(i, j int) bool { return len(enclosing[i]) > len(enclosing[j]) })
		problems = append(problems, fmt.Sprintf("%s: add a [tool.pytest.ini_options] table to %s (for example testpaths = [\"tests\"]) to pin pytest's rootdir to the member; without it the rootdir escapes past it and %s is loaded into its test run", m.Name, declarations.Join(m.Path, targets.Pyproject), declarations.Join(enclosing[0], "conftest.py")))
	}
	return reportErrors(r, problems, fmt.Sprintf("%d member(s) with tests but no [tool.pytest.ini_options] of their own under an enclosing conftest.py", len(problems)), "every member with tests under an enclosing conftest.py pins its own pytest configuration")
}

