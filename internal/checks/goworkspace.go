package checks

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
)

// The Go workspace family: Go modules of one workspace depending on each
// other. rlsbl does not raise a dependent's require line when a sibling
// module releases, because go.sum needs the new version's hash, which
// exists only once the tag reached the module proxy; and development across
// modules uses a committed go.work, since `go install module@version`
// refuses a module carrying a replace and its consumers never see one.
func goWorkspaceChecks() []check {
	return []check{
		errorCheck("go-workspace-require-current", checkGoWorkspaceRequireCurrent),
		errorCheck("go-workspace-replace", checkGoWorkspaceReplace),
	}
}

// goMember is a workspace member whose own directory holds a go.mod.
type goMember struct {
	member  declarations.Member
	module  string
	require []gomodule.Require
	replace []gomodule.Replace
}

// goMembers are every member of the workspace with a go.mod in its own
// directory, read strictly.
func goMembers(c *Context) []goMember {
	var out []goMember
	for _, m := range c.Workspace().Members() {
		f, found, err := gomodule.Read(c.Workspace().MemberDir(m))
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if !found || f.Module == nil {
			continue
		}
		requires, replaces := gomodule.Directives(f)
		out = append(out, goMember{member: m, module: f.Module.Mod.Path, require: requires, replace: replaces})
	}
	return out
}

func checkGoWorkspaceRequireCurrent(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	members := goMembers(c)
	if len(members) == 0 {
		return r.Skipped("no member holds a go.mod")
	}
	byModule := map[string]declarations.Member{}
	for _, g := range members {
		byModule[g.module] = g.member
	}
	var problems []string
	for _, g := range members {
		for _, req := range g.require {
			sibling, ok := byModule[req.Path]
			if !ok || sibling.Name == g.member.Name || !sibling.Versioned() {
				continue
			}
			latest, found, err := releaserecord.LatestReleasedVersion(c.Root(), releaserecord.ArchiveDir(sibling.Releasable))
			if err != nil {
				panic(unanswered(err.Error()))
			}
			if !found {
				continue
			}
			if required, err := semver.Parse(strings.TrimPrefix(req.Version, "v")); err == nil && semver.Compare(required, latest) >= 0 {
				continue
			}
			problems = append(problems, fmt.Sprintf("%s: %s requires %s %s (line %d), below that module's latest release, v%s. rlsbl does not raise it, because go.sum needs the new version's hash, which exists only once the tag reached the module proxy. Run `go get %s@v%s` in %s, then commit go.mod and go.sum", g.member.Name, declarations.Join(g.member.Path, gomodule.FileName), req.Path, req.Version, req.Line, latest, req.Path, latest, g.member.Path))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d stale sibling module requirement(s)", len(problems)), "every requirement of a sibling module is at its latest release or above")
}

// insideDir reports whether path (absolute, symlinks resolved) is dir or
// lies under it.
func insideDir(path, dir string) bool {
	return path == dir || strings.HasPrefix(path, dir+string(filepath.Separator))
}

// resolvedPath is path with its symlinks resolved where it exists.
func resolvedPath(path string) string {
	if target, err := filepath.EvalSymlinks(path); err == nil {
		return target
	}
	return filepath.Clean(path)
}

func checkGoWorkspaceReplace(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	members := goMembers(c)
	if len(members) == 0 {
		return r.Skipped("no member holds a go.mod")
	}
	root := resolvedPath(c.Root())
	var dirs []string
	for _, g := range members {
		dirs = append(dirs, g.member.Path)
	}
	sort.Strings(dirs)
	use := strings.Join(dirs, " ")
	work := fmt.Sprintf("run `go work init` and `go work use %s` at the repository root", use)
	if _, err := os.Stat(filepath.Join(c.Root(), gomodule.WorkFileName)); err == nil {
		work = fmt.Sprintf("run `go work use %s` at the repository root", use)
	}
	var problems []string
	for _, g := range members {
		if !g.member.Versioned() {
			continue
		}
		moduleDir := resolvedPath(c.Workspace().MemberDir(g.member))
		for _, rep := range g.replace {
			if !rep.IsLocal() {
				continue
			}
			target := rep.NewPath
			if !filepath.IsAbs(target) {
				target = filepath.Join(moduleDir, target)
			}
			if !insideDir(resolvedPath(target), root) {
				continue
			}
			problems = append(problems, fmt.Sprintf("%s: %s replaces %s with %s (line %d), a directory in this workspace. `go install %s@<version>` refuses a module carrying a replace, and its consumers never see one. Develop across the workspace's modules with a committed go.work instead: %s and commit go.work, then run `go mod edit -dropreplace=%s` in %s", g.member.Name, declarations.Join(g.member.Path, gomodule.FileName), rep.OldPath, rep.NewPath, rep.Line, g.module, work, rep.OldPath, g.member.Path))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d replace directive(s) into the workspace", len(problems)), "no versioned Go member replaces a module with a directory of the workspace")
}
