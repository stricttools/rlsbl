package scaffold

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
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
	"github.com/stricttools/rlsbl/internal/pipelines"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// render is one file scaffold renders: its repository-relative path and the
// text the template gives it ("theirs" of the three-way merge).
type render struct {
	path   string
	theirs string
	// executable files are written with mode 0755.
	executable bool
	// userOwned files are written only while absent and never recorded:
	// once created they are the project's.
	userOwned bool
	// mergeLines files (the root .gitignore) gain the template's lines they
	// lack and lose none; they are the project's and never recorded.
	mergeLines bool
	// shared files are the ones --skip-shared leaves alone.
	shared bool
}

// memberTarget is one target of the member scaffolded.
type memberTarget struct {
	name string
	// dir is the target's directory relative to the member ("." for the
	// member's own directory).
	dir string
	// abs is the target's directory.
	abs string
}

// memberContext is everything the renders of one member are computed from.
type memberContext struct {
	e          *strictcli.Effects
	repo       git.Repo
	ws         *workspace.Workspace
	member     declarations.Member
	releasable declarations.Releasable
	versioned  bool
	targets    []memberTarget
	features   Features
	// license is the releasable's license on the scaffold's date, empty when
	// the record holds none; author is the git identity's name.
	license string
	author  string
	now     time.Time
	runner  *declarations.TestRunner
	record  *lifecycle.Record
	options *options.Options
	github  github.Client
	// repoName is the repository on GitHub, once resolved.
	repoName     github.Repository
	repoResolved bool
}

// memberPath is a member-relative path as a repository-relative one.
func (c *memberContext) memberPath(rel string) string {
	return joinDir(c.member.Path, rel)
}

// targetNames are the member's target names.
func (c *memberContext) targetNames() []string {
	names := make([]string, len(c.targets))
	for i, t := range c.targets {
		names[i] = t.name
	}
	return names
}

// pipelinesFor are the member's pipelines publishing the named target.
func (c *memberContext) pipelinesFor(target string) []declarations.Pipeline {
	var out []declarations.Pipeline
	for _, p := range c.member.Pipelines {
		if p.Target == target {
			out = append(out, p)
		}
	}
	return out
}

// target is the member's target of that name.
func (c *memberContext) target(name string) (memberTarget, bool) {
	for _, t := range c.targets {
		if t.name == name {
			return t, true
		}
	}
	return memberTarget{}, false
}

// ciWorkflow is the CI workflow path of a target: ci.yml when the member
// has one target, ci-<target>.yml beside the others otherwise.
func (c *memberContext) ciWorkflow(target string) string {
	if len(c.targets) == 1 {
		return c.memberPath(workflows.Dir + "/ci.yml")
	}
	return c.memberPath(workflows.Dir + "/ci-" + target + ".yml")
}

// goBinaryPackaged reports whether a go-binary pipeline packages the named
// target: such a package carries a go binary and no code of its own to
// test.
func (c *memberContext) goBinaryPackaged(target string) bool {
	for _, p := range c.pipelinesFor(target) {
		if p.Artifact == declarations.ArtifactGoBinary {
			return true
		}
	}
	return false
}

var (
	requiresPythonFloor = regexp.MustCompile(`>=\s*(\d+\.\d+(?:\.\d+)?)`)
	versionVariable     = regexp.MustCompile(`^var\s+[Vv]ersion\b`)
)

// renders are every file scaffold renders for the member, in the order
// they are reported.
func (c *memberContext) renders() ([]render, error) {
	var out []render
	add := func(path, template string, vars Vars) error {
		text, err := renderTemplate(template, vars)
		if err != nil {
			return err
		}
		out = append(out, render{path: path, theirs: text})
		return nil
	}
	for _, t := range c.targets {
		var err error
		switch t.name {
		case declarations.TargetGo:
			err = c.goRenders(t, &out, add)
		case declarations.TargetNPM:
			err = c.npmRenders(t, &out, add)
		case declarations.TargetPyPI:
			err = c.pypiRenders(t, &out, add)
		default:
			err = fmt.Errorf("%q is not a release target rlsbl scaffolds; the targets are %s", t.name, strings.Join(targets.Names(), ", "))
		}
		if err != nil {
			return nil, err
		}
	}
	shared, err := c.sharedRenders()
	if err != nil {
		return nil, err
	}
	return append(out, shared...), nil
}

func (c *memberContext) goRenders(t memberTarget, out *[]render, add func(path, template string, vars Vars) error) error {
	version, err := goVersion(t.abs)
	if err != nil {
		return err
	}
	if err := add(c.memberPath(joinDir(t.dir, targets.VersionFile)), "go/VERSION.tpl", Vars{"version": version}); err != nil {
		return err
	}
	ci, err := renderTemplate("go/ci.yml.tpl", Vars{})
	if err != nil {
		return err
	}
	*out = append(*out, render{path: c.ciWorkflow(t.name), theirs: inDirectory(ci, t.dir)})
	var binary *declarations.Pipeline
	for _, p := range c.pipelinesFor(t.name) {
		if p.Artifact == declarations.ArtifactBinary {
			binary = &p
		}
	}
	if binary == nil {
		return nil
	}
	main, err := gomodule.ResolveMainPackageDir(c.e, t.abs, binary.InstallPaths)
	if err != nil {
		return err
	}
	name, err := binaryName(t.abs)
	if err != nil {
		return err
	}
	brews, err := c.brewsSection(*binary, name)
	if err != nil {
		return err
	}
	var goos, goarch []string
	seenOS, seenArch := map[string]bool{}, map[string]bool{}
	for _, p := range pipelines.Platforms() {
		if !seenOS[p.GOOS] {
			seenOS[p.GOOS] = true
			goos = append(goos, "      - "+p.GOOS)
		}
		if !seenArch[p.GOARCH] {
			seenArch[p.GOARCH] = true
			goarch = append(goarch, "      - "+p.GOARCH)
		}
	}
	if err := add(c.memberPath(joinDir(t.dir, ".goreleaser.yml")), "go/goreleaser.yml.tpl", Vars{
		"goreleaserMain":   main,
		"goreleaserGoos":   strings.Join(goos, "\n"),
		"goreleaserGoarch": strings.Join(goarch, "\n"),
		"binaryName":       name,
		"brewsSection":     brews,
	}); err != nil {
		return err
	}
	declares, err := declaresVersion(filepath.Join(t.abs, filepath.FromSlash(main)))
	if err != nil || declares {
		return err
	}
	return add(c.memberPath(joinDir(joinDir(t.dir, strings.TrimPrefix(main, "./")), "version.go")), "go/version.go.tpl", Vars{})
}

// goVersion is the version a go target's VERSION holds, or 0.0.0, the
// version a VERSION scaffold creates starts at.
func goVersion(dir string) (string, error) {
	data, err := os.ReadFile(filepath.Join(dir, targets.VersionFile))
	if errors.Is(err, fs.ErrNotExist) {
		return "0.0.0", nil
	}
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}

// binaryName is the binary a go module builds and its release archives are
// named after: the module path's last element.
func binaryName(dir string) (string, error) {
	path, found, err := gomodule.ModulePath(dir)
	if err != nil {
		return "", err
	}
	if !found {
		return "", fmt.Errorf("%s holds no go.mod: run `go mod init <module path>` there first", dir)
	}
	return gomodule.LastElement(path), nil
}

// declaresVersion reports whether a .go file of dir declares a Version
// variable at its top level.
func declaresVersion(dir string) (bool, error) {
	matches, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		return false, err
	}
	for _, m := range matches {
		f, err := os.Open(m)
		if err != nil {
			return false, err
		}
		scanner := bufio.NewScanner(f)
		found := false
		for scanner.Scan() {
			if versionVariable.MatchString(scanner.Text()) {
				found = true
				break
			}
		}
		err = scanner.Err()
		f.Close()
		if err != nil {
			return false, err
		}
		if found {
			return true, nil
		}
	}
	return false, nil
}

// brewsSection is goreleaser's brews section for a go binary pipeline
// publishing a Homebrew formula, or empty. The formula's description is the
// member's and its license the record's.
func (c *memberContext) brewsSection(p declarations.Pipeline, name string) (string, error) {
	if p.HomebrewTap == "" {
		return "", nil
	}
	if !c.features.RepositoryURLs {
		return "", fmt.Errorf("the pipeline %q publishes a Homebrew formula, which names the repository publicly, and the private-repository-publishing rule refuses that for this repository; remove homebrew_tap from the pipeline in %s", p.Name, declarations.ReleasablesFile)
	}
	if strings.TrimSpace(c.member.Description) == "" {
		return "", fmt.Errorf("the pipeline %q publishes a Homebrew formula, whose description is the member's: declare description on the member %q in %s", p.Name, c.member.Name, declarations.ReleasablesFile)
	}
	if c.license == "" {
		return "", fmt.Errorf("the pipeline %q publishes a Homebrew formula, whose license is the releasable's, and the lifecycle-and-license record holds no license in effect for %q", p.Name, c.releasable.Name)
	}
	repo, err := c.githubRepository()
	if err != nil {
		return "", err
	}
	return "\nbrews:\n" +
		"  - repository:\n" +
		"      owner: " + repo.Owner + "\n" +
		"      name: " + p.HomebrewTap + "\n" +
		"      token: \"{{ .Env.HOMEBREW_TAP_TOKEN }}\"\n" +
		"    homepage: \"https://github.com/" + repo.String() + "\"\n" +
		"    description: " + quoteYAML(c.member.Description) + "\n" +
		"    license: " + quoteYAML(c.license) + "\n" +
		"    install: |\n" +
		"      bin.install \"" + name + "\"\n" +
		"    test: |\n" +
		"      system \"#{bin}/" + name + "\", \"--version\"", nil
}

// quoteYAML is s as a double-quoted YAML scalar.
func quoteYAML(s string) string {
	data, _ := json.Marshal(s)
	return string(data)
}

func (c *memberContext) npmRenders(t memberTarget, out *[]render, add func(path, template string, vars Vars) error) error {
	goBinary := c.goBinaryPackaged(t.name)
	if !goBinary {
		manager, found, err := targets.PackageManager(t.abs)
		if err != nil {
			return err
		}
		if !found {
			manager = "npm"
		}
		template := map[string]string{"npm": "npm/ci.yml.tpl", "pnpm": "npm/ci-pnpm.yml.tpl", "yarn": "npm/ci-yarn.yml.tpl"}[manager]
		lines, err := NodeMatrix(t.abs)
		if err != nil {
			return err
		}
		packageName, _, err := npmPackageName(t.abs)
		if err != nil {
			return err
		}
		ci, err := renderTemplate(template, Vars{"npm.nodeMatrix": RenderNodeMatrix(lines), "npm.name": packageName})
		if err != nil {
			return err
		}
		*out = append(*out, render{path: c.ciWorkflow(t.name), theirs: inDirectory(ci, t.dir)})
	}
	ignore, err := renderTemplate("npm/npmignore.tpl", Vars{"npm.privatePathIgnore": targets.NpmignoreBlock()})
	if err != nil {
		return err
	}
	*out = append(*out, render{path: c.memberPath(joinDir(t.dir, ".npmignore")), theirs: ignore, userOwned: true})
	if goBinary {
		return c.npmLauncher(t, add)
	}
	return nil
}

// npmPackageName is the name package.json in dir declares.
func npmPackageName(dir string) (string, bool, error) {
	t, err := targets.Get(declarations.TargetNPM)
	if err != nil {
		return "", false, err
	}
	return t.ReadName(dir)
}

// launcherPath is where an npm go-binary main package's launcher sits,
// relative to the package.
const launcherPath = "bin/index.js"

// npmLauncher renders the launcher of an npm go-binary main package, whose
// package.json, written by the project, must name it as the binary's bin.
func (c *memberContext) npmLauncher(t memberTarget, add func(path, template string, vars Vars) error) error {
	binary, err := c.packagedBinary(t.name)
	if err != nil {
		return err
	}
	path := filepath.Join(t.abs, "package.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("an npm go-binary pipeline publishes the package in %s, whose package.json (written by the project: its name is the registry name) is missing: %w", c.memberPath(t.dir), err)
	}
	var manifest struct {
		Name    string            `json:"name"`
		Bin     any               `json:"bin"`
		Scripts map[string]string `json:"scripts"`
	}
	if err := json.Unmarshal(data, &manifest); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	bins, _ := manifest.Bin.(map[string]any)
	if manifest.Name == "" || bins[binary] != launcherPath {
		return fmt.Errorf("%s must declare the package's name and \"bin\": {%q: %q}, the launcher scaffold writes, which runs the binary of the platform package npm installed", path, binary, launcherPath)
	}
	for _, s := range []string{"preinstall", "install", "postinstall"} {
		if manifest.Scripts[s] != "" {
			return fmt.Errorf("%s declares a %s script; a go-binary package selects its platform package through optionalDependencies and runs nothing at install, so delete the script", path, s)
		}
	}
	var entries []string
	for _, p := range pipelines.Platforms() {
		entries = append(entries, fmt.Sprintf("  %q: %q,", p.OS+" "+p.CPU, pipelines.PlatformPackageName(manifest.Name, p)))
	}
	if err := add(c.memberPath(joinDir(t.dir, launcherPath)), "shared/go-binary/bin-index.js.tpl", Vars{"binaryName": binary, "platformPackages": strings.Join(entries, "\n")}); err != nil {
		return err
	}
	return nil
}

// packagedBinary is the binary the go-binary pipeline of the named target
// packages: the module of the go binary pipeline it names.
func (c *memberContext) packagedBinary(target string) (string, error) {
	for _, p := range c.pipelinesFor(target) {
		if p.Artifact != declarations.ArtifactGoBinary {
			continue
		}
		binary, ok := c.member.Pipeline(p.BinaryPipeline)
		if !ok {
			return "", fmt.Errorf("the pipeline %q packages the go binary pipeline %q, which the member %q does not declare", p.Name, p.BinaryPipeline, c.member.Name)
		}
		goTarget, ok := c.target(binary.Target)
		if !ok {
			return "", fmt.Errorf("the go binary pipeline %q publishes the target %q, which the member %q does not have", binary.Name, binary.Target, c.member.Name)
		}
		return binaryName(goTarget.abs)
	}
	return "", fmt.Errorf("no go-binary pipeline packages the %s target", target)
}

func (c *memberContext) pypiRenders(t memberTarget, out *[]render, add func(path, template string, vars Vars) error) error {
	if c.goBinaryPackaged(t.name) {
		return nil
	}
	importName, err := c.importName(t)
	if err != nil {
		return err
	}
	vars := Vars{"importName": importName, "pypi.privatePathCheck": indent(targets.PrivatePathsCheckProgram, "          ")}
	if floor, err := requiresPython(t.abs); err != nil {
		return err
	} else if floor != "" {
		vars["pypi.minRequiredPython"] = floor
	}
	if _, _, found, err := targets.PytestDeclaration(t.abs); err != nil {
		return err
	} else if found {
		vars["pypi.hasPytest"] = "true"
	}
	var nestedAbs []string
	for _, n := range c.ws.NestedMemberPaths(c.member) {
		nestedAbs = append(nestedAbs, filepath.Join(c.ws.Root, filepath.FromSlash(n)))
	}
	if nested := targets.NestedPathsInside(t.abs, nestedAbs); len(nested) > 0 {
		vars["pypi.nestedMembers"] = strings.Join(nested, " ")
	}
	ci, err := renderTemplate("pypi/ci.yml.tpl", vars)
	if err != nil {
		return err
	}
	*out = append(*out, render{path: c.ciWorkflow(t.name), theirs: inDirectory(ci, t.dir)})
	return nil
}

// importName is the name CI imports a pypi target's package by: the
// member's declared import_name, else its package directory as a module
// path, else the project name with hyphens as underscores.
func (c *memberContext) importName(t memberTarget) (string, error) {
	if c.member.ImportName != "" {
		return c.member.ImportName, nil
	}
	root, found, err := targets.PythonPackageRoot(t.abs)
	if err != nil {
		return "", err
	}
	if found {
		return strings.ReplaceAll(strings.TrimPrefix(root, "src/"), "/", "."), nil
	}
	pypi, err := targets.Get(declarations.TargetPyPI)
	if err != nil {
		return "", err
	}
	name, found, err := pypi.ReadName(t.abs)
	if err != nil {
		return "", err
	}
	if !found {
		return "", fmt.Errorf("%s/pyproject.toml declares no [project] name, so CI cannot tell which package to import", c.memberPath(t.dir))
	}
	return strings.ReplaceAll(name, "-", "_"), nil
}

// requiresPython is the lowest Python version [project] requires-python
// admits, or empty.
func requiresPython(dir string) (string, error) {
	data, err := os.ReadFile(filepath.Join(dir, targets.Pyproject))
	if err != nil {
		return "", err
	}
	for _, line := range strings.Split(string(data), "\n") {
		key, value, ok := strings.Cut(line, "=")
		if !ok || strings.TrimSpace(key) != "requires-python" {
			continue
		}
		if m := requiresPythonFloor.FindStringSubmatch(value); m != nil {
			return m[1], nil
		}
	}
	return "", nil
}

// inDirectory makes a workflow's jobs run in dir, a target's directory
// relative to the member; "." leaves the workflow as it is.
func inDirectory(text, dir string) string {
	if dir == "." || dir == "" {
		return text
	}
	return workflowInDirectory(text, dir)
}

// sharedRenders are the files every member gets whatever its targets: the
// .gitignore lines, the scratch directories, the stub go.mod files keeping
// private directories out of a Go module, the LICENSE, and the sandboxed
// test runner the member owns.
func (c *memberContext) sharedRenders() ([]render, error) {
	var out []render
	add := func(r render) {
		r.shared = true
		out = append(out, r)
	}
	ignore, err := renderTemplate("shared/gitignore.tpl", Vars{})
	if err != nil {
		return nil, err
	}
	add(render{path: c.memberPath(".gitignore"), theirs: ignore, mergeLines: true})
	hasGo := false
	for _, t := range c.targets {
		if t.name == declarations.TargetGo {
			hasGo = true
		}
	}
	scratchVars := Vars{"scratchGoModule": boolVar(hasGo)}
	for _, dir := range ScratchDirs {
		text, err := renderTemplate("shared/scratch/gitignore.tpl", scratchVars)
		if err != nil {
			return nil, err
		}
		add(render{path: c.memberPath(dir + "/.gitignore"), theirs: text})
		if hasGo {
			text, err := renderTemplate("shared/scratch/go.mod.tpl", Vars{})
			if err != nil {
				return nil, err
			}
			add(render{path: c.memberPath(dir + "/go.mod"), theirs: text})
		}
	}
	stubs, err := c.goStubs()
	if err != nil {
		return nil, err
	}
	stub, err := renderTemplate("shared/private/go.mod.tpl", Vars{})
	if err != nil {
		return nil, err
	}
	for _, p := range stubs {
		add(render{path: p, theirs: stub})
	}
	licensePath := c.memberPath("LICENSE")
	if _, err := os.Stat(filepath.Join(c.ws.Root, filepath.FromSlash(licensePath))); errors.Is(err, fs.ErrNotExist) {
		license, ok, err := c.licenseText()
		if err != nil {
			return nil, err
		}
		if ok {
			add(render{path: licensePath, theirs: license, userOwned: true})
		}
	} else if err != nil {
		return nil, err
	}
	if runner, ok, err := c.testRunnerRender(); err != nil {
		return nil, err
	} else if ok {
		add(runner)
	}
	return out, nil
}

// goStubs are the stub go.mod files (repository-relative) every go target's
// module needs in its private directories. .strictmetadata/ sits at the
// repository root and is inside a module only when the module is at the
// root.
func (c *memberContext) goStubs() ([]string, error) {
	var out []string
	seen := map[string]bool{}
	for _, t := range c.targets {
		if t.name != declarations.TargetGo {
			continue
		}
		dirs, err := targets.GoStubDirectories(c.repo, t.abs)
		if err != nil {
			return nil, err
		}
		moduleRel := joinDir(c.member.Path, t.dir)
		for _, d := range dirs {
			if d == declarations.MetadataDir && moduleRel != "." {
				continue
			}
			p := joinDir(moduleRel, d) + "/go.mod"
			if !seen[p] {
				seen[p] = true
				out = append(out, p)
			}
		}
	}
	sort.Strings(out)
	return out, nil
}
