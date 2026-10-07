package checks

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

const memberCI = "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: go test ./...\n"

// syncRouter writes the CI router `rlsbl monorepo sync` would write for the
// workspace as it stands.
func syncRouter(t *testing.T, r *testsupport.Repo) {
	t.Helper()
	ws, err := workspace.Load(r.Dir)
	if err != nil {
		t.Fatal(err)
	}
	actions, err := scaffold.ActionVersions()
	if err != nil {
		t.Fatal(err)
	}
	router, routes, err := workflows.CIRouter(ws, workflows.RouterInputs{Actions: actions})
	if err != nil || !routes {
		t.Fatalf("rendering the router: routes %v, %v", routes, err)
	}
	r.Write(workflows.RouterPath, router)
}

func TestAWorkspaceWithMemberCIFailsWithoutARouterUntilSynced(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-ci-router"), "pass")
	r.Write("widget/.github/workflows/ci.yml", memberCI)
	got := runCheck(t, inputs(t, r.Dir), "workspace-ci-router")
	mustStatus(t, got, "fail")
	mustMention(t, got, "widget", "rlsbl monorepo sync")
	syncRouter(t, r)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-ci-router"), "pass")
}

func TestAGeneratedRouterWithNothingToRouteFailsUntilSyncRemovesIt(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write(workflows.RouterPath, workflows.Header+"\nname: CI Router\n")
	got := runCheck(t, inputs(t, r.Dir), "workspace-ci-router")
	mustStatus(t, got, "fail")
	mustMention(t, got, "removes it")
	r.Write(workflows.RouterPath, "name: A router written by hand\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-ci-router"), "pass")
}

func TestAMemberCIWorkflowTheRouterDoesNotInlineFailsUntilSynced(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write("widget/.github/workflows/ci.yml", memberCI)
	got := runCheck(t, inputs(t, r.Dir), "workspace-ci-synced")
	mustStatus(t, got, "fail")
	mustMention(t, got, "does not exist", "rlsbl monorepo sync")
	syncRouter(t, r)
	got = runCheck(t, inputs(t, r.Dir), "workspace-ci-synced")
	mustStatus(t, got, "pass")
	mustMention(t, got, "gadget: no CI workflow of its own")
	r.Write("gadget/.github/workflows/ci-lint.yml", memberCI)
	got = runCheck(t, inputs(t, r.Dir), "workspace-ci-synced")
	mustStatus(t, got, "fail")
	mustMention(t, got, "gadget-ci-lint")
	syncRouter(t, r)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-ci-synced"), "pass")
}

func TestRouterFiltersDriftingFromTheWorkspaceFailUntilSynced(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write("widget/.github/workflows/ci.yml", memberCI)
	got := runCheck(t, inputs(t, r.Dir), "router-filters-fresh")
	mustStatus(t, got, "skip")
	syncRouter(t, r)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "router-filters-fresh"), "pass")
	// A lockfile at the root is a trigger of every member's filter.
	r.Write("go.work", "go 1.26\n\nuse (\n\t./gadget\n\t./widget\n)\n")
	got = runCheck(t, inputs(t, r.Dir), "router-filters-fresh")
	mustStatus(t, got, "fail")
	mustMention(t, got, "widget: committed", "go.work", "rlsbl monorepo sync")
	syncRouter(t, r)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "router-filters-fresh"), "pass")
}

func TestARouterWithTheDefaultQuantifierFails(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write("widget/.github/workflows/ci.yml", memberCI)
	syncRouter(t, r)
	data, err := os.ReadFile(r.Path(workflows.RouterPath))
	if err != nil {
		t.Fatal(err)
	}
	r.Write(workflows.RouterPath, strings.Replace(string(data), "predicate-quantifier: "+workflows.PredicateQuantifier, "predicate-quantifier: some", 1))
	got := runCheck(t, inputs(t, r.Dir), "router-filters-fresh")
	mustStatus(t, got, "fail")
	mustMention(t, got, `predicate-quantifier is "some"`)
}

func TestARouterWrittenByHandIsNotJudgedFresh(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write(workflows.RouterPath, "name: A router written by hand\n")
	got := runCheck(t, inputs(t, r.Dir), "router-filters-fresh")
	mustStatus(t, got, "skip")
	mustMention(t, got, "not a generated router")
}
