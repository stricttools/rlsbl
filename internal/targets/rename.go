package targets

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// ManifestRename is one manifest's pending package-name rewrite, as
// observed: Occurrences is 1 when the manifest's name field holds the old
// name and 0 when it does not; Text is the whole file with the field
// rewritten (the file as it is when there is nothing to rewrite).
type ManifestRename struct {
	Path        string
	CurrentName string
	Occurrences int
	Text        []byte
}

// RenamePackage plans renaming the package in dir from one name to another
// in its manifest, for a target whose package name is a manifest field (npm's
// package.json "name", pypi's [project].name), keeping every other byte. A
// Go module's name is its module path's last element, renamed by the
// module-path rewrite, and is refused here.
func RenamePackage(t Target, dir, from, to string) (ManifestRename, error) {
	if t.Facts().PackageRename != RenameManifestField {
		return ManifestRename{}, fmt.Errorf("the %s target's package name is not a manifest field (%s); it is renamed by the module-path rewrite", t.Name(), t.Facts().PackageNameField)
	}
	current, _, err := t.ReadName(dir)
	if err != nil {
		return ManifestRename{}, err
	}
	switch t.Name() {
	case NPM:
		p, err := readManifest(dir)
		if err != nil {
			return ManifestRename{}, err
		}
		if current != from {
			return ManifestRename{Path: p.path, CurrentName: current, Text: p.raw}, nil
		}
		text, err := p.withString("name", to)
		if err != nil {
			return ManifestRename{}, err
		}
		return ManifestRename{Path: p.path, CurrentName: current, Occurrences: 1, Text: text}, nil
	case PyPI:
		p, found, err := readPyproject(dir)
		if err != nil {
			return ManifestRename{}, err
		}
		if !found {
			return ManifestRename{}, fmt.Errorf("%s holds no %s", dir, Pyproject)
		}
		raw, err := os.ReadFile(p.path)
		if err != nil {
			return ManifestRename{}, err
		}
		if current != from {
			return ManifestRename{Path: p.path, CurrentName: current, Text: raw}, nil
		}
		if err := p.doc.Set("project.name", to); err != nil {
			return ManifestRename{}, fmt.Errorf("%s: %w", p.path, err)
		}
		return ManifestRename{Path: p.path, CurrentName: current, Occurrences: 1, Text: p.doc.Bytes()}, nil
	}
	return ManifestRename{}, fmt.Errorf("the %s target has no manifest rename", t.Name())
}

// CommandNames are the command-name entries of the manifest in dir that
// spell name: an object "bin" key of package.json, a [project.scripts] or
// [project.gui-scripts] key of pyproject.toml. A package rename leaves them
// alone, since a command name is a decision of its own, and lists them.
func CommandNames(t Target, dir, name string) ([]string, error) {
	switch t.Name() {
	case NPM:
		p, err := readManifest(dir)
		if err != nil {
			return nil, err
		}
		if _, isString, err := p.stringField("bin"); err == nil && isString {
			return nil, nil
		}
		bins, err := binEntries(p)
		if err != nil {
			return nil, err
		}
		for _, b := range bins {
			if b.command == name {
				return []string{fmt.Sprintf("%s: bin %q", p.path, name)}, nil
			}
		}
		return nil, nil
	case PyPI:
		p, found, err := readPyproject(dir)
		if err != nil || !found {
			return nil, err
		}
		var out []string
		for _, table := range []string{"scripts", "gui-scripts"} {
			for _, key := range p.keysOf("project", table) {
				if key == name {
					out = append(out, fmt.Sprintf("%s: [project.%s] %q", p.path, table, name))
				}
			}
		}
		return out, nil
	}
	return nil, nil
}

// SourceDirs are the directories under dir named after the package name
// that a rename leaves in place and lists as a step to take: a Python
// package's import directory, flat or under src/.
func SourceDirs(t Target, dir, name string) []string {
	if t.Name() != PyPI {
		return nil
	}
	module := strings.NewReplacer("-", "_", ".", "_").Replace(name)
	var out []string
	for _, rel := range []string{module, path.Join("src", module)} {
		full := filepath.Join(dir, filepath.FromSlash(rel))
		if info, err := os.Stat(full); err == nil && info.IsDir() {
			out = append(out, full)
		}
	}
	return out
}
