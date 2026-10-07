package scaffold

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
)

func TestTheRunnersWayBackToTheRoot(t *testing.T) {
	hygiene.Isolate(t)
	cases := map[string]string{
		"test-sandbox.sh":               ".",
		"scripts/test-sandbox.sh":       "..",
		"tools/scripts/test-sandbox.sh": "../..",
		"a/b/c/test-sandbox.sh":         "../../..",
	}
	for path, want := range cases {
		if got := rootRelative(path); got != want {
			t.Errorf("rootRelative(%q) = %q, want %q", path, got, want)
		}
	}
}

// The runner renders from its settings with every placeholder answered:
// extra environment variables sorted, ignored paths quoted, and the uv
// overlay block only for a runner declaring the uv cache.
func TestTheRunnerRendersFromItsSettings(t *testing.T) {
	hygiene.Isolate(t)
	r := &declarations.TestRunner{
		RunnerPath:   "scripts/test-sandbox.sh",
		Command:      "scripts/go-suite.sh",
		Caches:       []string{"go"},
		Prewarm:      []string{"go mod download"},
		ExtraEnv:     map[string]string{"ZED": "1", "ALPHA": "2"},
		CarryIgnored: []string{".env.test"},
	}
	vars := TestRunnerVars(r)
	if vars["sandboxExtraEnv"] != "  --setenv ALPHA 2\n  --setenv ZED 1" {
		t.Errorf("extra env: %q", vars["sandboxExtraEnv"])
	}
	if vars["sandboxCarryIgnored"] != "'.env.test'" || vars["sandboxRootRelative"] != ".." || vars["sandboxUvOverlays"] != "" {
		t.Errorf("vars: %v", vars)
	}
	text, err := renderTemplate(testRunnerTemplate, vars)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(text, "scripts/go-suite.sh") || !strings.Contains(text, "go mod download") || !strings.Contains(text, `SANDBOX_CACHES="go"`) {
		t.Fatalf("the runner:\n%s", text)
	}
	r.Caches = []string{"uv", "go"}
	if TestRunnerVars(r)["sandboxUvOverlays"] != "1" {
		t.Error("a runner declaring the uv cache has no uv overlay block")
	}
}
