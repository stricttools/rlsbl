package workflows

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The CI router's path filters, derived and read.
//
// Each member's filter is derived from the workspace itself: the member's
// territory, the territories of every member it depends on (any scope, any
// distance), the manifests and lockfiles at the repository root, the router
// itself, and the changelog file every release of its releasable writes;
// minus the members nested inside it. Nothing is hand-declared.
//
// A push whose diff matches none of a member's patterns leaves its CI jobs
// skipped on the pushed commit, and neither the release's CI check nor the
// publish workflow's wait-for-ci job reads a skipped check as a pass. So the
// release has to know whether a push can trigger a member's jobs, and it
// asks MatchesFilter, which reads the patterns the way dorny/paths-filter
// does under the quantifier the router declares.

// PredicateQuantifier is the dorny/paths-filter quantifier the router
// declares. Under it a file matches a filter when at least one pattern
// without '!' matches it and no negated pattern does; exclusion is final.
// Under the action's default ("some") a negated pattern matches everything
// outside itself, so ['**', '!pkg/**'] would match every file.
const PredicateQuantifier = "some-with-excludes"

// MatchEverything is the pattern matching every path.
const MatchEverything = "**"

// rootLockfiles are the lockfiles a repository root can hold. No target
// declares its lockfile, so they are listed here.
var rootLockfiles = []string{
	"go.sum",
	"go.work",
	"go.work.sum",
	"package-lock.json",
	"pnpm-lock.yaml",
	"poetry.lock",
	"uv.lock",
	"yarn.lock",
}

// RootTriggerFiles are the target manifests and lockfiles present at the
// repository root, sorted. A change to one can change what every member
// builds against, so every member reacts to it. Only present files are
// listed: a router freshness comparison derives from the same tree, so
// adding one makes the committed router stale, as it should.
func RootTriggerFiles(root string) ([]string, error) {
	seen := map[string]bool{}
	var names []string
	for _, t := range targets.All() {
		names = append(names, t.Facts().DetectionFiles...)
	}
	names = append(names, rootLockfiles...)
	var present []string
	for _, name := range names {
		if seen[name] {
			continue
		}
		seen[name] = true
		info, err := os.Stat(filepath.Join(root, name))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if !info.IsDir() {
			present = append(present, name)
		}
	}
	sort.Strings(present)
	return present, nil
}

// territoryPattern is the glob covering a member's directory: "**" for the
// root member, whose territory is everything no other member claims and
// which only Filters can narrow.
func territoryPattern(m declarations.Member) string {
	if m.IsRoot() {
		return MatchEverything
	}
	return m.Path + "/**"
}

// Filters derives every member's router path filter. Building it builds
// the workspace's dependency graph and refuses a graph with a manifest it
// could not read: such a graph is missing edges, so every filter derived from
// it would be narrower than the workspace, and a member would stop reacting
// to a dependency it has.
type Filters struct {
	w        *workspace.Workspace
	graph    *workspace.Graph
	triggers []string
}

// NewFilters builds the filters of the workspace.
func NewFilters(w *workspace.Workspace) (*Filters, error) {
	g := workspace.NewGraph(w)
	if len(g.ScanErrors) > 0 {
		lines := make([]string, 0, len(g.ScanErrors))
		for _, e := range g.ScanErrors {
			rel, err := filepath.Rel(w.Root, e.Path)
			if err != nil {
				rel = e.Path
			}
			lines = append(lines, fmt.Sprintf("  - %s: %s (%v)", e.Member, filepath.ToSlash(rel), e.Err))
		}
		return nil, fmt.Errorf("cannot derive the CI router's path filters: a manifest could not be read, so the dependency graph is missing edges and every filter derived from it would be narrower than the workspace; a member would stop reacting to a dependency it has, and its CI jobs would be skipped on the commit a release tags. Fix each manifest and run this again:\n%s", strings.Join(lines, "\n"))
	}
	triggers, err := RootTriggerFiles(w.Root)
	if err != nil {
		return nil, err
	}
	return &Filters{w: w, graph: g, triggers: triggers}, nil
}

// dependencies are the members m depends on, at any distance and in any
// scope: a change to a dev dependency breaks m's tests, which is what its CI
// runs.
func (f *Filters) dependencies(m declarations.Member) ([]declarations.Member, error) {
	names, err := f.graph.TransitiveDependencies(m.Name, -1)
	if err != nil {
		return nil, fmt.Errorf("cannot derive the CI router's path filter of %q: %w", m.Name, err)
	}
	var out []declarations.Member
	for _, n := range names {
		dep, ok := f.w.Declarations.Member(n)
		if !ok {
			return nil, fmt.Errorf("cannot derive the CI router's path filter of %q: its dependency %q is not a declared member", m.Name, n)
		}
		out = append(out, dep)
	}
	return out, nil
}

// nestedExcludes are "!<path>/**" for each member nested in m, sorted,
// except a nested member whose territory holds one of m's dependencies: an
// exclude is final, so excluding it would hide that dependency from m.
func (f *Filters) nestedExcludes(m declarations.Member, deps []declarations.Member) []string {
	var excludes []string
	for _, p := range f.w.NestedMemberPaths(m) {
		holds := false
		for _, d := range deps {
			if !d.IsRoot() && declarations.IsInside(d.Path, p) {
				holds = true
				break
			}
		}
		if !holds {
			excludes = append(excludes, "!"+p+"/**")
		}
	}
	sort.Strings(excludes)
	return excludes
}

// PatternsFor is the member's filter, in the order the router writes it:
// its own territory, the territories of its dependencies, the root trigger
// files, the router and its releasable's changelog file, then the excludes,
// each group sorted. The root member's filter is "**" and its excludes.
//
// The releasable's changelog file is in every member's filter because a
// release commit always writes it: a release whose writes all fall outside a
// member's directory (a first release, whose version write changes nothing)
// still triggers the member's jobs on the commit it tags, so the jobs run
// rather than conclude skipped, which no rerun could change. The cost,
// accepted: every release runs the CI of every member of the releasable.
// The changelog lines (unreleased.jsonl) are not in the filter, so adding an
// entry spends no CI.
func (f *Filters) PatternsFor(m declarations.Member) ([]string, error) {
	deps, err := f.dependencies(m)
	if err != nil {
		return nil, err
	}
	var patterns []string
	if m.IsRoot() {
		patterns = append(patterns, MatchEverything)
	} else {
		patterns = append(patterns, territoryPattern(m))
		// A dependency on the root member widens to "**", not the root's
		// own narrowed filter: its excludes would cancel this member's own
		// territory.
		seen := map[string]bool{}
		var depPatterns []string
		for _, d := range deps {
			p := territoryPattern(d)
			if !seen[p] {
				seen[p] = true
				depPatterns = append(depPatterns, p)
			}
		}
		sort.Strings(depPatterns)
		patterns = append(patterns, depPatterns...)
		patterns = append(patterns, f.triggers...)
		machinery := []string{RouterPath}
		if m.Versioned() {
			machinery = append(machinery, changelog.Home(f.w.Declarations, m.Releasable))
		}
		sort.Strings(machinery)
		patterns = append(patterns, machinery...)
	}
	patterns = append(patterns, f.nestedExcludes(m, deps)...)
	seen := map[string]bool{}
	var ordered []string
	for _, p := range patterns {
		if !seen[p] {
			seen[p] = true
			ordered = append(ordered, p)
		}
	}
	return ordered, nil
}

// Block is the router's `filters:` text for the members, in the order
// given: one entry per member, its patterns single-quoted.
func (f *Filters) Block(members []declarations.Member) (string, error) {
	var lines []string
	for _, m := range members {
		patterns, err := f.PatternsFor(m)
		if err != nil {
			return "", err
		}
		if len(patterns) == 1 {
			lines = append(lines, m.Name+": "+yamlSingleQuoted(patterns[0]))
			continue
		}
		lines = append(lines, m.Name+":")
		for _, p := range patterns {
			lines = append(lines, "  - "+yamlSingleQuoted(p))
		}
	}
	if len(lines) == 0 {
		return "", nil
	}
	return strings.Join(lines, "\n") + "\n", nil
}

// globRegexp translates a glob the way picomatch reads the router's
// patterns: '*' stops at '/', '**' crosses it, '?' is one character other
// than '/', and everything else is literal.
func globRegexp(pattern string) *regexp.Regexp {
	var b strings.Builder
	b.WriteString("^")
	for i := 0; i < len(pattern); {
		switch {
		case strings.HasPrefix(pattern[i:], "**/"):
			b.WriteString("(?:.*/)?")
			i += 3
		case strings.HasPrefix(pattern[i:], "**"):
			b.WriteString(".*")
			i += 2
		case pattern[i] == '*':
			b.WriteString("[^/]*")
			i++
		case pattern[i] == '?':
			b.WriteString("[^/]")
			i++
		default:
			b.WriteString(regexp.QuoteMeta(pattern[i : i+1]))
			i++
		}
	}
	b.WriteString("$")
	return regexp.MustCompile(b.String())
}

// MatchesPattern reports whether the repository-relative path matches one
// pattern without '!'. "a/**" matches the directory a itself too, as
// picomatch does. Negation belongs to a whole filter (MatchesFilter).
func MatchesPattern(path, pattern string) bool {
	if pattern == MatchEverything {
		return true
	}
	if prefix, ok := strings.CutSuffix(pattern, "/**"); ok {
		prefix = strings.TrimRight(prefix, "/")
		return path == prefix || strings.HasPrefix(path, prefix+"/")
	}
	return globRegexp(pattern).MatchString(path)
}

// MatchesFilter reports whether the path matches a whole filter under
// PredicateQuantifier: some pattern without '!' matches it and no negated
// pattern does, whatever their order. A filter of negated patterns only
// matches nothing.
func MatchesFilter(path string, patterns []string) bool {
	included := false
	for _, p := range patterns {
		if negated, ok := strings.CutPrefix(p, "!"); ok {
			if MatchesPattern(path, negated) {
				return false
			}
		} else if !included && MatchesPattern(path, p) {
			included = true
		}
	}
	return included
}

// AnyPathMatches reports whether a diff touching paths triggers a job
// filtered by patterns: the whole-filter question asked per path, never the
// cross product of paths and patterns, which would read an exclude as one
// more way to match.
func AnyPathMatches(paths, patterns []string) bool {
	for _, p := range paths {
		if MatchesFilter(p, patterns) {
			return true
		}
	}
	return false
}
