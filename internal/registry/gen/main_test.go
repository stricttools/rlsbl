package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"regexp"
	"slices"
	"strconv"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// releaseLine reads go1.26.6 or 3.14 as its major and minor numbers.
func releaseLine(t *testing.T, version string) [2]int {
	t.Helper()
	m := regexp.MustCompile(`^(?:go)?([0-9]+)\.([0-9]+)`).FindStringSubmatch(version)
	if m == nil {
		t.Fatalf("unrecognized version %q", version)
	}
	major, _ := strconv.Atoi(m[1])
	minor, _ := strconv.Atoi(m[2])
	return [2]int{major, minor}
}

func older(a, b [2]int) bool { return a[0] < b[0] || (a[0] == b[0] && a[1] < b[1]) }

// The committed Go table matches `go list std` under the local toolchain
// when the toolchain is on the table's release line; a newer line asks for
// regeneration, and an older one cannot judge a newer table, which fails
// rather than leaving the table unchecked.
func TestTheGoStdlibTableIsFresh(t *testing.T) {
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoCache))
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no Go toolchain on PATH: the freshness of the Go standard-library table cannot be checked here")
	}
	var committed goTable
	data, err := os.ReadFile("../go-stdlib-packages.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &committed); err != nil {
		t.Fatal(err)
	}
	local, err := localGoVersion()
	if err != nil {
		t.Fatal(err)
	}
	if older(releaseLine(t, local), releaseLine(t, committed.GoVersion)) {
		t.Fatalf("the local Go toolchain %s is older than the table's %s, and an older release line cannot judge a newer table, so its freshness is unchecked; run the suite with a toolchain on the table's release line", local, committed.GoVersion)
	}
	fresh, err := listStd()
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(fresh, committed.Packages) {
		t.Fatalf("internal/registry/go-stdlib-packages.json (from %s) no longer matches `go list std` under %s; run `go run ./internal/registry/gen` from the repository root and commit the result", committed.GoVersion, local)
	}
	if releaseLine(t, local) != releaseLine(t, committed.GoVersion) {
		t.Fatalf("the local Go toolchain %s is on a newer release line than the table's %s; run `go run ./internal/registry/gen` from the repository root and commit the result", local, committed.GoVersion)
	}
}

// The committed Python table matches sys.stdlib_module_names of the python3
// on PATH when it is the table's release; a newer one asks for
// regeneration, and an older one cannot judge it.
func TestThePythonStdlibTableIsFresh(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("no python3 on PATH: the freshness of the Python standard-library table cannot be checked here")
	}
	var committed pythonTable
	data, err := os.ReadFile("../python-stdlib-modules.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &committed); err != nil {
		t.Fatal(err)
	}
	fresh, version, err := freshPythonTable()
	if err != nil {
		t.Fatal(err)
	}
	if older(releaseLine(t, version), releaseLine(t, committed.PythonVersion)) {
		t.Skipf("the local python3 %s is older than the table's %s", version, committed.PythonVersion)
	}
	if string(fresh) != string(data) {
		t.Fatalf("internal/registry/python-stdlib-modules.json (from Python %s) does not match python3 %s; run `go run ./internal/registry/gen` from the repository root and commit the result", committed.PythonVersion, version)
	}
}

// The tables are rendered the way the committed files are written.
func TestTheRenderingMatchesTheCommittedFiles(t *testing.T) {
	hygiene.Isolate(t)
	for _, path := range []string{"../go-stdlib-packages.json", "../python-stdlib-modules.json"} {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var doc any
		if path == "../go-stdlib-packages.json" {
			var g goTable
			if err := json.Unmarshal(data, &g); err != nil {
				t.Fatal(err)
			}
			doc = g
		} else {
			var p pythonTable
			if err := json.Unmarshal(data, &p); err != nil {
				t.Fatal(err)
			}
			doc = p
		}
		again, err := render(doc)
		if err != nil {
			t.Fatal(err)
		}
		if string(again) != string(data) {
			t.Fatalf("%s is not in the generator's rendering", path)
		}
	}
}
