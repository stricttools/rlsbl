package cli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

const syncedToken = "npm_AbCdEfGh0123456789abcdefghijklmnWXYZ"

func TestSyncNpmTokenIsConsequentialAndDeclaresItsGrant(t *testing.T) {
	hygiene.Isolate(t)
	cmd, ok := appWith(t, testsupport.NewFakeHTTP(t)).Groups()["secrets"].Commands["sync-npm-token"]
	if !ok || cmd.Effect != strictcli.EffectMutating || !cmd.Consequential {
		t.Fatalf("registered %v: %+v", ok, cmd)
	}
}

// npmrcInHome writes the token into ~/.npmrc of the test's throwaway home.
func npmrcInHome(t *testing.T) {
	t.Helper()
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	testsupport.WriteFile(t, filepath.Join(home, ".npmrc"), "//registry.npmjs.org/:_authToken="+syncedToken+"\n")
}

func TestSyncNpmTokenSetsTheRepositoryTheOriginNames(t *testing.T) {
	hygiene.Isolate(t)
	npmrcInHome(t)
	repo := testsupport.NewRepo(t)
	hygiene.Chdir(t, repo.Dir)
	gh := testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/owner/repo/actions/secrets/NPM_TOKEN"}, Stdout: `{"name": "NPM_TOKEN", "updated_at": "2026-08-24T09:22:07Z"}`},
		testsupport.GHAnswer{Args: []string{"secret", "set", "NPM_TOKEN", "--repo", "owner/repo"}},
	)
	fake := testsupport.NewFakeHTTP(t, httpGet("https://registry.npmjs.org/-/whoami", 200, `{"username": "someone"}`))
	app := appWith(t, fake)

	r := app.Test([]string{"secrets", "sync-npm-token", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "has no origin remote") || !strings.Contains(r.Stderr, "--all") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	repo.Git("remote", "add", "origin", "git@github.com:owner/repo.git")
	r = app.Test([]string{"secrets", "sync-npm-token", "--approve-consequential"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "owner/repo: NPM_TOKEN set") {
		t.Fatalf("adding the origin did not clear the refusal: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	var set []testsupport.GHCall
	for _, c := range gh.Calls() {
		if c.Args[0] == "secret" {
			set = append(set, c)
		}
	}
	if len(set) != 1 || set[0].Stdin != syncedToken {
		t.Fatalf("the secret writes: %+v", set)
	}
	if strings.Contains(r.Stdout+r.Stderr, syncedToken) {
		t.Fatal("the token was printed")
	}
}
