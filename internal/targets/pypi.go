package targets

import (
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"

	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/semver"
)

// Pyproject is the Python manifest's file name.
const Pyproject = "pyproject.toml"

// pypiTarget is a pyproject.toml package.
type pypiTarget struct{}

func (pypiTarget) Name() string { return PyPI }

func (pypiTarget) Facts() Facts {
	return Facts{
		Name:                         PyPI,
		Ecosystem:                    "Python / PyPI",
		DetectionFiles:               []string{Pyproject},
		ContentBasedDetection:        true,
		VersionFiles:                 []string{Pyproject, "<package root>/__init__.py"},
		RegistryDisplayName:          "PyPI",
		BuildTimeoutSeconds:          120,
		ProjectInitHint:              "Run \"uv init\" first",
		PublisherBindsToRepository:   true,
		PublisherSetupURL:            "https://pypi.org/manage/account/publishing/",
		ReleaseMaterializationPolicy: MaterializeAlways,
		PackageRename:                RenameManifestField,
		PackageNameField:             "pyproject.toml [project].name",
		BuiltinTestCommand:           strings.Join(pytestArgv, " "),
		TestSettings:                 []string{"pypi_markers"},
		DevInstallGlobal:             "uv tool install -e .",
		DevInstallVenv:               "uv sync --all-packages",
		SharesWorkspaceEnvironment:   true,
		PublishWorkflowAttests:       true,
		SupportsDepFloors:            true,
		ScratchTestExclusion:         ScratchPytestNorecursedirs,
	}
}

// pyproject is a pyproject.toml read whole: its syntax tree, for the
// document order of a table's keys, and its decoded values.
type pyproject struct {
	path   string
	doc    *tomledit.Document
	values map[string]any
}

// readPyproject reads dir/pyproject.toml; found is false when there is none.
func readPyproject(dir string) (pyproject, bool, error) {
	path := filepath.Join(dir, Pyproject)
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return pyproject{}, false, nil
	}
	if err != nil {
		return pyproject{}, false, fmt.Errorf("reading %s: %w", path, err)
	}
	doc, err := tomledit.Parse(data)
	if err != nil {
		return pyproject{}, true, fmt.Errorf("%s: %w", path, err)
	}
	values, err := tomledit.Decode[map[string]any](doc)
	if err != nil {
		return pyproject{}, true, fmt.Errorf("%s: %w", path, err)
	}
	return pyproject{path: path, doc: doc, values: *values}, true, nil
}

// table is the table at the dotted keys, and false when any of them is
// absent or not a table.
func (p pyproject) table(keys ...string) (map[string]any, bool) {
	current := p.values
	for _, k := range keys {
		next, ok := current[k].(map[string]any)
		if !ok {
			return nil, false
		}
		current = next
	}
	return current, true
}

// keysOf are the keys of the table at the dotted keys, in document order;
// none when it is absent or not a table.
func (p pyproject) keysOf(keys ...string) []string {
	record := p.doc.Root()
	for _, k := range keys {
		entry, ok := record.Get(k)
		if !ok {
			return nil
		}
		if record, ok = entry.Record(); !ok {
			return nil
		}
	}
	var out []string
	for e := range record.Entries() {
		out = append(out, e.Key())
	}
	return out
}

// projectTable is the [project] table of dir/pyproject.toml; found is false
// when there is no pyproject.toml or it has no [project] table.
func projectTable(dir string) (map[string]any, bool, error) {
	p, found, err := readPyproject(dir)
	if err != nil || !found {
		return nil, false, err
	}
	project, ok := p.table("project")
	return project, ok, nil
}

// Detect: a pyproject.toml with a [project] table. A pyproject.toml without
// one declares no package: a virtual uv workspace root, or a file holding
// only tool settings.
func (pypiTarget) Detect(dir string) (bool, error) {
	_, found, err := projectTable(dir)
	return found, err
}

// projectString is the [project] string named key; found is false when the
// table does not declare it, and a value that is not a string is refused.
func projectString(dir string, project map[string]any, key string) (string, bool, error) {
	raw, present := project[key]
	if !present {
		return "", false, nil
	}
	s, ok := raw.(string)
	if !ok {
		return "", false, fmt.Errorf("%s: [project].%s is not a string", filepath.Join(dir, Pyproject), key)
	}
	return s, true, nil
}

func (pypiTarget) ReadVersion(dir string) (semver.Version, error) {
	project, found, err := projectTable(dir)
	if err != nil {
		return semver.Version{}, err
	}
	path := filepath.Join(dir, Pyproject)
	if !found {
		return semver.Version{}, fmt.Errorf("%s has no [project] table", path)
	}
	raw, found, err := projectString(dir, project, "version")
	if err != nil {
		return semver.Version{}, err
	}
	if !found {
		return semver.Version{}, fmt.Errorf("%s declares no [project].version", path)
	}
	v, err := semver.Parse(raw)
	if err != nil {
		return semver.Version{}, fmt.Errorf("%s: %w", path, err)
	}
	return v, nil
}

// WriteVersion sets [project].version in place, keeping every other byte of
// pyproject.toml, and the literal __version__ of the package's __init__.py
// when it has one. A file already holding the version is not written and
// not returned.
func (pypiTarget) WriteVersion(w Writer, dir string, v semver.Version) ([]string, error) {
	path := filepath.Join(dir, Pyproject)
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}
	doc, err := tomledit.Parse(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	current, err := doc.GetString("project.version")
	if err != nil {
		return nil, fmt.Errorf("%s: [project].version must be declared as a string to be written: %w", path, err)
	}
	var written []string
	if current != v.String() {
		if err := doc.Set("project.version", v.String()); err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		if err := replaceFile(w, path, doc.Bytes()); err != nil {
			return nil, err
		}
		written = append(written, Pyproject)
	}
	dunder, err := writeDunderVersion(w, dir, v)
	if err != nil {
		return nil, err
	}
	if dunder != "" {
		written = append(written, dunder)
	}
	return written, nil
}

// ReadName is [project].name; found is false when there is no [project]
// table or it declares no name.
func (pypiTarget) ReadName(dir string) (string, bool, error) {
	project, found, err := projectTable(dir)
	if err != nil || !found {
		return "", false, err
	}
	name, found, err := projectString(dir, project, "name")
	if err != nil || !found || name == "" {
		return "", false, err
	}
	return name, true, nil
}

// ReadMetadata reads [project].license (a string, or a table's text) and
// [project].description.
func (pypiTarget) ReadMetadata(dir string) (Metadata, error) {
	project, found, err := projectTable(dir)
	if err != nil || !found {
		return Metadata{}, err
	}
	var m Metadata
	switch license := project["license"].(type) {
	case string:
		m.License = license
	case map[string]any:
		m.License, _ = license["text"].(string)
	}
	if m.Description, _, err = projectString(dir, project, "description"); err != nil {
		return Metadata{}, err
	}
	return m, nil
}

func (pypiTarget) NormalizePackageName(name string) string { return registry.NormalizePypi(name) }

func (pypiTarget) PackageNameProblems(name string) []string { return registry.PypiNameProblems(name) }

// CompanionTags is empty: a PyPI release owes no tag besides its own.
func (pypiTarget) CompanionTags(string, semver.Version) []string { return nil }

// PythonPackageRoot is the directory, relative to dir, holding the import
// package of dir/pyproject.toml's project: hatch's declared wheel packages
// (the first), uv's build-backend module-root, then src/<name>/, <name>/,
// and the raw name on disk, where <name> is the project name with hyphens
// as underscores. found is false when there is no project name or no such
// directory. Both <name>/ and src/<name>/ existing without a declaration is
// refused: which one is the package cannot be told.
func PythonPackageRoot(dir string) (root string, found bool, err error) {
	doc, ok, err := readPyproject(dir)
	if err != nil || !ok {
		return "", false, err
	}
	project, _ := doc.table("project")
	name, _ := project["name"].(string)
	if name == "" {
		return "", false, nil
	}
	if wheel, ok := doc.table("tool", "hatch", "build", "targets", "wheel"); ok {
		if packages, ok := wheel["packages"].([]any); ok && len(packages) > 0 {
			first, ok := packages[0].(string)
			if !ok {
				return "", false, fmt.Errorf("%s: [tool.hatch.build.targets.wheel].packages holds something that is not a string", filepath.Join(dir, Pyproject))
			}
			return path.Clean(first), true, nil
		}
	}
	underscored := strings.ReplaceAll(name, "-", "_")
	if backend, ok := doc.table("tool", "uv", "build-backend"); ok {
		if moduleRoot, ok := backend["module-root"].(string); ok && moduleRoot != "" {
			return path.Join(moduleRoot, underscored), true, nil
		}
	}
	isDir := func(rel string) bool {
		info, err := os.Stat(filepath.Join(dir, filepath.FromSlash(rel)))
		return err == nil && info.IsDir()
	}
	flat, src := isDir(underscored), isDir("src/"+underscored)
	switch {
	case flat && src:
		return "", false, fmt.Errorf("the package layout of %s is ambiguous: both %s/ and src/%s/ exist; declare the package under [tool.hatch.build.targets.wheel] packages or [tool.uv.build-backend] module-root in pyproject.toml", dir, underscored, underscored)
	case src:
		return "src/" + underscored, true, nil
	case flat:
		return underscored, true, nil
	case isDir(name):
		return name, true, nil
	}
	return "", false, nil
}

// dunderLine is a top-level __version__ assignment of one string literal,
// annotated or not, in either quote.
var dunderLine = regexp.MustCompile(`^(__version__\s*(?::\s*str\s*)?=\s*)(["'])([^"'\\\n]*)(["'])(.*)$`)

// writeDunderVersion rewrites the first top-level __version__ assigned a
// string literal in the package's __init__.py, keeping its quote and the
// rest of the file, and returns the file written (relative to dir), or ""
// when there is none to write: no package root, no __init__.py, or no
// __version__ assigned a literal (one computed or imported is the
// package's own business).
func writeDunderVersion(w Writer, dir string, v semver.Version) (string, error) {
	root, found, err := PythonPackageRoot(dir)
	if err != nil || !found {
		return "", err
	}
	rel := path.Join(root, "__init__.py")
	file := filepath.Join(dir, filepath.FromSlash(rel))
	data, err := os.ReadFile(file)
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("reading %s: %w", file, err)
	}
	lines := strings.SplitAfter(string(data), "\n")
	for i, line := range lines {
		body := strings.TrimSuffix(line, "\n")
		ending := line[len(body):]
		trailingCR := strings.HasSuffix(body, "\r")
		body = strings.TrimSuffix(body, "\r")
		m := dunderLine.FindStringSubmatch(body)
		if m == nil || m[2] != m[4] {
			continue
		}
		if m[3] == v.String() {
			return "", nil
		}
		replaced := m[1] + m[2] + v.String() + m[4] + m[5]
		if trailingCR {
			replaced += "\r"
		}
		lines[i] = replaced + ending
		if err := replaceFile(w, file, []byte(strings.Join(lines, ""))); err != nil {
			return "", err
		}
		return rel, nil
	}
	return "", nil
}
