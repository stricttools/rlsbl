// Package secrets keeps the Actions secrets CI publishes with in step with
// the credentials on this machine: `rlsbl secrets sync-npm-token` copies the
// npm token in ~/.npmrc into the NPM_TOKEN secret of repositories that
// already carry one.
//
// No token is ever printed, logged, or put in an argument: npm is asked
// with an Authorization header, and `gh secret set` reads the token from
// its standard input.
package secrets

import (
	"fmt"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/registry"
)

// SetNpmToken writes token into repo's NPM_TOKEN Actions secret, piped on
// gh's standard input. grant names the grant the command declares for the
// write; empty runs it under none.
func SetNpmToken(gh github.Client, repo github.Repository, token, grant string) error {
	opts := []strictcli.EffectOption{strictcli.Redact(token)}
	if grant != "" {
		opts = append(opts, strictcli.UseGrant(grant))
	}
	return gh.SetSecret(repo, registry.NpmTokenSecret, []byte(token), opts...)
}

// SyncRequest is what one sync-npm-token run targets.
type SyncRequest struct {
	// All targets every repository the authenticated gh account can see
	// that has an NPM_TOKEN secret; otherwise Current is the one target.
	All     bool
	Current github.Repository
	// Npmrc is the npm config file holding the token (~/.npmrc).
	Npmrc string
	// Grant names the grant the command declares for the secret writes;
	// empty runs them under none.
	Grant string
}

// Outcome is one target's result, as printed.
type Outcome struct {
	Repository string
	// Result is "set", "would set", "skipped", or "failed".
	Result string
	Detail string
}

// SyncNpmToken copies the npm token the request's npmrc holds into the
// NPM_TOKEN secret of each target that already has one. The token is read
// and asked of npm (GET /-/whoami) first, and a token npm refuses stops the
// run before anything is set. Without All, a target without the secret is
// refused: the secret is never created. With All, archived repositories are
// named and skipped, since GitHub refuses writes to them, and repositories
// without the secret are counted and left alone. Every secret is read before
// any is written. A target that could not be read or set fails the run once
// every other target was tried.
func SyncNpmToken(ctx *strictcli.Context, gh github.Client, npm registry.Client, req SyncRequest) error {
	token, err := registry.ReadNpmrcToken(req.Npmrc)
	if err != nil {
		return err
	}
	user, err := npm.NpmWhoami(token)
	if err != nil {
		return err
	}
	ctx.Info(fmt.Sprintf("npm accepts the token in %s (npm user: %s).", req.Npmrc, user))

	candidates := []github.VisibleRepository{{Repository: req.Current}}
	if req.All {
		if candidates, err = gh.VisibleRepositories(); err != nil {
			return fmt.Errorf("the repositories this account can see cannot be listed: %w", err)
		}
	}
	var outcomes []Outcome
	var targets []github.Repository
	without := 0
	for _, c := range candidates {
		if c.Archived {
			outcomes = append(outcomes, Outcome{Repository: c.Repository.String(), Result: "skipped", Detail: "archived, so GitHub refuses writes to it"})
			continue
		}
		secret, err := gh.Secret(c.Repository, registry.NpmTokenSecret)
		switch {
		case err != nil:
			outcomes = append(outcomes, Outcome{Repository: c.Repository.String(), Result: "failed", Detail: "could not read its secrets (" + err.Error() + ")"})
		case secret.Present:
			targets = append(targets, c.Repository)
		case !req.All:
			return fmt.Errorf("%s has no %s secret, and this command never creates one: it only replaces a secret a publish workflow already uses", c.Repository, registry.NpmTokenSecret)
		default:
			without++
		}
	}
	for _, repo := range targets {
		if err := SetNpmToken(gh, repo, token, req.Grant); err != nil {
			outcomes = append(outcomes, Outcome{Repository: repo.String(), Result: "failed", Detail: err.Error()})
			continue
		}
		result := "set"
		if ctx.DryRun() {
			result = "would set"
		}
		outcomes = append(outcomes, Outcome{Repository: repo.String(), Result: result})
	}
	failed := 0
	for _, o := range outcomes {
		ctx.Info(o.Line())
		if o.Result == "failed" {
			failed++
		}
	}
	if req.All {
		ctx.Info(fmt.Sprintf("%d repositories carry %s; %d other visible repositories have none and were left alone.", len(targets), registry.NpmTokenSecret, without))
	}
	if failed > 0 {
		return fmt.Errorf("%d target(s) could not be read or set; the lines above name each one and why", failed)
	}
	return nil
}

// Line is the outcome as printed.
func (o Outcome) Line() string {
	switch o.Result {
	case "set":
		return fmt.Sprintf("%s: %s set", o.Repository, registry.NpmTokenSecret)
	case "would set":
		return fmt.Sprintf("%s: would set %s", o.Repository, registry.NpmTokenSecret)
	}
	return strings.TrimSuffix(fmt.Sprintf("%s: %s: %s", o.Repository, o.Result, o.Detail), ": ")
}
