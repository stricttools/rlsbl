package targets

import (
	"path"
	"strings"
)

// The paths a published upload must never carry. A package directory holds
// the project's working state beside its product: planning notes, release
// metadata, agent instructions, scratch output, and secrets. A registry
// keeps what it is sent permanently, so an upload carrying any of them is
// refused before it is published. npm packages and Go module zips are
// listed offline and refused before a release changes anything; a Python
// upload can only be listed by building it, which needs the network, so the
// pypi CI workflow builds it on the candidate commit and runs a standalone
// program over the result. That program is generated from this file's text
// (internal/targets/gen), which is why this file imports only the standard
// library and nothing of rlsbl: the rules rlsbl applies and the rules CI
// applies are one text.
//
// Paths are relative to the package directory: the root of an sdist or npm
// tarball below its top-level directory, the root of a Go module zip below
// module@version/, or a wheel's install root.

// PrivateRootDirs are private only at the package directory's root: the
// same name deeper in may be ordinary source (a todo package, a test's
// screenshots fixture). stricttools/ is the family's earlier metadata root.
var PrivateRootDirs = []string{"todo", "experiments", "screenshots", "stricttools"}

// PrivateDirs are private at any depth: release state, the family's metadata
// roots, and tool and agent state.
var PrivateDirs = []string{".rlsbl", ".rlsbl-monorepo", ".strictmetadata", ".stricttools", ".claude", ".selfdoc", ".strictcli"}

// PrivateFiles are agent instruction files, private at any depth.
var PrivateFiles = []string{"CLAUDE.md", "AGENTS.md"}

// PrivateNamePatterns are private at any depth, for a file or a directory
// alike: environment files, and anything named local-only.
var PrivateNamePatterns = []string{".env*", "*.local-only"}

// FixtureDir holds test fixtures, never the project's own state (Go's
// testdata convention): a fixture may hold any private name as a test's
// input or expected output.
const FixtureDir = "testdata"

// ExcludeEntries are every rule as a gitignore-syntax exclude entry, in
// declaration order; a refusal names its rule in this spelling.
func ExcludeEntries() []string {
	var out []string
	for _, d := range PrivateRootDirs {
		out = append(out, "/"+d+"/")
	}
	for _, d := range PrivateDirs {
		out = append(out, d+"/")
	}
	out = append(out, PrivateFiles...)
	return append(out, PrivateNamePatterns...)
}

// PrivateMatch is why the package-relative path rel is private: its rule (an
// exclude entry) and the private directory holding it, relative to the
// package directory ("" when the path is a private file itself). The
// outermost private component wins, so a whole private directory is one
// rule however deep its files sit, and nothing under a testdata directory is
// private. found is false for a path that is not private.
func PrivateMatch(rel string) (rule, directory string, found bool) {
	var parts []string
	for _, p := range strings.Split(strings.ReplaceAll(rel, "\\", "/"), "/") {
		if p != "" && p != "." {
			parts = append(parts, p)
		}
	}
	last := len(parts) - 1
	for _, p := range parts[:max(last, 0)] {
		if p == FixtureDir {
			return "", "", false
		}
	}
	for i, part := range parts {
		isDir := i < last
		dir := ""
		if isDir {
			dir = strings.Join(parts[:i+1], "/")
		}
		if isDir && i == 0 && contains(PrivateRootDirs, part) {
			return "/" + part + "/", dir, true
		}
		if isDir && contains(PrivateDirs, part) {
			return part + "/", dir, true
		}
		if !isDir && contains(PrivateFiles, part) {
			return part, "", true
		}
		for _, pattern := range PrivateNamePatterns {
			if ok, _ := path.Match(pattern, part); ok {
				return pattern, dir, true
			}
		}
	}
	return "", "", false
}

func contains(list []string, s string) bool {
	for _, item := range list {
		if item == s {
			return true
		}
	}
	return false
}

// PrivatePath is one private path an upload would carry.
type PrivatePath struct {
	Rel       string
	Rule      string
	Directory string
}

// PrivatePathsIn are the private paths among paths, in their order.
func PrivatePathsIn(paths []string) []PrivatePath {
	var found []PrivatePath
	for _, rel := range paths {
		if rule, dir, ok := PrivateMatch(rel); ok {
			found = append(found, PrivatePath{Rel: rel, Rule: rule, Directory: dir})
		}
	}
	return found
}

// PythonFix says how to keep rel out of a Python upload of the given part
// ("sdist" or "wheel"), in the spelling of the build backend that builds it.
func PythonFix(backend, part, rel, rule, directory string) string {
	switch {
	case strings.HasPrefix(backend, "hatchling"):
		return "add \"" + rule + "\" to exclude under [tool.hatch.build.targets." + part + "] in pyproject.toml"
	case strings.HasPrefix(backend, "uv_build"):
		key := "wheel-exclude"
		if part == "sdist" {
			key = "source-exclude"
		}
		return "add \"" + rule + "\" to " + key + " under [tool.uv.build-backend] in pyproject.toml"
	case strings.HasPrefix(backend, "setuptools"):
		if part == "wheel" {
			what := rel
			if directory != "" {
				what = directory
			}
			return "leave " + what + " out of the packages and package data under [tool.setuptools] in pyproject.toml"
		}
		line := "exclude " + rel
		if directory != "" {
			line = "prune " + directory
		}
		return "add \"" + line + "\" to MANIFEST.in"
	}
	shown := backend
	if shown == "" {
		shown = "the default build backend"
	}
	return "exclude " + rule + " from the " + part + " in the configuration of " + shown
}
