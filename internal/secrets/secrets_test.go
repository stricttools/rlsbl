package secrets_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/secrets"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// TestMain runs the tests through testsupport.RunTests, which builds the
// fake gh they use.
func TestMain(m *testing.M) {
	os.Exit(testsupport.RunTests(m))
}

const token = "npm_AbCdEfGh0123456789abcdefghijklmnWXYZ"

const whoamiURL = "https://registry.npmjs.org/-/whoami"

// npmrc writes an npm config file holding the token and returns its path.
func npmrc(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), ".npmrc")
	testsupport.WriteFile(t, path, content)
	return path
}

func tokenFile(t *testing.T) string {
	return npmrc(t, "registry=https://registry.npmjs.org/\n//registry.npmjs.org/:_authToken="+token+"\n")
}

func npmAccepts() testsupport.HTTPAnswer {
	return testsupport.HTTPAnswer{Method: "GET", URL: whoamiURL, Status: 200, Body: `{"username": "someone"}`}
}

func secretArgs(slug string) []string {
	return []string{"api", "--method", "GET", "repos/" + slug + "/actions/secrets/NPM_TOKEN"}
}

func present(slug string) testsupport.GHAnswer {
	return testsupport.GHAnswer{Args: secretArgs(slug), Stdout: `{"name": "NPM_TOKEN", "updated_at": "2026-08-24T09:22:07Z"}`}
}

func absent(slug string) testsupport.GHAnswer {
	return testsupport.GHAnswer{Args: secretArgs(slug), Stderr: "gh: Not Found (HTTP 404)", Exit: 1}
}

func setArgs(slug string) []string {
	return []string{"secret", "set", "NPM_TOKEN", "--repo", slug}
}

// sync runs SyncNpmToken as the handler of a mutating command.
func sync(t *testing.T, fake *testsupport.FakeHTTP, dryRun bool, req secrets.SyncRequest) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes(), HTTPClient: fake.Client()}, func(ctx *strictcli.Context) error {
		gh, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		npm, err := registry.New(registry.Reads(ctx.Effects()))
		if err != nil {
			return err
		}
		return secrets.SyncNpmToken(ctx, gh, npm, req)
	})
}

// sets are the secret writes the fake gh received, with what each read on
// its standard input.
func sets(gh *testsupport.GH) map[string]string {
	out := map[string]string{}
	for _, c := range gh.Calls() {
		if len(c.Args) == 5 && c.Args[0] == "secret" && c.Args[1] == "set" {
			out[c.Args[4]] = c.Stdin
		}
	}
	return out
}

func noTokenAnywhere(t *testing.T, gh *testsupport.GH, r strictcli.Result) {
	t.Helper()
	if strings.Contains(r.Stdout+r.Stderr, token) {
		t.Error("the token was printed")
	}
	for _, c := range gh.Calls() {
		if strings.Contains(strings.Join(c.Args, " "), token) {
			t.Errorf("the token was an argument: %v", c.Args)
		}
	}
}

func TestTheCurrentRepositoryIsSetFromStdin(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, present("owner/repo"), testsupport.GHAnswer{Args: setArgs("owner/repo")})
	repo, _ := github.ParseRepository("owner/repo")
	r := sync(t, testsupport.NewFakeHTTP(t, npmAccepts()), false, secrets.SyncRequest{Current: repo, Npmrc: tokenFile(t)})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if got := sets(gh); len(got) != 1 || got["owner/repo"] != token {
		t.Fatalf("the writes were %v", got)
	}
	if !strings.Contains(r.Stdout, "owner/repo: NPM_TOKEN set") || !strings.Contains(r.Stdout, "someone") {
		t.Errorf("stdout:\n%s", r.Stdout)
	}
	noTokenAnywhere(t, gh, r)
}

func TestTheTokenTravelsToNpmInAHeader(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, present("owner/repo"), testsupport.GHAnswer{Args: setArgs("owner/repo")})
	fake := testsupport.NewFakeHTTP(t, npmAccepts())
	repo, _ := github.ParseRepository("owner/repo")
	if r := sync(t, fake, false, secrets.SyncRequest{Current: repo, Npmrc: tokenFile(t)}); r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	requests := fake.Requests()
	if len(requests) != 1 || requests[0].Header.Get("Authorization") != "Bearer "+token || strings.Contains(requests[0].URL, token) {
		t.Fatalf("requests: %+v", requests)
	}
}

func TestARepositoryWithoutTheSecretIsRefusedNotCreated(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, absent("owner/repo"))
	repo, _ := github.ParseRepository("owner/repo")
	r := sync(t, testsupport.NewFakeHTTP(t, npmAccepts()), false, secrets.SyncRequest{Current: repo, Npmrc: tokenFile(t)})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "never creates") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if got := sets(gh); len(got) != 0 {
		t.Fatalf("a secret was written: %v", got)
	}
}

func TestATokenNpmRefusesIsRefusedBeforeAnythingIsSet(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, present("owner/repo"), testsupport.GHAnswer{Args: setArgs("owner/repo")})
	repo, _ := github.ParseRepository("owner/repo")
	fake := testsupport.NewFakeHTTP(t, testsupport.HTTPAnswer{Method: "GET", URL: whoamiURL, Status: 401, Body: `{"error": "no"}`})
	r := sync(t, fake, false, secrets.SyncRequest{Current: repo, Npmrc: tokenFile(t)})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "npm login") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	if len(gh.Calls()) != 0 {
		t.Fatalf("gh was asked before npm accepted the token: %v", gh.Calls())
	}
}

func TestAMissingTokenIsRefusedAndLoggingInClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, present("owner/repo"), testsupport.GHAnswer{Args: setArgs("owner/repo")})
	repo, _ := github.ParseRepository("owner/repo")
	path := npmrc(t, "registry=https://registry.npmjs.org/\n")
	r := sync(t, testsupport.NewFakeHTTP(t, npmAccepts()), false, secrets.SyncRequest{Current: repo, Npmrc: path})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, ".npmrc") || !strings.Contains(r.Stderr, "npm login") {
		t.Fatalf("exit %d:\n%s", r.ExitCode, r.Stderr)
	}
	// What `npm login` does: write the token line.
	testsupport.WriteFile(t, path, "//registry.npmjs.org/:_authToken="+token+"\n")
	if r := sync(t, testsupport.NewFakeHTTP(t, npmAccepts()), false, secrets.SyncRequest{Current: repo, Npmrc: path}); r.ExitCode != 0 {
		t.Fatalf("the fix did not clear the refusal: exit %d:\n%s", r.ExitCode, r.Stderr)
	}
}

func visibleAnswers() []testsupport.GHAnswer {
	return []testsupport.GHAnswer{
		{Args: []string{"api", "--method", "GET", "user", "--jq", ".login"}, Stdout: "me\n"},
		{Args: []string{"api", "--method", "GET", "--paginate", "user/orgs?per_page=100", "--jq", ".[].login"}, Stdout: "org\n"},
		{Args: []string{"api", "--method", "GET", "--paginate", "user/repos?affiliation=owner&per_page=100", "--jq", `.[] | "\(.full_name)\t\(.archived)"`}, Stdout: "me/plain\tfalse\nme/tool\tfalse\n"},
		{Args: []string{"api", "--method", "GET", "--paginate", "orgs/org/repos?per_page=100", "--jq", `.[] | "\(.full_name)\t\(.archived)"`}, Stdout: "org/lib\tfalse\norg/old\ttrue\n"},
	}
}

func TestAllSetsOnlyRepositoriesThatCarryTheSecret(t *testing.T) {
	hygiene.Isolate(t)
	answers := append(visibleAnswers(),
		absent("me/plain"), present("me/tool"), present("org/lib"),
		testsupport.GHAnswer{Args: setArgs("me/tool")}, testsupport.GHAnswer{Args: setArgs("org/lib")},
	)
	gh := testsupport.FakeGH(t, answers...)
	r := sync(t, testsupport.NewFakeHTTP(t, npmAccepts()), false, secrets.SyncRequest{All: true, Npmrc: tokenFile(t)})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	got := sets(gh)
	if len(got) != 2 || got["me/tool"] != token || got["org/lib"] != token {
		t.Fatalf("the writes were %v", got)
	}
	for _, want := range []string{"me/tool: NPM_TOKEN set", "org/lib: NPM_TOKEN set", "org/old: skipped: archived", "2 repositories carry NPM_TOKEN; 1 other visible repositories have none"} {
		if !strings.Contains(r.Stdout, want) {
			t.Errorf("stdout lacks %q:\n%s", want, r.Stdout)
		}
	}
	for _, c := range gh.Calls() {
		if strings.Contains(strings.Join(c.Args, " "), "org/old") {
			t.Errorf("the archived repository was asked about: %v", c.Args)
		}
	}
	noTokenAnywhere(t, gh, r)
}

func TestAnUnreadableSecretFailsTheRunAfterTheOthersAreSet(t *testing.T) {
	hygiene.Isolate(t)
	answers := append(visibleAnswers(),
		testsupport.GHAnswer{Args: secretArgs("me/plain"), Stderr: "gh: Forbidden (HTTP 403)", Exit: 1},
		present("me/tool"), present("org/lib"),
		testsupport.GHAnswer{Args: setArgs("me/tool")}, testsupport.GHAnswer{Args: setArgs("org/lib")},
	)
	gh := testsupport.FakeGH(t, answers...)
	r := sync(t, testsupport.NewFakeHTTP(t, npmAccepts()), false, secrets.SyncRequest{All: true, Npmrc: tokenFile(t)})
	if r.ExitCode != 1 || !strings.Contains(r.Stdout, "me/plain: failed: could not read its secrets") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if got := sets(gh); len(got) != 2 {
		t.Fatalf("the readable targets were not all set: %v", got)
	}
}

func TestADryRunSetsNothingAndNeverRendersTheToken(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, present("owner/repo"), testsupport.GHAnswer{Args: setArgs("owner/repo")})
	repo, _ := github.ParseRepository("owner/repo")
	r := sync(t, testsupport.NewFakeHTTP(t, npmAccepts()), true, secrets.SyncRequest{Current: repo, Npmrc: tokenFile(t)})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if got := sets(gh); len(got) != 0 {
		t.Fatalf("a dry run wrote a secret: %v", got)
	}
	if !strings.Contains(r.Stdout, "owner/repo: would set NPM_TOKEN") || !strings.Contains(r.Stdout, "gh secret set NPM_TOKEN --repo owner/repo") {
		t.Errorf("stdout:\n%s", r.Stdout)
	}
	noTokenAnywhere(t, gh, r)
}

func TestSetNpmTokenPipesTheTokenOnStdin(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: setArgs("o/r")})
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(ctx *strictcli.Context) error {
		client, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		repo, _ := github.ParseRepository("o/r")
		return secrets.SetNpmToken(client, repo, token, "")
	})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	calls := gh.Calls()
	if len(calls) != 1 || calls[0].Stdin != token || strings.Contains(strings.Join(calls[0].Args, " "), token) {
		t.Fatalf("calls: %+v", calls)
	}
}

func TestAGrantTheCommandDoesNotDeclareRefusesTheWrite(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: setArgs("o/r")})
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(ctx *strictcli.Context) error {
		client, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		repo, _ := github.ParseRepository("o/r")
		return secrets.SetNpmToken(client, repo, token, "set-secret")
	})
	if r.ExitCode == 0 || len(gh.Calls()) != 0 {
		t.Fatalf("exit %d, calls %v", r.ExitCode, gh.Calls())
	}
}
