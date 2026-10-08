package registry

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// npmrcWith writes an npm config file and returns its path.
func npmrcWith(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), ".npmrc")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// fakeNpm puts first on PATH an npm that prints stdout and exits with code.
func fakeNpm(t *testing.T, stdout string, code int) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "npm-out.json"), []byte(stdout), 0o644); err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\ncat \"${0%/*}/npm-out.json\"\nexit " + strconv.Itoa(code) + "\n"
	if err := os.WriteFile(filepath.Join(dir, "npm"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func TestReadNpmrcToken(t *testing.T) {
	hygiene.Isolate(t)
	if tok, err := ReadNpmrcToken(npmrcWith(t, "registry=https://registry.npmjs.org/\n//registry.npmjs.org/:_authToken=npm_abcdef\n")); err != nil || tok != "npm_abcdef" {
		t.Errorf("%q %v", tok, err)
	}
	if _, err := ReadNpmrcToken(filepath.Join(t.TempDir(), ".npmrc")); err == nil || !strings.Contains(err.Error(), "npm login") {
		t.Errorf("a missing file: %v", err)
	}
	if _, err := ReadNpmrcToken(npmrcWith(t, "color=false\n")); err == nil || !strings.Contains(err.Error(), "holds no") {
		t.Errorf("no token line: %v", err)
	}
	if _, err := ReadNpmrcToken(npmrcWith(t, "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n")); err == nil || !strings.Contains(err.Error(), "environment variable") {
		t.Errorf("a variable reference: %v", err)
	}
	if _, err := ReadNpmrcToken(npmrcWith(t, "//registry.npmjs.org/:_authToken=a\n//registry.npmjs.org/:_authToken=b\n")); err == nil || !strings.Contains(err.Error(), "2 ") {
		t.Errorf("two token lines: %v", err)
	}
}

func TestNpmWhoami(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t, get("https://registry.npmjs.org/-/whoami", 200, `{"username":"acme"}`))
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		if user, err := c.NpmWhoami("npm_secret"); err != nil || user != "acme" {
			t.Errorf("%q %v", user, err)
		}
		return nil
	})
	req := fake.Requests()[0]
	if req.Header.Get("Authorization") != "Bearer npm_secret" || strings.Contains(req.URL, "npm_secret") {
		t.Fatalf("the token travelled as %q in %s", req.Header.Get("Authorization"), req.URL)
	}
	for _, code := range []int{401, 403} {
		fake := testsupport.NewFakeHTTP(t, get("https://registry.npmjs.org/-/whoami", code, ""))
		withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
			_, err := c.NpmWhoami("npm_secret")
			if err == nil || !strings.Contains(err.Error(), "npm login") || !strings.Contains(err.Error(), SyncCommand) || strings.Contains(err.Error(), "npm_secret") {
				t.Errorf("%d: %v", code, err)
			}
			return nil
		})
	}
	fake = testsupport.NewFakeHTTP(t, get("https://registry.npmjs.org/-/whoami", 502, ""))
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		if _, err := c.NpmWhoami("npm_secret"); err == nil || !strings.Contains(err.Error(), "could not ask") {
			t.Errorf("an unreachable registry: %v", err)
		}
		return nil
	})
}

// withRunner runs fn with the effects handle of a mutating throwaway
// command, which may start programs the observe allowlist does not admit.
func withRunner(t *testing.T, fake *testsupport.FakeHTTP, fn func(e *strictcli.Effects) error) {
	t.Helper()
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, Allowlist: previewapply.Prefixes(), HTTPClient: fake.Client()}, fn)
}

func TestNpmTokenCreatedAt(t *testing.T) {
	hygiene.Isolate(t)
	token := "npm_abcdefghijklmnopqrstuvwxyz"
	cases := []struct {
		name, listing, want string
		code                int
	}{
		{"the matching unrevoked token", `[{"token":"npm_ab...wxyz","created":"2026-03-01T10:00:00.000Z"},{"token":"npm_zz...zzzz","created":"2020-01-01T00:00:00Z"}]`, "2026-03-01T10:00:00Z", 0},
		{"a wrapped listing", `{"objects":[{"token":"npm_ab...wxyz","created":"2026-03-01T10:00:00Z"}]}`, "2026-03-01T10:00:00Z", 0},
		{"no match", `[{"token":"npm_zz...zzzz","created":"2020-01-01T00:00:00Z"}]`, "0 unrevoked", 0},
		{"a revoked match", `[{"token":"npm_ab...wxyz","revoked":true,"created":"2020-01-01T00:00:00Z"}]`, "0 unrevoked", 0},
		// npm 10 lists revoked as the revocation time, and null for a live token.
		{"a match revoked at a time", `[{"token":"npm_ab...wxyz","revoked":"2026-02-01T00:00:00.000Z","created":"2020-01-01T00:00:00Z"}]`, "0 unrevoked", 0},
		{"a live match beside one revoked at a time", `[{"token":"npm_zz...zzzz","revoked":"2026-02-01T00:00:00.000Z","created":"2020-01-01T00:00:00Z"},{"token":"npm_ab...wxyz","revoked":null,"created":"2026-03-01T10:00:00Z"}]`, "2026-03-01T10:00:00Z", 0},
		{"a failing listing", `{}`, "exited 1", 1},
		{"an unparsable time", `[{"token":"npm_ab...wxyz","created":"yesterday"}]`, "no creation time", 0},
	}
	for _, c := range cases {
		fakeNpm(t, c.listing, c.code)
		withRunner(t, testsupport.NewFakeHTTP(t), func(e *strictcli.Effects) error {
			created, err := NpmTokenCreatedAt(e, token)
			got := ""
			if err != nil {
				got = err.Error()
			} else {
				got = created.UTC().Format(time.RFC3339)
			}
			if !strings.Contains(got, c.want) {
				t.Errorf("%s: %q, want %q", c.name, got, c.want)
			}
			return nil
		})
	}
}

// The npm-token-synced check runs inside read-only commands (rlsbl check,
// rlsbl failing-checks, a release's --dry-run), so the listing it reads must be
// an observe the allowlist admits.
func TestNpmTokenCreatedAtRunsInAReadOnlyCommand(t *testing.T) {
	hygiene.Isolate(t)
	fakeNpm(t, `[{"token":"npm_ab...wxyz","created":"2026-03-01T10:00:00Z"}]`, 0)
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes(), HTTPClient: testsupport.NewFakeHTTP(t).Client()}, func(e *strictcli.Effects) error {
		if _, err := NpmTokenCreatedAt(e, "npm_abcdefghijklmnopqrstuvwxyz"); err != nil {
			t.Errorf("a read-only command could not list the npm tokens: %v", err)
		}
		return nil
	})
}

func TestCompareNpmTokenSync(t *testing.T) {
	hygiene.Isolate(t)
	repo := github.Repository{Owner: "acme", Name: "portal"}
	created := time.Date(2026, 3, 1, 10, 0, 0, 0, time.UTC)
	if v := CompareNpmTokenSync(repo, "acme", created, github.Secret{Present: true, UpdatedAt: "2026-03-02T00:00:00Z"}); !v.OK() || len(v.Notes) != 1 {
		t.Errorf("set after: %+v", v)
	}
	v := CompareNpmTokenSync(repo, "acme", created, github.Secret{Present: true, UpdatedAt: "2026-02-01T00:00:00Z"})
	if v.OK() || !strings.Contains(v.Problems[0], SyncCommand) || !strings.Contains(v.Problems[0], "before") {
		t.Errorf("set before: %+v", v)
	}
	if v := CompareNpmTokenSync(repo, "acme", created, github.Secret{Present: true}); v.OK() || !strings.Contains(v.Problems[0], "no update time") {
		t.Errorf("no update time: %+v", v)
	}
	if v := CompareNpmTokenSync(repo, "acme", created, github.Secret{}); v.OK() || !strings.Contains(v.Problems[0], "has no NPM_TOKEN secret") {
		t.Errorf("absent: %+v", v)
	}
}

// The whole comparison: a secret set before the local token was created
// fails, naming the sync command; setting it after (what the sync does)
// clears it; an unreadable secret is never a pass.
func TestEvaluateNpmTokenSync(t *testing.T) {
	hygiene.Isolate(t)
	repo := github.Repository{Owner: "acme", Name: "portal"}
	npmrc := npmrcWith(t, "//registry.npmjs.org/:_authToken=npm_abcdefghijklmnopqrstuvwxyz\n")
	fakeNpm(t, `[{"token":"npm_ab...wxyz","created":"2026-03-01T10:00:00Z"}]`, 0)
	secret := []string{"api", "--method", "GET", "repos/acme/portal/actions/secrets/NPM_TOKEN"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: secret, Stdout: `{"updated_at":"2026-02-01T00:00:00Z"}`},
		testsupport.GHAnswer{Args: secret, Stdout: `{"updated_at":"2026-03-05T00:00:00Z"}`},
		testsupport.GHAnswer{Args: secret, Stderr: "HTTP 403\n", Exit: 1},
	)
	fake := testsupport.NewFakeHTTP(t, get("https://registry.npmjs.org/-/whoami", 200, `{"username":"acme"}`))
	withRunner(t, fake, func(e *strictcli.Effects) error {
		c, err := New(Reads(e))
		if err != nil {
			return err
		}
		gh, err := github.New(e)
		if err != nil {
			return err
		}
		if v := c.EvaluateNpmTokenSync(e, gh, npmrc, repo); v.OK() || !strings.Contains(v.Problems[0], SyncCommand) {
			t.Errorf("stale: %+v", v)
		}
		if v := c.EvaluateNpmTokenSync(e, gh, npmrc, repo); !v.OK() {
			t.Errorf("synced: %+v", v)
		}
		if v := c.EvaluateNpmTokenSync(e, gh, npmrc, repo); v.OK() || !strings.Contains(v.Problems[0], "could not read") {
			t.Errorf("unreadable: %+v", v)
		}
		return nil
	})
	missing := filepath.Join(t.TempDir(), ".npmrc")
	withRunner(t, testsupport.NewFakeHTTP(t), func(e *strictcli.Effects) error {
		c, _ := New(Reads(e))
		gh, _ := github.New(e)
		if v := c.EvaluateNpmTokenSync(e, gh, missing, repo); v.OK() || !strings.Contains(v.Problems[0], "Until then CI cannot publish to npm") {
			t.Errorf("no token: %+v", v)
		}
		return nil
	})
}
