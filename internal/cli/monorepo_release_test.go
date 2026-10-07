package cli

import (
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMonorepoReleaseOrderThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	monorepoFixture(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	payload := jsonPayload(t, app.Test([]string{"monorepo", "release", "order", "--json"}))
	want := map[string]any{
		"members":     []any{"root", "widget", "gadget"},
		"independent": false,
		"releasables": []any{"widget", "gadget"},
	}
	if !reflect.DeepEqual(payload, want) {
		t.Fatalf("the order payload: %v", payload)
	}
	r := app.Test([]string{"monorepo", "release", "order"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Releasables, in the order a batch release releases them:\n  1. widget\n  2. gadget") {
		t.Fatalf("the order: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestMonorepoReleaseInitAndRunThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	monorepoFixture(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	if r := app.Test([]string{"monorepo", "release", "init", "--dry-run"}); r.ExitCode == 0 {
		t.Fatalf("monorepo release init accepted --dry-run:\n%s", r.Stdout)
	}
	if r := app.Test([]string{"monorepo", "release", "run", "--approve-consequential"}); r.ExitCode == 0 || !strings.Contains(r.Stderr, "watch") {
		t.Fatalf("monorepo release run ran without --watch or --no-watch: exit %d\n%s", r.ExitCode, r.Stderr)
	}
	if r := app.Test([]string{"monorepo", "release", "init", "--releasables", "portal"}); r.ExitCode == 0 || !strings.Contains(r.Stderr, "--releasables names portal") {
		t.Fatalf("an undeclared releasable was accepted: exit %d\n%s", r.ExitCode, r.Stderr)
	}
}

func TestMonorepoReleaseInitNeedsTheReleasablesOrAllStated(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := monorepoFixture(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"monorepo", "release", "init"})
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, "--releasables") || !strings.Contains(r.Stderr, "--all") {
		t.Fatalf("an init stating no releasables was not refused naming --releasables and --all: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if r := app.Test([]string{"monorepo", "release", "init", "--all", "--releasables", "widget"}); r.ExitCode == 0 {
		t.Fatalf("--all with --releasables was accepted:\n%s", r.Stdout)
	}
	if _, err := os.Stat(repo.Path(".strictmetadata/batch-releases/unreleased.toml")); !os.IsNotExist(err) {
		t.Fatalf("a refused init wrote the batch release file: %v", err)
	}
	// The fix the refusal names: --all states every releasable.
	if r := app.Test([]string{"monorepo", "release", "init", "--all"}); r.ExitCode != 0 {
		t.Fatalf("--all: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	text, err := os.ReadFile(repo.Path(".strictmetadata/batch-releases/unreleased.toml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(text), "releasables.widget]") || !strings.Contains(string(text), "releasables.gadget]") {
		t.Fatalf("--all did not write a table per releasable:\n%s", text)
	}
}
