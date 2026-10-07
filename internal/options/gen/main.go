// Command gen regenerates rlsbl's options registry,
// internal/options/registry.toml, from the checks registry,
// internal/checks/checks.toml, through options.RenderRegistry. A rendering
// strictspec refuses is never written.
//
// Run it from the repository root:
//
//	go run ./internal/options/gen          # write the registry
//	go run ./internal/options/gen -check   # compare only; exit 1 when stale
package main

import (
	"bytes"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"

	"github.com/stricttools/rlsbl/internal/options"
)

func main() {
	check := flag.Bool("check", false, "compare the committed registry with a fresh rendering; write nothing")
	flag.Parse()
	if err := run(*check); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(check bool) error {
	checks, err := os.ReadFile(options.ChecksFile)
	if err != nil {
		return fmt.Errorf("reading %s (run this from the repository root): %w", options.ChecksFile, err)
	}
	rendered, err := options.RenderRegistry(checks)
	if err != nil {
		return fmt.Errorf("%w\nnothing was written", err)
	}
	current, err := os.ReadFile(options.RegistryFile)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("reading %s: %w", options.RegistryFile, err)
	}
	fresh := err == nil && bytes.Equal(current, rendered)
	if check {
		if !fresh {
			return fmt.Errorf("%s is stale: run `go run ./internal/options/gen` from the repository root and commit the result", options.RegistryFile)
		}
		fmt.Printf("%s is fresh.\n", options.RegistryFile)
		return nil
	}
	if fresh {
		fmt.Printf("%s is already fresh.\n", options.RegistryFile)
		return nil
	}
	if err := os.WriteFile(options.RegistryFile, rendered, 0o644); err != nil {
		return fmt.Errorf("writing %s: %w", options.RegistryFile, err)
	}
	fmt.Printf("Wrote %s.\n", options.RegistryFile)
	return nil
}
