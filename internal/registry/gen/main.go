// Command gen regenerates the standard-library tables package registry
// embeds: go-stdlib-packages.json, the union of what `go list std` lists
// over every platform `go tool dist list` names (cgo enabled, so
// runtime/cgo is included), evaluated in process with go/build,
// without import paths that have an internal or vendor element, since
// nothing outside the standard library can import those; and
// python-stdlib-modules.json, from sys.stdlib_module_names of the python3
// on PATH. Each table records the toolchain that produced it.
//
// Run it from the repository root:
//
//	go run ./internal/registry/gen          # write both tables
//	go run ./internal/registry/gen -check   # compare only; exit 1 when stale
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"go/build"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// The tables, relative to the repository root.
const (
	goTablePath     = "internal/registry/go-stdlib-packages.json"
	pythonTablePath = "internal/registry/python-stdlib-modules.json"
	generatorName   = "internal/registry/gen"
	goSource        = "union of `go list std` over every GOOS/GOARCH in `go tool dist list`, CGO_ENABLED=1; import paths with an internal or vendor element excluded"
	pythonSource    = "sys.stdlib_module_names of the python3 on PATH"
)

type goTable struct {
	Generator string   `json:"generator"`
	Source    string   `json:"source"`
	GoVersion string   `json:"go_version"`
	Packages  []string `json:"packages"`
}

type pythonTable struct {
	Generator     string   `json:"generator"`
	Source        string   `json:"source"`
	PythonVersion string   `json:"python_version"`
	Modules       []string `json:"modules"`
}

func run(env []string, name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	if env != nil {
		cmd.Env = append(os.Environ(), env...)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, strings.TrimSpace(stderr.String()))
	}
	return string(out), nil
}

var goVersionWord = regexp.MustCompile(`^go[0-9]+\.[0-9]+(\.[0-9]+)?`)

// localGoVersion is the local toolchain's version without its experiment
// suffix (go1.26.6).
func localGoVersion() (string, error) {
	raw, err := run(nil, "go", "env", "GOVERSION")
	if err != nil {
		return "", err
	}
	v := goVersionWord.FindString(strings.TrimSpace(raw))
	if v == "" {
		return "", fmt.Errorf("`go env GOVERSION` printed %q, which names no Go release", raw)
	}
	return v, nil
}

func importable(path string) bool {
	for _, part := range strings.Split(path, "/") {
		if part == "internal" || part == "vendor" {
			return false
		}
	}
	return true
}

// listStd is every importable standard-library import path, across all
// platforms, sorted: the union of what `go list std` lists under each
// GOOS/GOARCH `go tool dist list` names, with cgo enabled. It is evaluated
// in process with go/build, the build-constraint logic `go list` applies,
// over the toolchain's GOROOT, rather than by one `go list std` per
// platform, which took seconds on every suite run.
func listStd() ([]string, error) {
	platforms, err := run(nil, "go", "tool", "dist", "list")
	if err != nil {
		return nil, err
	}
	gorootOut, err := run(nil, "go", "env", "GOROOT")
	if err != nil {
		return nil, err
	}
	goroot := strings.TrimSpace(gorootOut)
	src := filepath.Join(goroot, "src")
	dirs, err := stdDirectories(src)
	if err != nil {
		return nil, err
	}
	set := map[string]bool{}
	for _, platform := range strings.Fields(platforms) {
		goos, goarch, ok := strings.Cut(platform, "/")
		if !ok {
			return nil, fmt.Errorf("`go tool dist list` printed %q, which is not GOOS/GOARCH", platform)
		}
		ctx := build.Default
		ctx.GOROOT, ctx.GOOS, ctx.GOARCH, ctx.CgoEnabled = goroot, goos, goarch, true
		for _, d := range dirs {
			if set[d.importPath] {
				continue
			}
			if _, err := ctx.ImportDir(d.dir, 0); err != nil {
				var noGo *build.NoGoError
				if errors.As(err, &noGo) {
					continue
				}
				// A package whose files go/build reads with another problem
				// (files of two package names, for one) is still a package
				// `go list std` lists.
			}
			set[d.importPath] = true
		}
	}
	out := make([]string, 0, len(set))
	for p := range set {
		if importable(p) {
			out = append(out, p)
		}
	}
	sort.Strings(out)
	return out, nil
}

// stdDirectory is one directory under GOROOT/src that may hold a package.
type stdDirectory struct {
	dir        string
	importPath string
}

// stdDirectories are the directories `go list std` considers: every
// directory under src but cmd/, testdata/, and names starting with . or _,
// without builtin, which documents the predeclared identifiers and is no
// package.
func stdDirectories(src string) ([]stdDirectory, error) {
	var out []stdDirectory
	err := filepath.WalkDir(src, func(p string, d fs.DirEntry, err error) error {
		if err != nil || !d.IsDir() || p == src {
			return err
		}
		rel, err := filepath.Rel(src, p)
		if err != nil {
			return err
		}
		name := d.Name()
		if rel == "cmd" || name == "testdata" || strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_") {
			return filepath.SkipDir
		}
		if rel != "builtin" {
			out = append(out, stdDirectory{dir: p, importPath: filepath.ToSlash(rel)})
		}
		return nil
	})
	return out, err
}

// freshGoTable renders the Go table from the local toolchain.
func freshGoTable() ([]byte, string, error) {
	version, err := localGoVersion()
	if err != nil {
		return nil, "", err
	}
	packages, err := listStd()
	if err != nil {
		return nil, "", err
	}
	data, err := render(goTable{Generator: generatorName, Source: goSource, GoVersion: version, Packages: packages})
	return data, version, err
}

// freshPythonTable renders the Python table from the python3 on PATH.
func freshPythonTable() ([]byte, string, error) {
	out, err := run(nil, "python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2]); print('\\n'.join(sorted(sys.stdlib_module_names)))")
	if err != nil {
		return nil, "", err
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) < 2 {
		return nil, "", fmt.Errorf("python3 printed no module names")
	}
	modules := lines[1:]
	sort.Strings(modules)
	data, err := render(pythonTable{Generator: generatorName, Source: pythonSource, PythonVersion: lines[0], Modules: modules})
	return data, lines[0], err
}

func render(v any) ([]byte, error) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return b.Bytes(), nil
}

func main() {
	check := flag.Bool("check", false, "compare the committed tables with fresh ones and exit 1 when either is stale, writing nothing")
	flag.Parse()
	stale := false
	for _, table := range []struct {
		path  string
		fresh func() ([]byte, string, error)
	}{{goTablePath, freshGoTable}, {pythonTablePath, freshPythonTable}} {
		fresh, _, err := table.fresh()
		if err != nil {
			fmt.Fprintln(os.Stderr, "gen:", err)
			os.Exit(2)
		}
		current, err := os.ReadFile(filepath.FromSlash(table.path))
		if err != nil && !os.IsNotExist(err) {
			fmt.Fprintln(os.Stderr, "gen:", err)
			os.Exit(2)
		}
		switch {
		case bytes.Equal(current, fresh):
			fmt.Printf("%s is up to date\n", table.path)
		case *check:
			fmt.Fprintf(os.Stderr, "%s is stale: run `go run ./internal/registry/gen` from the repository root and commit the result\n", table.path)
			stale = true
		default:
			if err := os.WriteFile(filepath.FromSlash(table.path), fresh, 0o644); err != nil {
				fmt.Fprintln(os.Stderr, "gen:", err)
				os.Exit(2)
			}
			fmt.Printf("%s rewritten\n", table.path)
		}
	}
	if stale {
		os.Exit(1)
	}
}
