package dependencies_test

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/dependencies"
)

// pyprojectWith writes a pyproject.toml declaring deps, and groups when
// given.
func pyprojectWith(t *testing.T, dir string, deps []string, extras, groups map[string][]string) {
	t.Helper()
	quoted := func(xs []string) string {
		var out []string
		for _, x := range xs {
			out = append(out, fmt.Sprintf("%q", x))
		}
		return "[" + strings.Join(out, ", ") + "]"
	}
	text := "[project]\nname = \"consumer\"\nversion = \"0.1.0\"\ndependencies = " + quoted(deps) + "\n"
	sections := func(header string, m map[string][]string) {
		if len(m) == 0 {
			return
		}
		text += "\n[" + header + "]\n"
		var keys []string
		for k := range m {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			text += k + " = " + quoted(m[k]) + "\n"
		}
	}
	sections("project.optional-dependencies", extras)
	sections("dependency-groups", groups)
	write(t, dir, "pyproject.toml", text)
}

// uvLock writes a uv.lock resolving packages.
func uvLock(t *testing.T, dir string, packages map[string]string) {
	t.Helper()
	text := "version = 1\n\n[[package]]\nname = \"consumer\"\nversion = \"0.1.0\"\nsource = { editable = \".\" }\n"
	var names []string
	for n := range packages {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		text += fmt.Sprintf("\n[[package]]\nname = %q\nversion = %q\nsource = { registry = \"https://pypi.org/simple\" }\n", n, packages[n])
	}
	write(t, dir, "uv.lock", text)
}

func floors(t *testing.T, dir string, names ...string) dependencies.Verdict {
	t.Helper()
	v, err := dependencies.EvaluateFloors(dir, names)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func joined(lines []string) string { return strings.Join(lines, "\n") }

func TestAPypiDependencyWithoutAFloorIsAProblem(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	pyprojectWith(t, dir, []string{"strictcli"}, nil, nil)
	uvLock(t, dir, map[string]string{"strictcli": "0.36.0"})
	v := floors(t, dir, "strictcli")
	if v.OK() || !strings.Contains(joined(v.Problems), "declares no version floor") || !strings.Contains(joined(v.Problems), `"strictcli>=0.36.0"`) {
		t.Fatalf("%+v", v)
	}
	// Declaring the floor the problem names clears it.
	pyprojectWith(t, dir, []string{"strictcli>=0.36.0"}, nil, nil)
	v = floors(t, dir, "strictcli")
	if !v.OK() || !strings.Contains(joined(v.Notes), "pypi: strictcli '>=0.36.0' covers the locked 0.36.0") {
		t.Fatalf("%+v", v)
	}
}

func TestPypiFloorsAreComparedAtMajorMinor(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		declared, locked string
		ok               bool
	}{
		{"strictcli>=0.30", "0.36.0", false},
		{"strictcli>=0.36.0", "0.36.9", true},
		{"strictcli>=0.36.0", "1.0.0", false},
		{"strictcli==0.36.0", "0.36.0", true},
		{"strictcli<1", "0.36.0", false},
		{"Strict_CLI[extra]>=0.36; python_version>'3'", "0.36.0", true},
	}
	for _, c := range cases {
		dir := t.TempDir()
		pyprojectWith(t, dir, []string{c.declared}, nil, nil)
		name := strings.FieldsFunc(c.declared, func(r rune) bool { return strings.ContainsRune("[<>=;", r) })[0]
		uvLock(t, dir, map[string]string{name: c.locked})
		if v := floors(t, dir, "strict-cli", "strictcli"); v.OK() != c.ok {
			t.Errorf("%s against %s: %+v", c.declared, c.locked, v)
		}
	}
}

func TestEveryPypiSectionIsPolicedAndTheRuntimeOneWins(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	pyprojectWith(t, dir, nil, map[string][]string{"cli": {"clikit>=0.1"}}, map[string][]string{"dev": {"testkit"}})
	uvLock(t, dir, map[string]string{"clikit": "0.5.0", "testkit": "0.3.1"})
	v := floors(t, dir, "clikit", "testkit")
	problems := joined(v.Problems)
	if !strings.Contains(problems, "[project].optional-dependencies.cli") || !strings.Contains(problems, "[dependency-groups].dev") {
		t.Fatalf("%+v", v)
	}
	pyprojectWith(t, dir, []string{"testkit>=0.3"}, nil, map[string][]string{"dev": {"testkit"}})
	if v := floors(t, dir, "testkit"); !v.OK() {
		t.Errorf("the runtime declaration did not win over the group: %+v", v)
	}
}

func TestWhatIsNotPolicedIsLeftAlone(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	pyprojectWith(t, dir, []string{"requests", "core @ file:///abs/core"}, nil, nil)
	uvLock(t, dir, map[string]string{"requests": "2.0.0", "core": "1.0.0", "transitive": "3.0.0"})
	v := floors(t, dir, "core", "transitive")
	if !v.OK() || !strings.Contains(joined(v.Notes), "pypi: pyproject.toml declares none of the enforced") {
		t.Fatalf("%+v", v)
	}
	if v := floors(t, dir); !v.OK() || joined(v.Notes) != "no ecosystem-internal dependencies to enforce" {
		t.Errorf("an empty list: %+v", v)
	}
}

func TestAMemberIsComparedAgainstTheWorkspaceRootLock(t *testing.T) {
	hygiene.Isolate(t)
	root := workspaceRoot(t, `["packages/*"]`, "")
	member := mkdir(t, root, "packages/widget")
	pyprojectWith(t, member, []string{"strictcli>=0.30"}, nil, nil)
	uvLock(t, root, map[string]string{"strictcli": "0.36.0"})
	if v := floors(t, member, "strictcli"); v.OK() {
		t.Fatalf("the member's floor went unpoliced: %+v", v)
	}
	write(t, root, "uv.lock", "not toml [")
	v := floors(t, member, "strictcli")
	if v.OK() || !strings.Contains(joined(v.Problems), "could not be read") {
		t.Errorf("an unreadable root lock: %+v", v)
	}
}

func TestNoLockIsANote(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	pyprojectWith(t, dir, []string{"strictcli"}, nil, nil)
	write(t, dir, "package.json", `{"name": "consumer", "version": "0.1.0", "dependencies": {"kit": "*"}}`)
	v := floors(t, dir, "strictcli", "kit")
	if !v.OK() || !strings.Contains(joined(v.Notes), "pypi: no uv.lock -- probed") || !strings.Contains(joined(v.Notes), "npm: no package-lock.json") {
		t.Fatalf("%+v", v)
	}
}

func packageJSON(t *testing.T, dir string, doc map[string]any) {
	t.Helper()
	data, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	write(t, dir, "package.json", string(data))
}

func TestNpmFloors(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	packageJSON(t, dir, map[string]any{"name": "consumer", "dependencies": map[string]any{"kit": "*"}, "peerDependencies": map[string]any{"peer": "^0.2.0"}})
	write(t, dir, "package-lock.json", `{"lockfileVersion": 3, "packages": {"": {}, "node_modules/kit": {"version": "1.2.0"}, "node_modules/peer": {"version": "0.4.0"}, "node_modules/x/node_modules/kit": {"version": "0.1.0"}}}`)
	v := floors(t, dir, "kit", "peer")
	if len(v.Problems) != 2 || !strings.Contains(joined(v.Problems), `"kit": ">=1.2.0"`) || !strings.Contains(joined(v.Problems), "package.json peerDependencies") {
		t.Fatalf("%+v", v)
	}
	packageJSON(t, dir, map[string]any{"name": "consumer", "dependencies": map[string]any{"kit": "^1.2.0", "peer": "workspace:*"}})
	v = floors(t, dir, "kit", "peer")
	if !v.OK() || !strings.Contains(joined(v.Notes), "npm: kit '^1.2.0' covers the locked 1.2.0") || strings.Contains(joined(v.Notes), "peer") {
		t.Fatalf("%+v", v)
	}
	write(t, dir, "package-lock.json", `{"lockfileVersion": 1, "dependencies": {"kit": {"version": "1.3.0"}}}`)
	if v := floors(t, dir, "kit"); v.OK() {
		t.Errorf("a version 1 lock was not read: %+v", v)
	}
}

func TestGoIsSatisfiedWithANoteAndDoesNotHideAPypiProblem(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	write(t, dir, "go.mod", "module example.com/consumer\n\ngo 1.26\n")
	pyprojectWith(t, dir, []string{"strictcli"}, nil, nil)
	uvLock(t, dir, map[string]string{"strictcli": "0.36.0"})
	v := floors(t, dir, "strictcli")
	if v.OK() || !strings.Contains(joined(v.Notes), "go: go.mod require lines ARE the declared minimums") {
		t.Fatalf("%+v", v)
	}
}

func TestWorkspaceSiblingsAreInternal(t *testing.T) {
	hygiene.Isolate(t)
	d := &declarations.Releasables{Layout: declarations.LayoutWorkspace, Members: []declarations.Member{
		{Path: ".", Name: "root"}, {Path: "widget", Name: "widget", RegistryName: "widget-kit"}, {Path: "gadget", Name: "gadget"},
	}}
	if got := strings.Join(dependencies.WorkspacePackageNames(d), ","); got != "root,widget-kit,gadget" {
		t.Errorf("got %s", got)
	}
	standalone := &declarations.Releasables{Layout: declarations.LayoutStandalone, Members: []declarations.Member{{Path: ".", Name: "root"}}}
	if got := dependencies.WorkspacePackageNames(standalone); got != nil {
		t.Errorf("a standalone repository has siblings: %v", got)
	}
}

func TestFloorParsers(t *testing.T) {
	hygiene.Isolate(t)
	render := func(r dependencies.FloorReading, v [2]int) string {
		switch r {
		case dependencies.FloorFound:
			return fmt.Sprintf("found %d.%d", v[0], v[1])
		case dependencies.FloorNotApplicable:
			return "not applicable"
		}
		return "none"
	}
	pypi := [][2]string{
		{">=1.2", "found 1.2"},
		{">=1.2,<2", "found 1.2"},
		{"~=0.36.0", "found 0.36"},
		{"===0.5.0", "found 0.5"},
		{">1.0,>=1.4", "found 1.4"},
		{"<2", "none"},
		{"!=1.0", "none"},
		{"", "none"},
		{"==1.0.*", "found 1.0"},
		{">=v2.3.4", "found 2.3"},
		{">=1.2, != 1.3", "found 1.2"},
	}
	for _, c := range pypi {
		if got := render(dependencies.PypiFloor(c[0])); got != c[1] {
			t.Errorf("pypi %q: %s, want %s", c[0], got, c[1])
		}
	}
	npm := [][2]string{
		{"^1.2.0", "found 1.2"},
		{"~0.3.1", "found 0.3"},
		{">=1.0.0 <2", "found 1.0"},
		{"*", "none"},
		{"x", "none"},
		{"1.x", "none"},
		{"^1.0.0 || ^2.0.0", "none"},
		{"workspace:*", "not applicable"},
		{"file:../kit", "not applicable"},
		{"git+https://x/y", "not applicable"},
		{"", "none"},
	}
	for _, c := range npm {
		if got := render(dependencies.NpmFloor(c[0])); got != c[1] {
			t.Errorf("npm %q: %s, want %s", c[0], got, c[1])
		}
	}
}
