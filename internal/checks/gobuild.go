package checks

import (
	"fmt"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/targets"
)

// The Go build family: what a Go module's go.mod and build configuration
// must say for CI and the linker to do what the project means.
func goBuildChecks() []check {
	return []check{
		errorCheck("go-toolchain-declared", checkGoToolchainDeclared),
		errorCheck("ldflags-symbol", checkLdflagsSymbol),
	}
}

// noGoTarget is the skip reason of a Go check that finds no Go module.
const noGoTarget = "no go target"

// goModuleDirs are the absolute directories of the go targets of every
// member the check sees, each once, in member order.
func goModuleDirs(c *Context) []string {
	var dirs []string
	seen := map[string]bool{}
	for _, m := range c.Members() {
		for _, t := range targetsOf(c, m) {
			if t.target.Name() == targets.Go && !seen[t.dir] {
				seen[t.dir] = true
				dirs = append(dirs, t.dir)
			}
		}
	}
	return dirs
}

func checkGoToolchainDeclared(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	dirs := goModuleDirs(c)
	if len(dirs) == 0 {
		return r.Skipped(noGoTarget)
	}
	problems, err := gomodule.ToolchainProblems(c.Root(), dirs)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return reportErrors(r, problems, fmt.Sprintf("%d go.mod file(s) without a toolchain line", len(problems)), fmt.Sprintf("every go.mod of the %d Go module(s) declares a toolchain line", len(dirs)))
}

// underDir are the repository-relative files lying inside the
// repository-relative directory dir, relative to it.
func underDir(files []string, dir string) []string {
	var out []string
	for _, f := range files {
		if dir == declarations.RootPath {
			out = append(out, f)
		} else if rest, ok := strings.CutPrefix(f, dir+"/"); ok {
			out = append(out, rest)
		}
	}
	return out
}

func checkLdflagsSymbol(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	dirs := goModuleDirs(c)
	if len(dirs) == 0 {
		return r.Skipped(noGoTarget)
	}
	tracked, err := c.Repo().TrackedFiles()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	listed, err := c.Repo().TrackedAndUntrackedFiles()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var modules []gomodule.LdflagsModule
	for _, dir := range dirs {
		rel, err := c.Workspace().RelativePath(dir)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		var nested []string
		for _, m := range c.Workspace().Members() {
			if m.Path != rel && !m.IsRoot() && declarations.IsInside(m.Path, rel) {
				nested = append(nested, underDir([]string{m.Path}, rel)...)
			}
		}
		modules = append(modules, gomodule.LdflagsModule{
			Dir:           dir,
			Tracked:       underDir(tracked, rel),
			Listed:        underDir(listed, rel),
			NestedMembers: nested,
		})
	}
	verdict, err := gomodule.EvaluateLdflags(modules)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if verdict.SkipReason != "" {
		return r.Skipped(verdict.SkipReason)
	}
	for _, note := range verdict.Notes {
		r.Note(note)
	}
	for _, p := range verdict.Problems {
		r.Error(p)
	}
	for _, w := range verdict.Warnings {
		r.Warn(w)
	}
	if len(verdict.Problems) > 0 || len(verdict.Warnings) > 0 {
		return r.Found(fmt.Sprintf("%d -X symbol mismatch(es), %d injected symbol(s) nothing reads", len(verdict.Problems), len(verdict.Warnings)))
	}
	if verdict.Verified > 0 {
		return r.Passed(fmt.Sprintf("%d -X symbol(s) match the Go source", verdict.Verified))
	}
	return r.Passed("no -X linker flag in the tracked build configuration")
}
