// Package checks is rlsbl's checks: the checks registry, embedded from
// checks.toml so the binary carries it wherever it is installed, and the
// implementation of each check, one file per family, registered on the
// application through strictcli's check framework by Register.
//
// Every check runs with a *Context, built by NewContext for the directory a
// run stands in. A check without a scope answers for one releasable, and
// refuses at a workspace root, which names none; a check's scope in
// checks.toml widens or narrows what it sees (see framework.go). The
// external checks a member declares in its release declarations are
// supplied by a check provider at materialization.
package checks

import _ "embed"

// Registry is checks.toml, the checks registry strictcli's check framework
// reads at app construction.
//
//go:embed checks.toml
var Registry []byte
