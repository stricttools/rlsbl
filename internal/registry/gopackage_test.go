package registry

import (
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestTheGoStdlibTableShape(t *testing.T) {
	hygiene.Isolate(t)
	table := GoStdlib()
	if table.Generator != "internal/registry/gen" || !strings.Contains(table.Source, "go list std") || !strings.HasPrefix(table.GoVersion, "go1.") {
		t.Fatalf("stamp: %+v", table)
	}
	if !sort.StringsAreSorted(table.Packages) || len(slices.Compact(slices.Clone(table.Packages))) != len(table.Packages) {
		t.Fatal("the packages are not sorted and unique")
	}
	for _, p := range table.Packages {
		for _, part := range strings.Split(p, "/") {
			if part == "internal" || part == "vendor" {
				t.Errorf("%s is not importable", p)
			}
		}
	}
	for _, p := range []string{"syscall/js", "log/syslog", "testing", "encoding/json"} {
		if !slices.Contains(table.Packages, p) {
			t.Errorf("%s is missing", p)
		}
	}
}

func TestThePythonStdlibTableShape(t *testing.T) {
	hygiene.Isolate(t)
	table := PythonStdlib()
	if table.Generator != "internal/registry/gen" || table.PythonVersion == "" || !sort.StringsAreSorted(table.Modules) {
		t.Fatalf("stamp: %s %s", table.Generator, table.PythonVersion)
	}
	for _, m := range []string{"json", "os", "asyncio", "tomllib"} {
		if !slices.Contains(table.Modules, m) {
			t.Errorf("%s is missing", m)
		}
	}
}

func TestInvalidGoPackageNames(t *testing.T) {
	hygiene.Isolate(t)
	v := JudgeGoPackageName("go-toml-edit")
	if v.Status != StatusInvalid || v.Reason != "not-identifier" || !strings.Contains(v.Note, "package clause") || !strings.Contains(v.Note, "does not choose") || len(v.Conflicts) != 0 {
		t.Errorf("go-toml-edit: %+v", v)
	}
	if !strings.Contains(v.Note, "('-' not allowed)") {
		t.Errorf("the dash is not named: %s", v.Note)
	}
	for _, name := range []string{"9lives", "has.dot", "has space", ""} {
		if v := JudgeGoPackageName(name); v.Status != StatusInvalid || v.Reason != "not-identifier" {
			t.Errorf("%q: %+v", name, v)
		}
	}
	for _, name := range []string{"func", "type", "range", "go", "package"} {
		if v := JudgeGoPackageName(name); v.Status != StatusInvalid || v.Reason != "keyword" {
			t.Errorf("%q: %+v", name, v)
		}
	}
	if v := JudgeGoPackageName("_"); v.Status != StatusInvalid || v.Reason != "blank" {
		t.Errorf("_: %+v", v)
	}
}

func TestStdlibCollisions(t *testing.T) {
	hygiene.Isolate(t)
	if v := JudgeGoPackageName("testing"); v.Status != StatusTaken || v.Reason != "stdlib" || !slices.Equal(v.Conflicts, []string{"testing"}) {
		t.Errorf("testing: %+v", v)
	}
	if v := JudgeGoPackageName("json"); !slices.Equal(v.Conflicts, []string{"encoding/json"}) || !strings.Contains(v.Note, "encoding/json") {
		t.Errorf("json: %+v", v)
	}
	if v := JudgeGoPackageName("template"); !slices.Equal(v.Conflicts, []string{"html/template", "text/template"}) {
		t.Errorf("template: %+v", v)
	}
	if v := JudgeGoPackageName("rand"); !slices.Contains(v.Conflicts, "math/rand/v2") {
		t.Errorf("rand: %+v", v)
	}
	for _, name := range []string{"v2", "abi", "bytealg", "poll"} {
		if v := JudgeGoPackageName(name); v.Status != StatusAvailable {
			t.Errorf("%s: %+v", name, v)
		}
	}
}

func TestDiscouragedGoPackageNames(t *testing.T) {
	hygiene.Isolate(t)
	for name, reason := range map[string]string{"testSandbox": "uppercase", "test_sandbox": "underscore", "len": "predeclared", "string": "predeclared"} {
		if v := JudgeGoPackageName(name); v.Status != StatusDiscouraged || v.Reason != reason {
			t.Errorf("%s: %+v", name, v)
		}
	}
	v := JudgeGoPackageName("Test_Sandbox")
	if v.Reason != "uppercase" || !strings.Contains(v.Note, "uppercase") || !strings.Contains(v.Note, "underscore") {
		t.Errorf("Test_Sandbox: %+v", v)
	}
}

func TestAvailableGoPackageNames(t *testing.T) {
	hygiene.Isolate(t)
	for _, name := range []string{"testsandbox", "base64x", "rlsbl"} {
		if v := JudgeGoPackageName(name); v.Status != StatusAvailable || v.Reason != "" || v.Note != "" {
			t.Errorf("%s: %+v", name, v)
		}
	}
}

func TestPackageNameOf(t *testing.T) {
	hygiene.Isolate(t)
	for path, want := range map[string]string{"math/rand/v2": "rand", "encoding/json": "json", "v2": "v2", "a/v1": "v1", "a/v10": "a"} {
		if got := PackageNameOf(path); got != want {
			t.Errorf("%s: %q, want %q", path, got, want)
		}
	}
}
