package cli

import (
	"fmt"
	"go/parser"
	"go/token"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// layers is the import order of rlsbl's packages under internal/, lowest
// first. A package imports only packages of lower layers, and a package of
// its own layer only where sameLayerImports names the pair.
var layers = [][]string{
	{"semver", "git", "github", "saferm", "previewapply"},
	{"declarations"},
	{"workspace", "options", "upstream"},
	{"releaserecord", "runstate", "gomodule", "dependencies", "registry"},
	{"changelog", "targets"},
	{"pipelines"},
	{"publishrules", "releasenotes", "devtools", "secrets", "tagging", "workflows", "scaffold"},
	{"checks", "ci"},
	{"release"},
	{"releaseops", "historyrewrite", "batchrelease", "monorepo", "rewrite"},
	{"lifecycleops", "migration"},
	{"cli"},
}

// sameLayerImports names each import allowed between two packages of one
// layer, importer first, with the reason for it.
var sameLayerImports = map[[2]string]string{}

// testOnly is the package only _test.go files may import.
const testOnly = "testsupport"

// internalPackage is the top-level package under internal/ an import path
// names, or "" when it names none.
func internalPackage(path string) string {
	rest, ok := strings.CutPrefix(path, testsupport.ModulePath+"/internal/")
	if !ok {
		return ""
	}
	top, _, _ := strings.Cut(rest, "/")
	return top
}

// layeringViolations checks the imports of every non-test Go file listed.
func layeringViolations(t *testing.T, root string, files []string) []string {
	t.Helper()
	rank := map[string]int{}
	for i, layer := range layers {
		for _, name := range layer {
			rank[name] = i
		}
	}
	fset := token.NewFileSet()
	var problems []string
	for _, rel := range files {
		if strings.HasSuffix(rel, "_test.go") {
			continue
		}
		importer := testsupport.ImportPath(rel)
		from := internalPackage(importer)
		if from != "" && from != testOnly {
			if _, ok := rank[from]; !ok {
				problems = append(problems, fmt.Sprintf("%s: package internal/%s has no place in the layer order; add it to layers", rel, from))
				continue
			}
		}
		f, err := parser.ParseFile(fset, filepath.Join(root, filepath.FromSlash(rel)), nil, parser.ImportsOnly)
		if err != nil {
			t.Fatalf("parsing %s: %v", rel, err)
		}
		for _, spec := range f.Imports {
			path, err := strconv.Unquote(spec.Path.Value)
			if err != nil || (path != testsupport.ModulePath && !strings.HasPrefix(path, testsupport.ModulePath+"/")) {
				continue
			}
			to := internalPackage(path)
			switch {
			case to == testOnly:
				problems = append(problems, fmt.Sprintf("%s imports %s, which only _test.go files may import", rel, path))
			case from == testOnly:
				problems = append(problems, fmt.Sprintf("%s: the test harness imports %s; it imports nothing of rlsbl, so every package's tests can use it", rel, path))
			case from == "":
				// The binary and the root package: cmd/rlsbl takes the
				// application from internal/cli and the version from the
				// root package; the root package imports nothing.
				if !strings.HasPrefix(rel, "cmd/") || (path != testsupport.ModulePath && to != "cli") {
					problems = append(problems, fmt.Sprintf("%s imports %s; outside internal/, only cmd/rlsbl imports, and only internal/cli and the root package", rel, path))
				}
			case to == "":
				problems = append(problems, fmt.Sprintf("%s imports %s, which is not under internal/", rel, path))
			case to == from:
			case rank[to] > rank[from]:
				problems = append(problems, fmt.Sprintf("%s: internal/%s imports internal/%s, which is in a higher layer", rel, from, to))
			case rank[to] == rank[from]:
				if _, ok := sameLayerImports[[2]string{from, to}]; !ok {
					problems = append(problems, fmt.Sprintf("%s: internal/%s imports internal/%s of its own layer, which sameLayerImports does not name", rel, from, to))
				}
			}
		}
	}
	return problems
}

func TestPackagesImportOnlyDownTheLayerOrder(t *testing.T) {
	hygiene.Isolate(t)
	root := testsupport.ModuleRoot(t)
	if problems := layeringViolations(t, root, testsupport.GoFiles(t, root)); len(problems) > 0 {
		t.Fatalf("layering violations:\n  %s", strings.Join(problems, "\n  "))
	}
}

func TestTheLayerOrderNamesEachPackageOnce(t *testing.T) {
	hygiene.Isolate(t)
	seen := map[string]bool{}
	for _, layer := range layers {
		for _, name := range layer {
			if seen[name] {
				t.Errorf("%s is in two layers", name)
			}
			seen[name] = true
		}
	}
	for pair, reason := range sameLayerImports {
		if strings.TrimSpace(reason) == "" {
			t.Errorf("the same-layer import %s -> %s carries no reason", pair[0], pair[1])
		}
	}
}

func TestTheLayeringGuardRefusesAnUpwardImport(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	m := testsupport.ModulePath
	files := map[string]string{
		"internal/git/up.go":            "package git\n\nimport _ \"" + m + "/internal/cli\"\n",
		"internal/git/down_test.go":     "package git\n\nimport _ \"" + m + "/internal/cli\"\n",
		"internal/semver/side.go":       "package semver\n\nimport _ \"" + m + "/internal/git\"\n",
		"internal/cli/ok.go":            "package cli\n\nimport _ \"" + m + "/internal/git\"\n",
		"internal/cli/harness.go":       "package cli\n\nimport _ \"" + m + "/internal/testsupport\"\n",
		"internal/unplaced/x.go":        "package unplaced\n",
		"internal/testsupport/x.go":     "package testsupport\n\nimport _ \"" + m + "/internal/semver\"\n",
		"cmd/rlsbl/main.go":             "package main\n\nimport (\n\t_ \"" + m + "\"\n\t_ \"" + m + "/internal/cli\"\n)\n",
		"cmd/rlsbl/bad.go":              "package main\n\nimport _ \"" + m + "/internal/git\"\n",
		"internal/options/gen/main.go":  "package main\n\nimport _ \"" + m + "/internal/options\"\n",
		"internal/options/registry.go":  "package options\n\nimport _ \"" + m + "/internal/semver\"\n",
		"internal/workspace/residue.go": "package workspace\n\nimport _ \"" + m + "/internal/options\"\n",
	}
	var rels []string
	for rel, content := range files {
		testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), content)
		rels = append(rels, rel)
	}
	got := strings.Join(layeringViolations(t, root, rels), "\n")
	for _, want := range []string{
		"internal/git/up.go: internal/git imports internal/cli, which is in a higher layer",
		"internal/semver/side.go: internal/semver imports internal/git of its own layer",
		"internal/cli/harness.go imports " + m + "/internal/testsupport, which only _test.go files may import",
		"internal/unplaced/x.go: package internal/unplaced has no place in the layer order",
		"internal/testsupport/x.go: the test harness imports",
		"cmd/rlsbl/bad.go imports " + m + "/internal/git",
		"internal/workspace/residue.go: internal/workspace imports internal/options of its own layer",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing violation %q in:\n%s", want, got)
		}
	}
	for _, clean := range []string{"down_test.go", "internal/cli/ok.go", "cmd/rlsbl/main.go", "options/gen/main.go", "options/registry.go"} {
		if strings.Contains(got, clean) {
			t.Errorf("%s was reported:\n%s", clean, got)
		}
	}
}
