package cli

import (
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func newTestApp(t *testing.T) *strictcli.App {
	t.Helper()
	app, err := New(Dependencies{Version: "0.131.0", HTTPClient: testsupport.NewFakeHTTP(t).Client()})
	if err != nil {
		t.Fatal(err)
	}
	return app
}

func TestTheAppReportsItsVersion(t *testing.T) {
	hygiene.Isolate(t)
	r := newTestApp(t).Test([]string{"--version"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "0.131.0") {
		t.Fatalf("--version: exit %d, %q", r.ExitCode, r.Stdout)
	}
}

// An unversioned binary is refused, naming the VERSION file; a version
// written there (handed in, as the binary hands in the embedded file) clears
// the refusal.
func TestNewRefusesAMissingVersionOrHTTPClient(t *testing.T) {
	hygiene.Isolate(t)
	_, err := New(Dependencies{HTTPClient: &http.Client{}})
	if err == nil || !strings.Contains(err.Error(), "VERSION file") {
		t.Fatalf("an unversioned app: %v", err)
	}
	if _, err := New(Dependencies{Version: "0.1.0", HTTPClient: &http.Client{}}); err != nil {
		t.Fatalf("with a version: %v", err)
	}
	if _, err := New(Dependencies{Version: "0.1.0"}); err == nil {
		t.Fatal("an app without an HTTP client was built")
	}
}

// The app takes its observe allowlist from the declared one, or the
// standard governs a list nothing uses.
func TestTheAppInstallsTheDeclaredObserveAllowlist(t *testing.T) {
	hygiene.Isolate(t)
	raw, ok := newTestApp(t).DumpSchemaDict()["proc_observe_allowlist"].([]interface{})
	if !ok {
		t.Fatal("the app declares no observe allowlist")
	}
	var got [][]string
	for _, entry := range raw {
		var prefix []string
		for _, tok := range entry.([]interface{}) {
			prefix = append(prefix, tok.(string))
		}
		got = append(got, prefix)
	}
	want := previewapply.Prefixes()
	if len(got) != len(want) {
		t.Fatalf("the app holds %d prefixes, the declared list %d", len(got), len(want))
	}
	for i := range want {
		if !slices.Equal(got[i], want[i]) {
			t.Fatalf("prefix %d is %q, declared %q", i, got[i], want[i])
		}
	}
}

func TestTheChecksCommandsAreRegistered(t *testing.T) {
	hygiene.Isolate(t)
	cmds := newTestApp(t).Commands()
	for _, name := range []string{"check", "failing-checks"} {
		if _, ok := cmds[name]; !ok {
			t.Errorf("the checks registry did not register %s", name)
		}
	}
}

func TestTheSourceTreeRootIsThisCheckout(t *testing.T) {
	hygiene.Isolate(t)
	root, ok := sourceTreeRoot()
	if !ok {
		t.Fatal("a test binary has no source tree root")
	}
	got, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	want, err := filepath.EvalSymlinks(testsupport.ModuleRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("source tree root = %s, want %s", got, want)
	}
	if _, err := os.Stat(filepath.Join(root, "internal", "cli", "app.go")); err != nil {
		t.Fatal(err)
	}
}
