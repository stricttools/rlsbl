package gomodule

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// listTimeout bounds `go list`, which reads only the local tree.
const listTimeout = 60 * time.Second

// Runner starts programs: the strictcli effects handle.
type Runner interface {
	Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error)
}

// Package is one Go package `go list` reports.
type Package struct {
	Name       string
	ImportPath string
	// Dir is relative to the module root: "." or "./cmd/x".
	Dir string
}

// The go toolchain is the one source of a project's packages and of which of
// them are main packages: hand-rolled globbing for main.go misreads entry
// files with other names, _test.go files in package main directories, and a
// root package main file without func main beside the main package under cmd/.
// Nothing here falls back to scanning files.

// ListPackages enumerates every package of the module rooted at dir (an
// absolute directory holding a go.mod), with `go list -e`, which reports
// packages whose imports do not resolve without needing the network.
// Error pseudo-packages (no name) are left out. A missing go toolchain, a
// directory without a go.mod, and a failing `go list` are errors; none of
// them is an empty list.
func ListPackages(r Runner, dir string) ([]Package, error) {
	if _, err := exec.LookPath("go"); err != nil {
		return nil, fmt.Errorf("'go' is not on PATH: the Go toolchain is required to list the packages of the Go project at %s", dir)
	}
	if _, err := os.Stat(filepath.Join(dir, FileName)); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("%s holds no go.mod, so it has no packages to list", dir)
		}
		return nil, err
	}
	res, err := r.Run([]interface{}{"go", "list", "-e", "-f", "{{.Name}}\t{{.ImportPath}}\t{{.Dir}}", "./..."},
		strictcli.Cwd(dir), strictcli.Check(false), strictcli.Timeout(listTimeout))
	if err != nil {
		return nil, err
	}
	if res.ExitCode() != 0 {
		return nil, fmt.Errorf("`go list ./...` failed in %s (exit %d):\n%s", dir, res.ExitCode(), strings.TrimSpace(res.Stderr()))
	}
	root, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return nil, err
	}
	var packages []Package
	for _, line := range strings.Split(res.Stdout(), "\n") {
		fields := strings.Split(line, "\t")
		if len(fields) != 3 || fields[0] == "" {
			continue
		}
		pkgDir, err := filepath.EvalSymlinks(fields[2])
		if err != nil {
			return nil, fmt.Errorf("`go list` in %s named the package directory %s: %w", dir, fields[2], err)
		}
		rel, err := filepath.Rel(root, pkgDir)
		if err != nil {
			return nil, err
		}
		packages = append(packages, Package{Name: fields[0], ImportPath: fields[1], Dir: relDir(filepath.ToSlash(rel))})
	}
	return packages, nil
}

// relDir spells a module-relative directory the way `go install` takes it.
func relDir(rel string) string {
	if rel == "." {
		return "."
	}
	return "./" + rel
}

// MainPackages are the package main packages (binaries) of the module
// rooted at dir.
func MainPackages(r Runner, dir string) ([]Package, error) {
	all, err := ListPackages(r, dir)
	if err != nil {
		return nil, err
	}
	var mains []Package
	for _, p := range all {
		if p.Name == "main" {
			mains = append(mains, p)
		}
	}
	return mains, nil
}

// DescribeMainPackages names the main packages `go list` found, for errors.
func DescribeMainPackages(mains []Package) string {
	if len(mains) == 0 {
		return "go list found no main packages in this project."
	}
	quoted := make([]string, len(mains))
	for i, p := range mains {
		quoted[i] = fmt.Sprintf("%q", p.Dir)
	}
	return "go list found main packages at: " + strings.Join(quoted, ", ")
}

// NormalizeInstallPath spells a declared install path the way the main
// package list spells it ("." or "./cmd/x"), and refuses one that leaves the
// module.
func NormalizeInstallPath(p string) (string, error) {
	clean := path.Clean(filepath.ToSlash(p))
	if clean == ".." || strings.HasPrefix(clean, "../") || strings.HasPrefix(clean, "/") {
		return "", fmt.Errorf("the install path %q lies outside the module: name a main package directory inside it, relative to the module root", p)
	}
	return relDir(clean), nil
}

// ValidateInstallPaths checks that every declared install path is a main
// package of the module rooted at dir, and returns them normalized.
func ValidateInstallPaths(r Runner, dir string, paths []string) ([]string, error) {
	mains, err := MainPackages(r, dir)
	if err != nil {
		return nil, err
	}
	have := map[string]bool{}
	for _, p := range mains {
		have[p.Dir] = true
	}
	out := make([]string, 0, len(paths))
	for _, p := range paths {
		norm, err := NormalizeInstallPath(p)
		if err != nil {
			return nil, err
		}
		if !have[norm] {
			return nil, fmt.Errorf("the declared install path '%s' is not a main package. %s", p, DescribeMainPackages(mains))
		}
		out = append(out, norm)
	}
	return out, nil
}

// ResolveMainPackageDir is the one main package of the module rooted at dir,
// for what needs a single binary (goreleaser's main, where version.go goes).
// With install paths declared they must name one main package and no
// more; without, the module must have one. Anything else is an error
// naming what `go list` found.
func ResolveMainPackageDir(r Runner, dir string, declared []string) (string, error) {
	if declared != nil {
		paths, err := ValidateInstallPaths(r, dir, declared)
		if err != nil {
			return "", err
		}
		if len(paths) != 1 {
			return "", fmt.Errorf("the go pipeline's install_paths name %d main packages (%s), but goreleaser's main and the placement of version.go need one and no more", len(paths), strings.Join(declared, ", "))
		}
		return paths[0], nil
	}
	mains, err := MainPackages(r, dir)
	if err != nil {
		return "", err
	}
	switch len(mains) {
	case 1:
		return mains[0].Dir, nil
	case 0:
		return "", fmt.Errorf("no main packages in %s. %s", dir, DescribeMainPackages(mains))
	}
	return "", fmt.Errorf("several main packages in %s and no install_paths declared on its go pipeline in .strictmetadata/releasables/releasables.toml. %s Declare install_paths (e.g. [\"./cmd/x\"]) to name the one", dir, DescribeMainPackages(mains))
}
