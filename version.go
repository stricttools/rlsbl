// Package rlsbl carries rlsbl's release version. The binary's entry point is
// cmd/rlsbl; every engine package lives under internal/.
package rlsbl

import (
	_ "embed"
	"strings"
)

//go:embed VERSION
var versionFile string

// Version is the release version, read from the repository's VERSION file at
// build time with surrounding whitespace removed. It is the only place the
// version is read from a file: every package that needs it is handed it, so
// nothing under internal/ depends on the repository's own layout. A binary
// installed with go install carries the VERSION of the tagged commit it was
// built from.
var Version = strings.TrimSpace(versionFile)
