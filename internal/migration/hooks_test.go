package migration

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// hookWritingCwd is a customized hook script that records where it ran.
const hookWritingCwd = "#!/usr/bin/env bash\npwd > ran-here\n"

// runMigratedHooks migrates the fixture for real and runs the releasable's
// hooks at the point through the release's hook runner, from the
// representative member.
func runMigratedHooks(t *testing.T, f *fixture, releasable, representative string, point release.HookPoint) {
	t.Helper()
	f.migrate()
	ws, err := workspace.Load(f.repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	runs, err := release.HookRuns(ws, releasable, representative, point, release.HookContext{Version: "0.4.0"})
	if err != nil {
		t.Fatal(err)
	}
	if len(runs) == 0 {
		t.Fatalf("the migrated declarations give the releasable %q no %s hook", releasable, point)
	}
	var runErr error
	r := testsupport.RunCommand(t, commandOptions(false), func(ctx *strictcli.Context) error {
		runErr = release.RunHooks(ctx.Effects(), runs, nil, 0)
		return runErr
	})
	if runErr != nil || r.ExitCode != 0 {
		t.Fatalf("the migrated hook did not run: %v\nstdout:\n%s\nstderr:\n%s", runErr, r.Stdout, r.Stderr)
	}
}

func ranIn(t *testing.T, f *fixture, memberDir string) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(f.repo.Dir, memberDir, "ran-here"))
	if err != nil {
		t.Fatalf("the hook did not run from %s: %v", memberDir, err)
	}
	want, _ := filepath.EvalSymlinks(filepath.Join(f.repo.Dir, memberDir))
	got, _ := filepath.EvalSymlinks(strings.TrimSpace(string(data)))
	if got != want {
		t.Fatalf("the hook ran in %s, not %s", got, want)
	}
}

func TestAMigratedWorkspaceReleasableHookRunsThroughTheReleasesHookRunner(t *testing.T) {
	hygiene.Isolate(t)
	f := workspaceFixture(t)
	f.write(".rlsbl-monorepo/releasables/widget/hooks/pre-release.sh", hookWritingCwd)
	f.commit("hook script")
	runMigratedHooks(t, f, "widget", "widget", release.PreRelease)
	ranIn(t, f, "widget")
}

func TestAMigratedHookOfAReleasableWithMembersAtSeveralDepthsRunsFromEveryMember(t *testing.T) {
	hygiene.Isolate(t)
	f := workspaceFixture(t)
	f.edit(".rlsbl-monorepo/workspace.toml", "[layers]", "[[projects]]\npath = \"libs/widget-core\"\nname = \"widget-core\"\nreleasable = \"widget\"\n\n[layers]")
	f.write("libs/widget-core/package.json", `{"name": "widget-core", "version": "0.3.0", "license": "MIT"}`+"\n")
	f.write(".rlsbl-monorepo/releasables/widget/hooks/pre-release.sh", hookWritingCwd)
	f.commit("a deeper member and a hook script")
	d, err := declarations.Parse([]byte(planned(t, f.mustPlan(), declarations.ReleasablesFile)))
	if err != nil {
		t.Fatal(err)
	}
	r, _ := d.Releasable("widget")
	if len(r.Hooks.PreRelease) != 1 || r.Hooks.PreRelease[0].Command != `bash "$(git rev-parse --show-toplevel)/.strictmetadata/release-hooks/widget/pre-release.sh"` {
		t.Fatalf("the releasable's hooks: %+v", r.Hooks)
	}
	runMigratedHooks(t, f, "widget", "widget-core", release.PreRelease)
	ranIn(t, f, "libs/widget-core")
}
