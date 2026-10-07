// Package checks holds rlsbl's checks registry, embedded from checks.toml so
// the binary carries it wherever it is installed.
package checks

import _ "embed"

// Registry is checks.toml, the checks registry strictcli's check framework
// reads at app construction.
//
//go:embed checks.toml
var Registry []byte
