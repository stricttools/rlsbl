// Command gen regenerates the file package pipelines commits: the pipeline
// types table (pipeline-types.json), which the docs directive
// table-pipelines renders.
//
// Run it from the repository root:
//
//	go run ./internal/pipelines/gen          # write it
//	go run ./internal/pipelines/gen -check   # compare only; exit 1 when it is stale
package main

import (
	"bytes"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/stricttools/rlsbl/internal/pipelines"
)

func main() {
	check := flag.Bool("check", false, "compare the committed file with a fresh rendering and exit 1 when it is stale, writing nothing")
	flag.Parse()
	fresh, err := pipelines.RenderTypeTable()
	if err != nil {
		fmt.Fprintln(os.Stderr, "gen:", err)
		os.Exit(2)
	}
	path := filepath.FromSlash(pipelines.TypeTablePath)
	current, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		fmt.Fprintln(os.Stderr, "gen:", err)
		os.Exit(2)
	}
	switch {
	case bytes.Equal(current, fresh):
		fmt.Printf("%s is up to date\n", pipelines.TypeTablePath)
	case *check:
		fmt.Fprintf(os.Stderr, "%s is stale: run `%s` from the repository root and commit the result\n", pipelines.TypeTablePath, pipelines.RegenerateCommand)
		os.Exit(1)
	default:
		if err := os.WriteFile(path, fresh, 0o644); err != nil {
			fmt.Fprintln(os.Stderr, "gen:", err)
			os.Exit(2)
		}
		fmt.Printf("%s rewritten\n", pipelines.TypeTablePath)
	}
}
