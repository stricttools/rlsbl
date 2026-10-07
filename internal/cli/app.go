// Package cli is rlsbl's command line: the strictcli application, every
// command and group registration (one file per group), their flags and
// arguments, the payload types and human renderings of --json, and the
// wiring of the checks registry and the observe allowlist. No other package
// registers a command; handlers here call into the packages that do the
// work.
package cli

import (
	"errors"
	"net/http"
	"path/filepath"
	"runtime"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/checks"
	"github.com/stricttools/rlsbl/internal/previewapply"
)

// appHelp is the application's description in its help.
const appHelp = "Release orchestration and project scaffolding: bumps versions, validates a structured changelog, tags only the commit CI verified, publishes to npm, PyPI, and the Go module proxy, and scaffolds CI workflows"

// httpTimeout bounds every HTTP request the released binary sends, so a
// registry that never answers cannot hang a command.
const httpTimeout = 60 * time.Second

// Dependencies are what the application takes from outside. Every field is
// required.
type Dependencies struct {
	// Version is the release version the binary carries.
	Version string
	// HTTPClient sends every HTTP request the commands make: NewHTTPClient
	// in the binary, a fake transport's client in tests.
	HTTPClient *http.Client
}

// NewHTTPClient is the HTTP client of the released binary.
func NewHTTPClient() *http.Client {
	return &http.Client{Timeout: httpTimeout}
}

// New builds the application with every command registered.
func New(d Dependencies) (*strictcli.App, error) {
	if d.Version == "" {
		return nil, errors.New("this rlsbl binary carries no version: the VERSION file at the root of the repository it was built from is empty; write the version into it and build again")
	}
	if d.HTTPClient == nil {
		return nil, errors.New("no HTTP client was given to the application")
	}
	opts := []strictcli.AppOption{
		strictcli.WithChecksEmbed(checks.Registry),
		strictcli.WithProcObserveAllowlist(previewapply.Prefixes()),
		strictcli.WithHTTPClient(d.HTTPClient),
		strictcli.WithHandshakeEnv(pushStdinEnv, "Pre-push ref lines (`<local ref> <local sha> <remote ref> <remote sha>`) for the checks the pre-push hook selects, exported by the hook from git's stdin."),
	}
	if root, ok := sourceTreeRoot(); ok {
		opts = append(opts, strictcli.WithSourceTreeRoot(root))
	}
	app := strictcli.NewApp("rlsbl", d.Version, appHelp, opts...)
	r := newRegistry(app)
	registerNames(r)
	registerDiscover(r)
	registerRewrite(r)
	registerScaffold(r, d.Version)
	registerWatch(r)
	registerMonorepo(r, d.Version)
	registerStatus(r)
	registerUnreleased(r)
	registerTargets(r)
	registerCommit(r)
	registerDev(r)
	registerUpstream(r)
	registerSecrets(r)
	registerOptions(r)
	registerReleaseGroup(r)
	registerReleaseOps(r)
	registerHistoryRewrites(r, d.Version)
	registerMigrate(r, d.Version)
	registerChangelog(r)
	registerTransition(r)
	if err := registerChecks(app); err != nil {
		return nil, err
	}
	return app, nil
}

// sourceTreeRoot is the root of the checkout this file was compiled from,
// which strictcli keeps the CLI test-coverage state under. A binary built
// with -trimpath (every released one) records a module-relative path here,
// and so has no source tree: strictcli's coverage is then off, as it is for
// a binary whose checkout is not on this machine.
func sourceTreeRoot() (string, bool) {
	_, file, _, ok := runtime.Caller(0)
	if !ok || !filepath.IsAbs(file) {
		return "", false
	}
	return filepath.Dir(filepath.Dir(filepath.Dir(file))), true
}
