package checks

import (
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The Go tag family: what a Go tag may name and which ones a release owes.
// A Go tag is a published artifact (the module proxy resolves it and keeps
// the answer forever), so a wrong one is refused before anything is tagged.
func goTagChecks() []check {
	return []check{
		errorCheck("path-tag-format-go-member", checkPathTagFormatGoMember),
		errorCheck("go-module-major-suffix", checkGoModuleMajorSuffix),
		errorCheck("go-companion-tags", checkGoCompanionTags),
	}
}

// pathTagPattern is a Go path tag scheme, <path>/v{version}.
var pathTagPattern = regexp.MustCompile(`^([^{}]+)/v\{version\}$`)

// majorSuffix is a module path whose last element is a major version of 2
// or more.
var majorSuffix = regexp.MustCompile(`^(.+)/(v(?:[2-9]|[1-9][0-9]+))$`)

// goMemberPaths are the paths of the members carrying a go target.
func goMemberPaths(c *Context, members []declarations.Member) []string {
	var paths []string
	for _, m := range members {
		for _, t := range targetsOf(c, m) {
			if t.target.Name() == targets.Go {
				paths = append(paths, m.Path)
				break
			}
		}
	}
	sort.Strings(paths)
	return paths
}

func checkPathTagFormatGoMember(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	var problems []string
	for _, rel := range c.Workspace().Releasables() {
		scheme, err := workspace.SchemeOf(rel)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		match := pathTagPattern.FindStringSubmatch(scheme.Pattern())
		if match == nil {
			continue
		}
		path := match[1]
		goPaths := goMemberPaths(c, c.Workspace().MembersOf(rel.Name))
		if slices.Contains(goPaths, path) {
			continue
		}
		alternatives := []string{`"{name}@v{version}"`}
		for _, p := range goPaths {
			alternatives = append(alternatives, fmt.Sprintf("%q", p+"/v{version}"))
		}
		problems = append(problems, fmt.Sprintf("the releasable %q declares tag_format %q, a Go module proxy tag for the path %q, but no Go member of %q lives at %q. A Go tag publishes the module at the path it names, permanently, so this one would publish a version of a module %q does not own, or of none. Set tag_format of the releasable %q in %s to one of %s", rel.Name, rel.TagFormat, path, rel.Name, path, rel.Name, rel.Name, declarations.ReleasablesFile, strings.Join(alternatives, ", ")))
	}
	return reportErrors(r, problems, fmt.Sprintf("%d releasable(s) tagging a path none of their Go members has", len(problems)), "every path-style tag_format names a Go member of its releasable")
}

func checkGoModuleMajorSuffix(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	dirs := goModuleDirs(c)
	if len(dirs) == 0 {
		return r.Skipped(noGoTarget)
	}
	var problems []string
	for _, dir := range dirs {
		module, found, err := gomodule.ModulePath(dir)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if !found {
			continue
		}
		match := majorSuffix.FindStringSubmatch(module)
		if match == nil {
			continue
		}
		where := dirLabel(c, dir)
		problems = append(problems, fmt.Sprintf("%s declares the module %q, whose trailing /%s marks major version %s: Go resolves only %s.x.y versions of it. A major version of 2 or more is refused before anything is tagged, because a Go tag is permanent and a stable major is never released by accident. Drop the suffix with `rlsbl rewrite go-module-path --from-module %s --to-module %s`", declarations.Join(where, gomodule.FileName), module, match[2], match[2][1:], match[2], module, match[1]))
	}
	return reportErrors(r, problems, fmt.Sprintf("%d module path(s) with a major-version suffix", len(problems)), "no module path carries a major-version suffix")
}

func checkGoCompanionTags(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	local, err := c.Repo().TagCommits()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var problems, missing []string
	checked := 0
	for _, rel := range c.Workspace().Releasables() {
		dir := releaserecord.ArchiveDir(rel.Name)
		v, found, err := releaserecord.LatestReleasedVersion(c.Root(), dir)
		if err != nil {
			problems = append(problems, fmt.Sprintf("%s: the release record cannot be read: %v", rel.Name, err))
			continue
		}
		if !found {
			continue
		}
		archive, err := releaserecord.ReadArchive(c.Root(), dir, v)
		if err != nil {
			problems = append(problems, fmt.Sprintf("%s: the archive of %s cannot be read: %v", rel.Name, v, err))
			continue
		}
		if archive.Fate != releaserecord.FateRecorded {
			r.Note(fmt.Sprintf("%s: %s is recorded unrecoverable, so there is no release commit its companion tags could stand at", rel.Name, v))
			continue
		}
		scheme, err := workspace.SchemeOf(rel)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		var recorded []string
		for p := range archive.ReleaseCommit.Trees {
			recorded = append(recorded, p)
		}
		sort.Strings(recorded)
		expected, err := targets.ExpectedRefsOf(v, targets.RefInputs{
			SchemeTag:     scheme.Render(v),
			Members:       refMembers(c, rel),
			RecordedPaths: recorded,
			ShippedAs:     archive.ShippedAs,
		})
		if err != nil {
			problems = append(problems, fmt.Sprintf("%s: the refs %s owns cannot be derived: %v", rel.Name, v, err))
			continue
		}
		if len(expected.Companions) == 0 {
			continue
		}
		checked++
		for _, tag := range expected.Companions {
			if _, ok := local[tag]; !ok {
				missing = append(missing, fmt.Sprintf("%s: %s, its latest release, owes the companion tag %s, which does not exist locally. %s", rel.Name, v, tag, reconcileFix))
			}
		}
	}
	for _, p := range problems {
		r.Error(p)
	}
	for _, m := range missing {
		r.Warn(m)
	}
	if len(problems) > 0 || len(missing) > 0 {
		return r.Found(fmt.Sprintf("%d release record problem(s), %d Go companion tag(s) missing", len(problems), len(missing)))
	}
	if checked == 0 {
		return r.Skipped("no latest release owes a Go companion tag")
	}
	return r.Passed(fmt.Sprintf("every Go companion tag of the latest releases of %d releasable(s) exists", checked))
}
