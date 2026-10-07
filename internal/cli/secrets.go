package cli

import (
	"errors"
	"fmt"
	"os"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/secrets"
)

const secretsGroupHelp = "Keep the Actions secrets CI publishes with in step with the credentials on this machine. No secret value is ever " +
	"printed or passed as an argument."

const syncNpmTokenHelp = "Copy the npm token in ~/.npmrc (the //registry.npmjs.org/:_authToken= line) into the NPM_TOKEN Actions secret of " +
	"repositories that already have one. Refuses when ~/.npmrc holds no token, and when npm does not accept it (asked with GET " +
	"https://registry.npmjs.org/-/whoami); otherwise prints the npm user it authenticates. Without --all the target is the repository the " +
	"working directory's origin remote names, and a repository without an NPM_TOKEN secret is refused: the secret is never created. With --all " +
	"the targets are every repository the authenticated gh account can see (its own and those of every organization it belongs to) that has an " +
	"NPM_TOKEN secret; archived repositories are named and skipped, since GitHub refuses writes to them. Every target's secret is read before " +
	"any is written. Each target is set with `gh secret set NPM_TOKEN --repo <owner/repo>`, the token piped on stdin, and printed with its " +
	"outcome. Exits 1 when any target could not be read or set, once every other target was tried. The token is never printed."

// setSecretGrant is the grant sync-npm-token writes secrets under.
const setSecretGrant = "set-secret"

func registerSecrets(r *commandSet) {
	r.group([]string{"secrets"}, secretsGroupHelp)
	r.add(command{
		path:   []string{"secrets", "sync-npm-token"},
		help:   syncNpmTokenHelp,
		effect: mutating,
		// Only a person may decide to replace the credential CI publishes
		// with, across every repository the account can see with --all.
		consequential: true,
		grants: []strictcli.Grant{strictcli.NewGrant(setSecretGrant,
			"replaces the NPM_TOKEN Actions secret a repository's CI publishes to npm with",
			strictcli.ProcMutate)},
		flags: []strictcli.Flag{
			strictcli.BoolFlag("all", "Target every repository the authenticated gh account can see that has an NPM_TOKEN secret, instead of the "+
				"repository the origin remote names (the current repository when not passed)", strictcli.Optional()),
		},
		run: runSyncNpmToken,
	})
}

// originRepository is the GitHub repository the origin remote of the
// repository holding the working directory names.
func originRepository(ctx *strictcli.Context) (github.Repository, error) {
	_, root, err := workingRepository()
	if err != nil {
		return github.Repository{}, fmt.Errorf("%w. Run the command inside a checkout of the repository to set, or pass --all", err)
	}
	repo, err := git.Open(ctx.Effects(), root)
	if err != nil {
		return github.Repository{}, err
	}
	configured, err := repo.RemoteConfigured("origin")
	if err != nil {
		return github.Repository{}, err
	}
	if !configured {
		return github.Repository{}, errors.New("this checkout has no origin remote, so there is no current repository to set the secret on. Run the command inside a checkout whose origin is the repository on GitHub, or pass --all")
	}
	url, err := repo.RemoteURL("origin")
	if err != nil {
		return github.Repository{}, err
	}
	return github.RepositoryFromRemoteURL(url)
}

func runSyncNpmToken(ctx *strictcli.Context, kw map[string]any) (any, error) {
	all, _ := strictcli.GetOpt[bool](kw, "all")
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, fmt.Errorf("the home directory, where ~/.npmrc lives, cannot be found: %w", err)
	}
	req := secrets.SyncRequest{All: all, Npmrc: registry.NpmrcPath(home), Grant: setSecretGrant}
	if !all {
		if req.Current, err = originRepository(ctx); err != nil {
			return nil, err
		}
	}
	gh, err := github.New(ctx.Effects())
	if err != nil {
		return nil, err
	}
	npm, err := registry.New(registry.Reads(ctx.Effects()))
	if err != nil {
		return nil, err
	}
	return nil, secrets.SyncNpmToken(ctx, gh, npm, req)
}
