package cli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// appWith builds the application with its HTTP requests going to fake.
func appWith(t *testing.T, fake *testsupport.FakeHTTP) *strictcli.App {
	t.Helper()
	app, err := New(Dependencies{Version: "0.131.0", HTTPClient: fake.Client()})
	if err != nil {
		t.Fatal(err)
	}
	return app
}

// jsonPayload is the payload of a --json run.
func jsonPayload(t *testing.T, r strictcli.Result) map[string]any {
	t.Helper()
	var envelope map[string]any
	if err := json.Unmarshal([]byte(r.Stdout), &envelope); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, r.Stdout)
	}
	payload, ok := envelope["payload"].(map[string]any)
	if !ok {
		t.Fatalf("no payload object in %s", r.Stdout)
	}
	return payload
}

func httpGet(url string, status int, body string) testsupport.HTTPAnswer {
	return testsupport.HTTPAnswer{Method: "GET", URL: url, Status: status, Body: body}
}

func TestCheckNameGoPayloadAndExitCodes(t *testing.T) {
	hygiene.Isolate(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"check-name", "--target", "go", "--json", "json"})
	if r.ExitCode != 1 {
		t.Fatalf("exit %d: %s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	results := jsonPayload(t, r)["results"].([]any)
	got := results[0].(map[string]any)
	if got["status"] != "taken" || got["reason"] != "stdlib" || got["exit_code"].(float64) != 1 {
		t.Fatalf("%v", got)
	}
	conflicts := got["structured_conflicts"].([]any)
	if len(conflicts) != 1 || conflicts[0].(map[string]any)["name"] != "encoding/json" || conflicts[0].(map[string]any)["rule"] != "go-stdlib" {
		t.Fatalf("%v", conflicts)
	}
	if _, ok := got["rule_sentences"].(map[string]any)["go-stdlib"]; !ok {
		t.Fatalf("no sentence for go-stdlib: %v", got)
	}
	r = app.Test([]string{"check-name", "--target", "go", "--json", "testsandbox"})
	available := jsonPayload(t, r)["results"].([]any)[0].(map[string]any)
	if r.ExitCode != 0 || available["reason"] != nil || len(available["structured_conflicts"].([]any)) != 0 || len(available["rule_sentences"].(map[string]any)) != 0 {
		t.Fatalf("exit %d: %v", r.ExitCode, available)
	}
	if _, ok := available["note"]; ok {
		t.Fatalf("an available name carries a note: %v", available)
	}
}

func TestCheckNameGoHumanOutput(t *testing.T) {
	hygiene.Isolate(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"check-name", "--target", "go", "go-toml-edit"})
	if r.ExitCode != 1 || !strings.Contains(r.Stdout, `"go-toml-edit" is not a valid Go package name.`) {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stdout)
	}
	r = app.Test([]string{"check-name", "--target", "go", "testsandbox"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, `"testsandbox" is available as a Go package name.`) || !strings.Contains(r.Stdout, "Checked: Go package-name rules, Go standard library (offline)") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stdout)
	}
	r = app.Test([]string{"check-name", "--target", "go", "testsandbox", "testing", "go-x"})
	if r.ExitCode != 1 || !strings.Contains(r.Stdout, "Summary: 1 available, 1 taken, 1 invalid (3 total)") || strings.Contains(r.Stdout, "delay") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stdout)
	}
	// A discouraged name exits 1 though Go accepts it, as the help says.
	if r := app.Test([]string{"check-name", "--target", "go", "My_Pkg"}); r.ExitCode != 1 {
		t.Fatalf("a discouraged name exited %d", r.ExitCode)
	}
}

// Every target is checked, and the run exits with the highest code any
// verdict gives: an npm check that could not be made exits 2.
func TestCheckNameAcrossTargetsExitsWithTheHighestCode(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t, httpGet("https://registry.npmjs.org/testsandbox", 503, ""))
	r := appWith(t, fake).Test([]string{"check-name", "--target", "go", "--target", "npm", "--delay", "0", "--json", "testsandbox"})
	if r.ExitCode != 2 {
		t.Fatalf("exit %d: %s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	results := jsonPayload(t, r)["results"].([]any)
	if len(results) != 2 || results[0].(map[string]any)["target"] != "go" || results[1].(map[string]any)["status"] != "error" {
		t.Fatalf("%v", results)
	}
}

func TestCheckNameRefusesAnUnknownTargetAndAnEmptyName(t *testing.T) {
	hygiene.Isolate(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	if r := app.Test([]string{"check-name", "--target", "github", "x"}); r.ExitCode == 0 || !strings.Contains(r.Stderr, "github") {
		t.Fatalf("github: exit %d: %s", r.ExitCode, r.Stderr)
	}
	if r := app.Test([]string{"check-name", "x"}); r.ExitCode == 0 || !strings.Contains(r.Stderr, "target") {
		t.Fatalf("no target: exit %d: %s", r.ExitCode, r.Stderr)
	}
	if r := app.Test([]string{"check-name", "--target", "go", ""}); r.ExitCode != 1 || !strings.Contains(r.Stderr, "must not be empty") {
		t.Fatalf("an empty name: exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestCheckNameHelpStatesTheExitCodes(t *testing.T) {
	hygiene.Isolate(t)
	for _, want := range []string{"Exits 0 when every name is available", "2 when any check ended in an error", "a discouraged Go name exits 1 even though Go accepts it"} {
		if !strings.Contains(checkNameHelp, want) {
			t.Errorf("the help does not say %q", want)
		}
	}
}

// claim-name refuses a taken name before anything is published.
func TestClaimNameRefusesATakenName(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.PathOnly(t)
	fake := testsupport.NewFakeHTTP(t, httpGet("https://registry.npmjs.org/portal", 200, `{}`))
	r := appWith(t, fake).Test([]string{"claim-name", "--target", "npm", "--approve-consequential", "portal"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "appears taken") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

// A PyPI name only a visually similar registered name collides with is
// refused: PyPI blocks it, so publishing a placeholder under it would fail.
func TestClaimNameRefusesAnUltranormCollision(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.PathOnly(t)
	t.Setenv("UV_PUBLISH_TOKEN", "pypi-secret")
	fake := testsupport.NewFakeHTTP(t,
		httpGet("https://pypi.org/simple/lo/", 404, ""),
		httpGet("https://pypi.org/simple/l-o/", 404, ""),
		httpGet("https://pypi.org/simple/l0/", 404, ""),
		httpGet("https://pypi.org/simple/1o/", 200, ""),
	)
	r := appWith(t, fake).Test([]string{"claim-name", "--target", "pypi", "--approve-consequential", "lo"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "visually similar to 1o") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestClaimNameExitsTwoWhenTheCheckCannotBeMade(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.PathOnly(t)
	fake := testsupport.NewFakeHTTP(t, httpGet("https://registry.npmjs.org/portal", 502, ""))
	r := appWith(t, fake).Test([]string{"claim-name", "--target", "npm", "--approve-consequential", "portal"})
	if r.ExitCode != 2 || !strings.Contains(r.Stderr, "502") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

// npmFreeAnswers answers 404 for portal-x and every name npm's moniker rule
// could fold onto it, and an empty search.
func npmFreeAnswers() []testsupport.HTTPAnswer {
	var out []testsupport.HTTPAnswer
	for _, n := range []string{"portal-x", "portal.x", "portal_x", "portalx"} {
		out = append(out, httpGet("https://registry.npmjs.org/"+n, 404, ""))
	}
	return append(out, httpGet("https://registry.npmjs.org/-/v1/search?text=portal-x&size=20", 200, `{"objects":[]}`))
}

func TestClaimNameWithoutCredentialsIsRefusedNamingBothPlaces(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.PathOnly(t)
	t.Setenv("NPM_TOKEN", "")
	r := appWith(t, testsupport.NewFakeHTTP(t, npmFreeAnswers()...)).Test([]string{"claim-name", "--target", "npm", "--approve-consequential", "portal-x"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "NPM_TOKEN") || !strings.Contains(r.Stderr, "~/.npmrc") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

// writeHomeFile writes a file into the test's home directory.
func writeHomeFile(t *testing.T, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(os.Getenv("HOME"), name), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

// A dry run of a claim reads the registry for real and records the
// placeholder's writes and the publish without performing them; the token
// is never rendered.
func TestClaimNameDryRunRecordsThePublishAndRunsNothing(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.PathOnly(t)
	t.Setenv("UV_PUBLISH_TOKEN", "")
	t.Setenv("PYPI_TOKEN", "")
	writeHomeFile(t, ".pypirc", "[pypi]\nusername = __token__\npassword = pypi-topsecret\n")
	fake := testsupport.NewFakeHTTP(t,
		httpGet("https://pypi.org/simple/zx/", 404, ""),
		httpGet("https://pypi.org/simple/z-x/", 404, ""),
	)
	r := appWith(t, fake).Test([]string{"claim-name", "--target", "pypi", "--dry-run", "--approve-consequential", "zx"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d: %s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	out := r.Stdout + r.Stderr
	if !strings.Contains(out, "uv publish") || strings.Contains(out, "pypi-topsecret") || strings.Contains(out, "Claimed") {
		t.Fatalf("the preview:\n%s", out)
	}
}
