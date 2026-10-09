// Command rlsbl releases software: it bumps versions, validates a structured
// changelog, tags only the commit CI verified, and publishes to npm, PyPI, and
// the Go module proxy. The command tree is built by internal/cli.
package main

import (
	"fmt"
	"os"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl"
	"github.com/stricttools/rlsbl/internal/cli"
)

func main() {
	// The release's schema dump tells the entry point among the module's
	// main packages by the one that imports strictcli itself.
	var app *strictcli.App
	app, err := cli.New(cli.Dependencies{Version: rlsbl.Version, HTTPClient: cli.NewHTTPClient()})
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	app.Run()
}
