package release_test

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// hookedDeclarations declare the releasable kit with the members widget and
// gadget, each point carrying hooks on the releasable and on both members.
// %s is the dir of the widget's pre-checks hook.
const hookedDeclarations = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "kit"
tag_format = "{name}@v{version}"
publish_mode = "none"
hooks = { pre_checks = ["echo kit-pre-checks"], pre_release = ["echo kit-pre-release"], post_release = ["echo kit-post-release"] }

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "widget"
name = "widget"
releasable = "kit"
hooks = { pre_checks = [{ cmd = "echo widget-pre-checks", dir = "%s", env = { STAGE = "widget" } }], pre_release = ["echo widget-pre-release"] }

[[members]]
path = "gadget"
name = "gadget"
releasable = "kit"
hooks = { pre_checks = ["echo gadget-pre-checks"], pre_release = ["echo gadget-pre-release"], post_release = ["echo gadget-post-release"] }

[[members]]
path = "widget/inner"
name = "inner"
releasable = "kit"
`

func hookedWorkspace(t *testing.T, widgetDir string) *workspace.Workspace {
	t.Helper()
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(declarations.ReleasablesFile)), fmt.Sprintf(hookedDeclarations, widgetDir))
	for _, dir := range []string{"widget/src", "widget/inner", "gadget"} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	ws, err := workspace.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	return ws
}

func commands(runs []release.HookRun) string {
	var out []string
	for _, r := range runs {
		out = append(out, r.Command)
	}
	return strings.Join(out, ",")
}

func TestHooksRunInTheirPointsOrder(t *testing.T) {
	hygiene.Isolate(t)
	ws := hookedWorkspace(t, "src")
	c := release.HookContext{Version: "1.2.0", Bump: "minor", PreviousVersion: "1.1.0", Description: "a release"}
	for point, want := range map[release.HookPoint]string{
		release.PreChecks:   "echo kit-pre-checks,echo gadget-pre-checks,echo widget-pre-checks",
		release.PreRelease:  "echo gadget-pre-release,echo widget-pre-release,echo kit-pre-release",
		release.PostRelease: "echo kit-post-release,echo gadget-post-release",
	} {
		runs, err := release.HookRuns(ws, "kit", "widget", point, c)
		mustNotFail(t, err)
		if got := commands(runs); got != want {
			t.Errorf("%s: %s", point, got)
		}
	}
}

func TestAHookRunsWhereItsDeclarerIsWithTheReleasesVariables(t *testing.T) {
	hygiene.Isolate(t)
	ws := hookedWorkspace(t, "src")
	runs, err := release.HookRuns(ws, "kit", "gadget", release.PreChecks, release.HookContext{Version: "1.2.0", Bump: "minor", PreviousVersion: "1.1.0", Description: "a release"})
	mustNotFail(t, err)
	kit, gadget, widget := runs[0], runs[1], runs[2]
	if kit.Dir != filepath.Join(ws.Root, "gadget") || kit.Env["RLSBL_PACKAGE"] != "" {
		t.Errorf("the releasable's hook runs from the representative member without a package: %+v", kit)
	}
	if gadget.Dir != filepath.Join(ws.Root, "gadget") || gadget.Env["RLSBL_PACKAGE"] != "gadget" {
		t.Errorf("gadget's hook: %+v", gadget)
	}
	if widget.Dir != filepath.Join(ws.Root, "widget", "src") || widget.Env["STAGE"] != "widget" || widget.Env["RLSBL_VERSION"] != "1.2.0" || widget.Env["RLSBL_BUMP_TYPE"] != "minor" || widget.Env["RLSBL_PREV_VERSION"] != "1.1.0" || widget.Env["RLSBL_DESCRIPTION"] != "a release" {
		t.Errorf("widget's hook: %+v", widget)
	}
}

func TestAHookDirInAnotherMembersTerritoryIsRefusedUntilDeclaredThere(t *testing.T) {
	hygiene.Isolate(t)
	ws := hookedWorkspace(t, "inner")
	_, err := release.HookRuns(ws, "kit", "widget", release.PreChecks, release.HookContext{Version: "1.2.0"})
	if err == nil || !strings.Contains(err.Error(), `the member "inner" owns`) || !strings.Contains(err.Error(), `hooks of the member "inner"`) {
		t.Fatalf("a hook dir in a nested member was not refused naming it: %v", err)
	}
	// The fix the refusal names: the hook moves to the member owning the
	// directory, which runs it from its own directory.
	ws = hookedWorkspace(t, "src")
	_, err = release.HookRuns(ws, "kit", "widget", release.PreChecks, release.HookContext{Version: "1.2.0"})
	mustNotFail(t, err)
}

func TestAFatalHookStopsTheRunAndPostReleaseHooksAllRun(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	runs := func(point release.HookPoint, commands ...string) []release.HookRun {
		var out []release.HookRun
		for _, c := range commands {
			out = append(out, release.HookRun{Point: point, Declarer: `the releasable "kit"`, Command: c, Dir: dir, Env: map[string]string{"RLSBL_VERSION": "1.2.0"}})
		}
		return out
	}
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		return release.RunHooks(e, runs(release.PreRelease, "echo first >> log", "exit 3", "echo never >> log"), map[string]string{"EXTRA": "x"}, time.Minute)
	})
	if err == nil || !strings.Contains(err.Error(), "`exit 3`") {
		t.Fatalf("a failing pre-release hook was not the error: %v", err)
	}
	if got := read(t, filepath.Join(dir, "log")); got != "first\n" {
		t.Errorf("the run went on past a fatal failure: %q", got)
	}
	_, err = mutating(t, false, func(e *strictcli.Effects) error {
		return release.RunHooks(e, runs(release.PostRelease, "exit 4", "echo $RLSBL_VERSION $EXTRA >> after", "exit 5"), map[string]string{"EXTRA": "x"}, 0)
	})
	if err == nil || !strings.Contains(err.Error(), "`exit 4`") || !strings.Contains(err.Error(), "`exit 5`") {
		t.Fatalf("the post-release failures were not all named: %v", err)
	}
	if got := read(t, filepath.Join(dir, "after")); got != "1.2.0 x\n" {
		t.Errorf("a post-release hook after a failure did not run with the release's variables: %q", got)
	}
}

func TestADeclaredPreReleaseHookIsTheReleasablesOrTheMembersOwn(t *testing.T) {
	hygiene.Isolate(t)
	ws := hookedWorkspace(t, "src")
	kit, _ := ws.Declarations.Releasable("kit")
	widget, _ := ws.Declarations.Member("widget")
	inner, _ := ws.Declarations.Member("inner")
	if by, ok := release.DeclaresPreReleaseHook(kit, inner); !ok || by != `the releasable "kit"` {
		t.Errorf("the releasable's hook: %q %v", by, ok)
	}
	kit.Hooks.PreRelease = nil
	if by, ok := release.DeclaresPreReleaseHook(kit, widget); !ok || by != `the member "widget"` {
		t.Errorf("the member's hook: %q %v", by, ok)
	}
	if _, ok := release.DeclaresPreReleaseHook(kit, inner); ok {
		t.Error("a member without a hook under a releasable without one declares one")
	}
}
