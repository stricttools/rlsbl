// Command gen regenerates the files package targets commits: the support
// matrix (support-matrix.json, every target's facts and the tables the docs
// render), and the standalone program the pypi CI workflow runs over a
// built upload (privatepathscheck/main.go, from privatepaths.go and
// privatepathscheck.go.in).
//
// Run it from the repository root:
//
//	go run ./internal/targets/gen          # write both
//	go run ./internal/targets/gen -check   # compare only; exit 1 when either is stale
package main

import (
	"bytes"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/stricttools/rlsbl/internal/targets"
)

func main() {
	check := flag.Bool("check", false, "compare the committed files with fresh renderings and exit 1 when either is stale, writing nothing")
	flag.Parse()
	stale := false
	for _, file := range []struct {
		path   string
		render func() ([]byte, error)
	}{
		{targets.MatrixPath, targets.RenderMatrix},
		{targets.PrivatePathsProgramPath, targets.RenderPrivatePathsProgram},
	} {
		fresh, err := file.render()
		if err != nil {
			fmt.Fprintln(os.Stderr, "gen:", err)
			os.Exit(2)
		}
		path := filepath.FromSlash(file.path)
		current, err := os.ReadFile(path)
		if err != nil && !os.IsNotExist(err) {
			fmt.Fprintln(os.Stderr, "gen:", err)
			os.Exit(2)
		}
		switch {
		case bytes.Equal(current, fresh):
			fmt.Printf("%s is up to date\n", file.path)
		case *check:
			fmt.Fprintf(os.Stderr, "%s is stale: run `%s` from the repository root and commit the result\n", file.path, targets.RegenerateCommand)
			stale = true
		default:
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				fmt.Fprintln(os.Stderr, "gen:", err)
				os.Exit(2)
			}
			if err := os.WriteFile(path, fresh, 0o644); err != nil {
				fmt.Fprintln(os.Stderr, "gen:", err)
				os.Exit(2)
			}
			fmt.Printf("%s rewritten\n", file.path)
		}
	}
	if stale {
		os.Exit(1)
	}
}
