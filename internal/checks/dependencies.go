package checks

import (
	"fmt"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/dependencies"
)

// The dependencies family: what the member's manifests, lockfiles,
// generated validators, and dev overlays say about what it is built
// against, read through internal/dependencies.
func dependencyChecks() []check {
	return []check{
		errorCheck("dep-floors", checkDepFloors),
		errorCheck("dep-locks", checkDepLocks),
		errorCheck("dev-overlay-drift", checkDevOverlayDrift),
		errorCheck("strictspec-generated-format", checkStrictspecGeneratedFormat),
	}
}

// memberTargetDirs are the distinct directories of the targets of the
// member the run answers for, absolute, in target order.
func memberTargetDirs(c *Context) []string {
	var dirs []string
	seen := map[string]bool{}
	for _, t := range targetsOf(c, c.Member()) {
		if !seen[t.dir] {
			seen[t.dir] = true
			dirs = append(dirs, t.dir)
		}
	}
	return dirs
}

// dirLabel names an absolute directory relative to the repository root,
// for a finding about one of several.
func dirLabel(c *Context, dir string) string {
	for _, root := range []string{c.Root(), resolvedPath(c.Root())} {
		rel, err := filepath.Rel(root, dir)
		if err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return filepath.ToSlash(rel)
		}
	}
	return dir
}

// labelled prefixes each line with the directory it is about when the
// check read more than one.
func labelled(c *Context, dir string, several bool, lines []string) []string {
	if !several {
		return lines
	}
	out := make([]string, len(lines))
	for i, l := range lines {
		out[i] = dirLabel(c, dir) + ": " + l
	}
	return out
}

func checkDepFloors(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	m := c.Member()
	dirs := memberTargetDirs(c)
	if len(dirs) == 0 {
		return r.Skipped(fmt.Sprintf("the member %q has no target", m.Name))
	}
	seen := map[string]bool{}
	var names []string
	for _, n := range append(append([]string(nil), m.InternalDepFloors...), dependencies.WorkspacePackageNames(c.Declarations())...) {
		if !seen[n] {
			seen[n] = true
			names = append(names, n)
		}
	}
	var problems, notes []string
	for _, dir := range dirs {
		v, err := dependencies.EvaluateFloors(dir, names)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		problems = append(problems, labelled(c, dir, len(dirs) > 1, v.Problems)...)
		notes = append(notes, labelled(c, dir, len(dirs) > 1, v.Notes)...)
	}
	passed := "every ecosystem-internal dependency declares a floor at the locked version"
	if len(notes) > 0 {
		passed = strings.Join(firstOf(notes, 3), "; ")
	}
	return reportErrors(r, problems, fmt.Sprintf("%d lagging dependency floor(s)", len(problems)), passed)
}

// firstOf is at most n leading elements of list.
func firstOf(list []string, n int) []string {
	if len(list) > n {
		return list[:n]
	}
	return list
}

func checkDepLocks(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	dirs := memberTargetDirs(c)
	if len(dirs) == 0 {
		return r.Skipped(fmt.Sprintf("the member %q has no target", c.Member().Name))
	}
	var problems, notes, skips []string
	for _, dir := range dirs {
		v, err := dependencies.EvaluateLocks(dir)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if v.SkipReason != "" {
			skips = append(skips, labelled(c, dir, len(dirs) > 1, []string{v.SkipReason})...)
			continue
		}
		problems = append(problems, labelled(c, dir, len(dirs) > 1, v.Problems)...)
		notes = append(notes, labelled(c, dir, len(dirs) > 1, v.Notes)...)
	}
	if len(skips) == len(dirs) {
		return r.Skipped(strings.Join(skips, "; "))
	}
	passed := "every lockfile resolves the manifest beside it"
	if len(notes) > 0 {
		passed = strings.Join(firstOf(notes, 3), "; ")
	}
	return reportErrors(r, problems, fmt.Sprintf("%d stale lock finding(s)", len(problems)), passed)
}

func checkDevOverlayDrift(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	dir := c.Workspace().MemberDir(c.Member())
	recorded, found, err := dependencies.LoadSentinel(dir)
	if err != nil {
		return reportErrors(r, []string{err.Error()}, "the dev overlay sentinel cannot be read", "")
	}
	if !found {
		return r.Skipped(fmt.Sprintf("no dev overlay was ever synced here (no %s)", dependencies.SentinelFile))
	}
	if len(recorded) == 0 {
		return r.Skipped(dependencies.SentinelFile + " records no overlay")
	}
	var drifted []string
	for _, o := range recorded {
		installed, err := dependencies.InspectInstalled(dir, c.UVProjectEnvironment(), o.Package)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if state, detail := dependencies.ClassifyOverlay(o, installed); state != dependencies.OverlayHealthy {
			drifted = append(drifted, detail)
		}
	}
	return reportErrors(r, drifted, fmt.Sprintf("%d of %d dev overlay(s) wiped or missing: a bare `uv sync` or `uv run` reinstalled registry wheels; run `rlsbl dev sync` to restore the editable overlays", len(drifted), len(recorded)), fmt.Sprintf("all %d dev overlay(s) are editable installs of their declared checkouts", len(recorded)))
}

func checkStrictspecGeneratedFormat(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	v, err := dependencies.EvaluateGeneratedFormat(c.Workspace().MemberDir(c.Member()))
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if v.SkipReason != "" {
		return r.Skipped(v.SkipReason)
	}
	passed := "every generated validator declares a format the strictspec runtime reads"
	if len(v.Notes) > 0 {
		passed = strings.Join(firstOf(v.Notes, 3), "; ")
	}
	return reportErrors(r, v.Problems, fmt.Sprintf("%d unreadable generated validator(s)", len(v.Problems)), passed)
}
