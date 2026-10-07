// Package gomodule reads Go modules for rlsbl: go.mod parsed with
// golang.org/x/mod/modfile (the module path, requirements, replacements,
// the toolchain line, retractions), the containment rule for module paths,
// whether each module path still names where the repository lives, the
// packages `go list` enumerates and the main packages among them, go.work's
// use directives, and whether every -X linker flag names a symbol the
// linker can set.
//
// go.mod is parsed strictly: a file the go command would refuse is refused
// here, naming the file, rather than read in part.
package gomodule

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/mod/modfile"
	"golang.org/x/mod/semver"
)

// FileName is the name of a module's manifest.
const FileName = "go.mod"

// WorkFileName is the name of a workspace's go.work.
const WorkFileName = "go.work"

// Parse parses go.mod text; path names the file in errors.
func Parse(path string, data []byte) (*modfile.File, error) {
	f, err := modfile.Parse(path, data, nil)
	if err != nil {
		return nil, fmt.Errorf("%s does not parse as a go.mod: %w", path, err)
	}
	return f, nil
}

// Read parses dir/go.mod. found is false, with no error, when dir holds no
// go.mod.
func Read(dir string) (f *modfile.File, found bool, err error) {
	path := filepath.Join(dir, FileName)
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", path, err)
	}
	f, err = Parse(path, data)
	if err != nil {
		return nil, true, err
	}
	return f, true, nil
}

// ModulePath is the module path dir/go.mod declares. found is false when dir
// holds no go.mod; a go.mod that declares no module directive is an error
// naming it, because nothing then states what the module publishes as.
func ModulePath(dir string) (path string, found bool, err error) {
	f, found, err := Read(dir)
	if err != nil || !found {
		return "", found, err
	}
	if f.Module == nil || f.Module.Mod.Path == "" {
		return "", true, fmt.Errorf("%s declares no module path, so nothing states what this module publishes as; add a module directive", filepath.Join(dir, FileName))
	}
	return f.Module.Mod.Path, true, nil
}

// LastElement is a module path's last element, the name a Go consumer
// resolves the module by.
func LastElement(modulePath string) string {
	return modulePath[strings.LastIndex(modulePath, "/")+1:]
}

// BinaryName is the name `go build` and `go install` give the binary of the
// main package at modulePath: its last element, or the element before it
// when the last is a major version suffix (v2 and up), so
// example.com/portal/v2 builds portal, not v2.
func BinaryName(modulePath string) string {
	last := LastElement(modulePath)
	if last == modulePath || !majorVersionElement(last) {
		return last
	}
	return LastElement(modulePath[:len(modulePath)-len(last)-1])
}

// majorVersionElement reports whether a path element is a major version
// suffix: v followed by digits, at least 2, without a leading zero.
func majorVersionElement(s string) bool {
	if len(s) < 2 || s[0] != 'v' || s[1] == '0' || (s[1] == '1' && len(s) == 2) {
		return false
	}
	for _, r := range s[1:] {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// Require is one require directive.
type Require struct {
	Path    string
	Version string
	// Line is 1-based.
	Line int
}

// Replace is one replace directive.
type Replace struct {
	OldPath string
	NewPath string
	// NewVersion is empty for a directory replacement.
	NewVersion string
	// Line is 1-based.
	Line int
}

// IsLocal reports whether the replacement points at a directory: Go's rule,
// a path starting with ./ or ../, or an absolute one, or . or .. itself.
func (r Replace) IsLocal() bool {
	return modfile.IsDirectoryPath(r.NewPath)
}

// Directives are the require and replace directives of f, in file order.
func Directives(f *modfile.File) ([]Require, []Replace) {
	var requires []Require
	for _, r := range f.Require {
		requires = append(requires, Require{Path: r.Mod.Path, Version: r.Mod.Version, Line: r.Syntax.Start.Line})
	}
	var replaces []Replace
	for _, r := range f.Replace {
		replaces = append(replaces, Replace{OldPath: r.Old.Path, NewPath: r.New.Path, NewVersion: r.New.Version, Line: r.Syntax.Start.Line})
	}
	return requires, replaces
}

// ToolchainFix is the command every missing-toolchain finding names.
const ToolchainFix = "go mod edit -toolchain=<version>"

// ToolchainProblems names each module directory of moduleDirs (absolute)
// whose go.mod declares no toolchain line. The go directive is the oldest Go
// a consumer may build with; the toolchain line is the Go the project
// develops and tests with, and CI's setup-go installs it, falling back to the
// go directive only when the line is absent. Only presence is asked: the line
// is never compared with the Go on this machine, which the repository does
// not own. A directory without a go.mod is passed over.
func ToolchainProblems(root string, moduleDirs []string) ([]string, error) {
	var problems []string
	for _, dir := range moduleDirs {
		f, found, err := Read(dir)
		if err != nil {
			return nil, err
		}
		if !found || f.Toolchain != nil {
			continue
		}
		rel, err := filepath.Rel(root, filepath.Join(dir, FileName))
		if err != nil {
			return nil, err
		}
		rel = filepath.ToSlash(rel)
		where := filepath.ToSlash(filepath.Dir(rel))
		problems = append(problems, fmt.Sprintf("%s declares no toolchain line, so CI's setup-go installs the `go` directive's version (the oldest Go consumers may use) instead of the Go this module is developed with. Declare it: `%s` in %s (e.g. -toolchain=go1.26.6), and commit go.mod.", rel, ToolchainFix, where))
	}
	return problems, nil
}

// Retracted reports whether a retract directive of f covers version
// (vX.Y.Z): a single version or a range holding it.
func Retracted(f *modfile.File, version string) bool {
	for _, r := range f.Retract {
		if semver.Compare(r.Low, version) <= 0 && semver.Compare(version, r.High) <= 0 {
			return true
		}
	}
	return false
}

// AddRetraction appends a retract directive for version (vX.Y.Z) to the
// go.mod text data, keeping every other byte, with rationale (when not
// empty, its whitespace collapsed) as its comment. changed is false when a
// retraction already covers the version. A module version can never be
// removed from the proxy, only retracted, and the retraction takes effect
// once a later version carrying it is published.
func AddRetraction(path string, data []byte, version, rationale string) (out []byte, changed bool, err error) {
	f, err := Parse(path, data)
	if err != nil {
		return nil, false, err
	}
	if !semver.IsValid(version) || semver.Canonical(version) != version {
		return nil, false, fmt.Errorf("%q is not a canonical module version (vMAJOR.MINOR.PATCH)", version)
	}
	if Retracted(f, version) {
		return data, false, nil
	}
	line := "retract " + version
	if r := strings.Join(strings.Fields(rationale), " "); r != "" {
		line += " // " + r
	}
	text := strings.TrimRight(string(data), " \t\r\n")
	out = []byte(text + "\n\n" + line + "\n")
	if _, err := Parse(path, out); err != nil {
		return nil, false, err
	}
	return out, true, nil
}

// WorkUses are the directories root/go.work's use directives name, as
// written. found is false when root holds no go.work.
func WorkUses(root string) (uses []string, found bool, err error) {
	path := filepath.Join(root, WorkFileName)
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", path, err)
	}
	w, err := modfile.ParseWork(path, data, nil)
	if err != nil {
		return nil, true, fmt.Errorf("%s does not parse as a go.work: %w", path, err)
	}
	for _, u := range w.Use {
		uses = append(uses, u.Path)
	}
	return uses, true, nil
}
