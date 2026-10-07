// Package targets is rlsbl's release target protocol for go, npm, and pypi:
// detecting a target from its manifests, reading and writing its version
// (VERSION, package.json, pyproject.toml with the package's __version__),
// reading its package name, license, and description, the companion tags a
// Go module owes, the test command a target's built-in runner runs, the
// Python build, the files an upload would carry and the private paths it
// must never carry (with the standalone program pypi CI runs over a built
// upload), the exclusions scaffold writes into a project's own runner and
// build configuration, whether a project is a strictcli program and its
// entry point, the local-install commands, and the support matrix.
//
// Every per-target fact is declared once, in the target's Facts, and the
// support matrix is rendered from them.
package targets

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

// The release targets, by name: the names releasables.toml declares.
const (
	Go   = declarations.TargetGo
	NPM  = declarations.TargetNPM
	PyPI = declarations.TargetPyPI
)

// Writer writes files: the strictcli effects handle.
type Writer interface {
	Write(path interface{}, content interface{}, opts ...strictcli.EffectOption) (strictcli.Unsettled, error)
	Rename(src interface{}, dst interface{}, opts ...strictcli.EffectOption) (strictcli.Unsettled, error)
}

// Runner starts programs: the strictcli effects handle.
type Runner interface {
	Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error)
}

// Handle is the whole of the effects handle a build or an offline upload
// listing needs: programs, files, and the scratch directories they make and
// remove.
type Handle interface {
	Runner
	Writer
	Mkdir(path interface{}, opts ...strictcli.EffectOption) (strictcli.Unsettled, error)
	Remove(path interface{}, opts ...strictcli.EffectOption) (strictcli.Unsettled, error)
}

// replaceFile writes content to path through a temporary file renamed over
// it, so no reader sees a partly written manifest.
func replaceFile(w Writer, path string, content []byte) error {
	tmp := path + ".rlsbl-writing"
	if _, err := w.Write(tmp, string(content)); err != nil {
		return err
	}
	_, err := w.Rename(tmp, path)
	return err
}

// Metadata is what a manifest says about its package beyond its name and
// version; an empty field is one the manifest does not state.
type Metadata struct {
	License     string
	Description string
}

// Target is one release target.
type Target interface {
	// Name is go, npm, or pypi.
	Name() string
	// Facts are the target's declared per-target facts.
	Facts() Facts
	// Detect reports whether dir holds a project of this target.
	Detect(dir string) (bool, error)
	// ReadVersion reads the version from the target's version file in dir.
	ReadVersion(dir string) (semver.Version, error)
	// WriteVersion writes v into the target's version files in dir, keeping
	// every other byte of them, and returns the files written, relative to
	// dir.
	WriteVersion(w Writer, dir string, v semver.Version) ([]string, error)
	// ReadName reads the package name from the manifest in dir; found is
	// false when dir holds no manifest.
	ReadName(dir string) (name string, found bool, err error)
	// ReadMetadata reads the license and description from the manifest in
	// dir.
	ReadMetadata(dir string) (Metadata, error)
	// NormalizePackageName folds a name into the form the registry compares
	// names by.
	NormalizePackageName(name string) string
	// PackageNameProblems says why name cannot be this target's package
	// name; empty when it can.
	PackageNameProblems(name string) []string
	// CompanionTags are the tags a release of version owes besides its
	// primary tag for a member at memberPath (repository-relative, "." for
	// the root) carrying this target.
	CompanionTags(memberPath string, version semver.Version) []string
}

// supported is every target rlsbl supports, in name order.
var supported = []Target{goTarget{}, npmTarget{}, pypiTarget{}}

// All are the supported targets, in name order.
func All() []Target { return append([]Target(nil), supported...) }

// Names are the supported targets' names, in name order.
func Names() []string {
	names := make([]string, len(supported))
	for i, t := range supported {
		names[i] = t.Name()
	}
	return names
}

// Get is the target named name; a name rlsbl does not support is refused,
// naming every target it does.
func Get(name string) (Target, error) {
	for _, t := range supported {
		if t.Name() == name {
			return t, nil
		}
	}
	return nil, fmt.Errorf("%q is not a release target rlsbl supports; the supported targets are %s", name, strings.Join(Names(), ", "))
}

// Detect are the targets whose manifests dir holds, in name order, each
// with an empty path (the directory itself).
func Detect(dir string) ([]declarations.Target, error) {
	var found []declarations.Target
	for _, t := range supported {
		ok, err := t.Detect(dir)
		if err != nil {
			return nil, err
		}
		if ok {
			found = append(found, declarations.Target{Name: t.Name()})
		}
	}
	return found, nil
}

// MemberTargets are the member's targets: the ones it declares, or, when it
// declares none, the ones detected in its directory under root (the
// repository root, absolute). A declared target naming an unsupported
// target, or a directory that does not exist, is refused.
func MemberTargets(root string, m declarations.Member) ([]declarations.Target, error) {
	if m.Targets == nil {
		return Detect(filepath.Join(root, filepath.FromSlash(m.Path)))
	}
	for _, t := range m.Targets {
		if _, err := Get(t.Name); err != nil {
			return nil, fmt.Errorf("the member %q declares a target: %w", m.Name, err)
		}
		dir := filepath.Join(root, filepath.FromSlash(m.TargetDir(t)))
		info, err := os.Stat(dir)
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("the member %q declares the %s target at %s, which does not exist: create the package there, or correct the target's path in .strictmetadata/releasables/releasables.toml", m.Name, t.Name, m.TargetDir(t))
		}
		if err != nil {
			return nil, err
		}
		if !info.IsDir() {
			return nil, fmt.Errorf("the member %q declares the %s target at %s, which is not a directory", m.Name, t.Name, m.TargetDir(t))
		}
	}
	return append([]declarations.Target(nil), m.Targets...), nil
}
