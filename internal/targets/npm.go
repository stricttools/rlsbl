package targets

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/semver"
)

// npmTarget is a package.json package.
type npmTarget struct{}

func (npmTarget) Name() string { return NPM }

func (npmTarget) Facts() Facts {
	return Facts{
		Name:                         NPM,
		Ecosystem:                    "Node.js / npm",
		DetectionFiles:               []string{PackageJSON},
		VersionFiles:                 []string{PackageJSON},
		RegistryDisplayName:          "npm",
		ProjectInitHint:              "Run \"npm init\" first",
		ReleaseMaterializationPolicy: MaterializeAlways,
		PackageRename:                RenameManifestField,
		PackageNameField:             "package.json \"name\"",
		BuiltinTestCommand:           "npm test",
		TestSettings:                 []string{},
		DevInstallGlobal:             "npm link",
		DevInstallVenv:               "npm install",
		SupportsDepFloors:            true,
		ListsUploadOffline:           true,
		ScratchTestExclusion:         ScratchRunnerChosenByProject,
	}
}

func (npmTarget) Detect(dir string) (bool, error) {
	return exists(filepath.Join(dir, PackageJSON))
}

// readManifest reads dir/package.json, refusing its absence.
func readManifest(dir string) (packageJSON, error) {
	p, found, err := readPackageJSON(dir)
	if err != nil {
		return packageJSON{}, err
	}
	if !found {
		return packageJSON{}, fmt.Errorf("%s holds no %s", dir, PackageJSON)
	}
	return p, nil
}

func (npmTarget) ReadVersion(dir string) (semver.Version, error) {
	p, err := readManifest(dir)
	if err != nil {
		return semver.Version{}, err
	}
	raw, found, err := p.stringField("version")
	if err != nil {
		return semver.Version{}, err
	}
	if !found {
		return semver.Version{}, fmt.Errorf("%s declares no top-level \"version\"", p.path)
	}
	v, err := semver.Parse(raw)
	if err != nil {
		return semver.Version{}, fmt.Errorf("%s: %w", p.path, err)
	}
	return v, nil
}

// WriteVersion replaces the value of package.json's one top-level
// "version", keeping every other byte; a manifest with no top-level
// version, with two, or with one that is not a string is refused.
func (npmTarget) WriteVersion(w Writer, dir string, v semver.Version) ([]string, error) {
	p, err := readManifest(dir)
	if err != nil {
		return nil, err
	}
	out, err := p.withString("version", v.String())
	if err != nil {
		return nil, err
	}
	if err := replaceFile(w, p.path, out); err != nil {
		return nil, err
	}
	return []string{PackageJSON}, nil
}

// ReadName is package.json's "name"; found is false when dir holds no
// package.json or it declares no name.
func (npmTarget) ReadName(dir string) (string, bool, error) {
	p, found, err := readPackageJSON(dir)
	if err != nil || !found {
		return "", false, err
	}
	name, found, err := p.stringField("name")
	if err != nil || !found || name == "" {
		return "", false, err
	}
	return name, true, nil
}

// ReadMetadata reads package.json's "license" and "description", each a
// string when present.
func (npmTarget) ReadMetadata(dir string) (Metadata, error) {
	p, found, err := readPackageJSON(dir)
	if err != nil || !found {
		return Metadata{}, err
	}
	var m Metadata
	if m.License, _, err = p.stringField("license"); err != nil {
		return Metadata{}, err
	}
	if m.Description, _, err = p.stringField("description"); err != nil {
		return Metadata{}, err
	}
	return m, nil
}

func (npmTarget) NormalizePackageName(name string) string { return registry.NormalizeNpm(name) }

func (npmTarget) PackageNameProblems(name string) []string { return registry.NpmNameProblems(name) }

// CompanionTags is empty: an npm release owes no tag besides its own.
func (npmTarget) CompanionTags(string, semver.Version) []string { return nil }

// PackageManager is the package manager a package's lockfile names, looked
// for in dir and each directory above it up to the git root: pnpm for
// pnpm-lock.yaml, yarn for yarn.lock, npm for package-lock.json, checked in
// that order in each directory. found is false when no directory up to the
// git root (or the filesystem root) holds one.
func PackageManager(dir string) (manager string, found bool, err error) {
	current, err := filepath.Abs(dir)
	if err != nil {
		return "", false, err
	}
	for {
		for _, lock := range []struct{ file, manager string }{
			{"pnpm-lock.yaml", "pnpm"},
			{"yarn.lock", "yarn"},
			{"package-lock.json", "npm"},
		} {
			ok, err := exists(filepath.Join(current, lock.file))
			if err != nil {
				return "", false, err
			}
			if ok {
				return lock.manager, true, nil
			}
		}
		if info, err := os.Stat(filepath.Join(current, ".git")); err == nil && info.IsDir() {
			return "", false, nil
		}
		parent := filepath.Dir(current)
		if parent == current {
			return "", false, nil
		}
		current = parent
	}
}
