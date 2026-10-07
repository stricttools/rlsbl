package targets

import (
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/semver"
)

var v130 = semver.Version{Major: 1, Minor: 3, Patch: 0}

// writeVersion writes v130 for target name into dir and returns the files
// it reported.
func writeVersion(t *testing.T, name, dir string) ([]string, error) {
	t.Helper()
	target, err := Get(name)
	if err != nil {
		t.Fatal(err)
	}
	var written []string
	var writeErr error
	mutate(t, func(e *strictcli.Effects) error {
		written, writeErr = target.WriteVersion(e, dir, v130)
		return nil
	})
	return written, writeErr
}

func TestTheGoVersionLivesInVersion(t *testing.T) {
	hygiene.Isolate(t)
	dir := project(t, map[string]string{"go.mod": "module example.com/portal\n", "VERSION": "1.2.3\n"})
	target, _ := Get(Go)
	if v, err := target.ReadVersion(dir); err != nil || v.String() != "1.2.3" {
		t.Fatalf("ReadVersion = %v, %v", v, err)
	}
	written, err := writeVersion(t, Go, dir)
	if err != nil || !slices.Equal(written, []string{"VERSION"}) || read(t, filepath.Join(dir, "VERSION")) != "1.3.0\n" {
		t.Fatalf("written %q, %v", written, err)
	}
	if _, err := target.ReadVersion(project(t, map[string]string{"go.mod": "module x\n"})); err == nil || !strings.Contains(err.Error(), "holds no VERSION file") {
		t.Fatalf("a missing VERSION: %v", err)
	}
	if _, err := target.ReadVersion(project(t, map[string]string{"VERSION": "1.2.3-rc.1\n"})); err == nil {
		t.Fatal("a pre-release version was read")
	}
	if name, found, err := target.ReadName(dir); err != nil || !found || name != "portal" {
		t.Fatalf("ReadName = %q, %v, %v", name, found, err)
	}
}

func TestThePackageJSONVersionWriteKeepsEveryOtherByte(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ before, after string }{
		{
			"{\n    \"name\": \"portal\",\n    \"version\": \"1.2.3\",\n    \"bin\": {\"portal\": \"cli.js\"}\n}\n",
			"{\n    \"name\": \"portal\",\n    \"version\": \"1.3.0\",\n    \"bin\": {\"portal\": \"cli.js\"}\n}\n",
		},
		{
			"{\"version\":\"1.2.3\",\"name\":\"portal\",\"description\":\"<b> & \\u00e9\"}",
			"{\"version\":\"1.3.0\",\"name\":\"portal\",\"description\":\"<b> & \\u00e9\"}",
		},
		{
			"{\n\t\"name\": \"portal\",\n\t\"config\": {\"version\": \"9.9.9\"},\n\t\"version\" :  \"1.2.3\"\n}",
			"{\n\t\"name\": \"portal\",\n\t\"config\": {\"version\": \"9.9.9\"},\n\t\"version\" :  \"1.3.0\"\n}",
		},
	}
	for _, c := range cases {
		dir := project(t, map[string]string{"package.json": c.before})
		written, err := writeVersion(t, NPM, dir)
		if err != nil || !slices.Equal(written, []string{"package.json"}) {
			t.Fatalf("written %q, %v", written, err)
		}
		if got := read(t, filepath.Join(dir, "package.json")); got != c.after {
			t.Errorf("got:\n%s\nwant:\n%s", got, c.after)
		}
	}
}

func TestThePackageJSONVersionWriteRefusesAnythingButOneTopLevelVersion(t *testing.T) {
	hygiene.Isolate(t)
	for _, c := range []struct{ content, want string }{
		{"{\"name\": \"portal\", \"config\": {\"version\": \"1.0.0\"}}", "declares no top-level \"version\""},
		{"{\"version\": \"1.0.0\", \"version\": \"1.0.1\"}", "declares \"version\" 2 times"},
		{"{\"version\": 1}", "\"version\" is not a string"},
		{"[\"version\"]", "not a JSON object"},
		{"{\"version\": \"1.0.0\"} {}", "more than one JSON value"},
	} {
		dir := project(t, map[string]string{"package.json": c.content})
		if _, err := writeVersion(t, NPM, dir); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: %v", c.content, err)
		}
		if got := read(t, filepath.Join(dir, "package.json")); got != c.content {
			t.Errorf("%s: the refused manifest was changed to %s", c.content, got)
		}
	}
}

func TestTheNpmManifestIsReadStrictly(t *testing.T) {
	hygiene.Isolate(t)
	dir := project(t, map[string]string{"package.json": "{\"name\": \"portal\", \"version\": \"2.0.1\", \"license\": \"MIT\", \"description\": \"A portal\"}"})
	target, _ := Get(NPM)
	if v, err := target.ReadVersion(dir); err != nil || v.String() != "2.0.1" {
		t.Fatalf("ReadVersion = %v, %v", v, err)
	}
	if m, err := target.ReadMetadata(dir); err != nil || m != (Metadata{License: "MIT", Description: "A portal"}) {
		t.Fatalf("ReadMetadata = %+v, %v", m, err)
	}
	if name, found, err := target.ReadName(dir); err != nil || !found || name != "portal" {
		t.Fatalf("ReadName = %q, %v, %v", name, found, err)
	}
	if _, err := target.ReadMetadata(project(t, map[string]string{"package.json": "{\"license\": {\"type\": \"MIT\"}}"})); err == nil {
		t.Fatal("a license that is not a string was read")
	}
}

const pyprojectWithComments = "# the portal\n[project]\nname = \"portal-kit\"\nversion = \"1.2.3\" # bumped by rlsbl\nlicense = { text = \"MIT\" }\ndescription = \"A portal\"\n\n[tool.other]\nkeep = 'this'\n"

func TestThePyprojectVersionWriteKeepsTheFileAndUpdatesTheLiteralDunderVersion(t *testing.T) {
	hygiene.Isolate(t)
	dir := project(t, map[string]string{
		"pyproject.toml":             pyprojectWithComments,
		"src/portal_kit/__init__.py": "\"\"\"The portal.\"\"\"\n\n__version__: str = '1.2.3'  # kept in step\n",
	})
	written, err := writeVersion(t, PyPI, dir)
	if err != nil || !slices.Equal(written, []string{"pyproject.toml", "src/portal_kit/__init__.py"}) {
		t.Fatalf("written %q, %v", written, err)
	}
	if got := read(t, filepath.Join(dir, "pyproject.toml")); got != strings.Replace(pyprojectWithComments, "\"1.2.3\"", "\"1.3.0\"", 1) {
		t.Errorf("pyproject.toml:\n%s", got)
	}
	if got := read(t, filepath.Join(dir, "src/portal_kit/__init__.py")); got != "\"\"\"The portal.\"\"\"\n\n__version__: str = '1.3.0'  # kept in step\n" {
		t.Errorf("__init__.py:\n%s", got)
	}
	target, _ := Get(PyPI)
	if m, err := target.ReadMetadata(dir); err != nil || m != (Metadata{License: "MIT", Description: "A portal"}) {
		t.Errorf("ReadMetadata = %+v, %v", m, err)
	}
}

func TestAComputedDunderVersionIsLeftAlone(t *testing.T) {
	hygiene.Isolate(t)
	initPy := "from importlib.metadata import version\n\n__version__ = version(\"portal\")\n"
	dir := project(t, map[string]string{"pyproject.toml": "[project]\nname = \"portal\"\nversion = \"1.2.3\"\n", "portal/__init__.py": initPy})
	written, err := writeVersion(t, PyPI, dir)
	if err != nil || !slices.Equal(written, []string{"pyproject.toml"}) || read(t, filepath.Join(dir, "portal/__init__.py")) != initPy {
		t.Fatalf("written %q, %v", written, err)
	}
}

func TestAnAmbiguousPackageLayoutIsRefusedUntilItIsDeclared(t *testing.T) {
	hygiene.Isolate(t)
	files := map[string]string{
		"pyproject.toml":         "[project]\nname = \"portal\"\nversion = \"1.2.3\"\n",
		"portal/__init__.py":     "",
		"src/portal/__init__.py": "",
	}
	if _, err := writeVersion(t, PyPI, project(t, files)); err == nil || !strings.Contains(err.Error(), "ambiguous") {
		t.Fatalf("an ambiguous layout: %v", err)
	}
	files["pyproject.toml"] += "\n[tool.hatch.build.targets.wheel]\npackages = [\"src/portal\"]\n"
	dir := project(t, files)
	if root, found, err := PythonPackageRoot(dir); err != nil || !found || root != "src/portal" {
		t.Fatalf("PythonPackageRoot = %q, %v, %v", root, found, err)
	}
}

func TestAPEP440PreReleaseIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	target, _ := Get(PyPI)
	if _, err := target.ReadVersion(project(t, map[string]string{"pyproject.toml": "[project]\nname = \"p\"\nversion = \"1.2.3a0\"\n"})); err == nil {
		t.Fatal("a pre-release version was read")
	}
}
