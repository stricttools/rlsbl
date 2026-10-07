package releaseops_test

import (
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaseops"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

const publishWorkflow = `name: publish
on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      tag:
        description: "Release tag to publish"
        required: false
        type: string
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
`

const ciWorkflow = `name: ci
on:
  push:
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo test
`

// portalWithWorkflows is portal with a publish and a CI workflow, both
// dispatchable, and 0.3.0 and 0.4.0 released.
func portalWithWorkflows(t *testing.T) *project {
	t.Helper()
	p := newPortal(t, "package.json")
	p.Write(".github/workflows/publish.yml", publishWorkflow)
	p.Write(".github/workflows/ci.yml", ciWorkflow)
	p.Commit("the workflows", ".github/workflows/publish.yml", ".github/workflows/ci.yml")
	p.release("portal", "0.3.0")
	p.release("portal", "0.4.0")
	return p
}

func dispatchArgs(workflow string, tagInput bool) []string {
	args := []string{"workflow", "run", workflow, "--repo", slug, "--ref", "v0.4.0"}
	if tagInput {
		args = append(args, "-f", "tag=v0.4.0")
	}
	return args
}

func retry(t *testing.T, p *project, dryRun bool) strictcli.Result {
	t.Helper()
	return run(t, nil, dryRun, func(ctx *strictcli.Context) error {
		return releaseops.Retry(ctx, releaseops.RetryRequest{Dir: p.Dir, Sleep: func(time.Duration) {}})
	})
}

func TestRetryWritesTheRetryFileAndDispatchesAtTheTag(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithWorkflows(t)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"),
		testsupport.GHAnswer{Args: dispatchArgs("ci.yml", false)},
		testsupport.GHAnswer{Args: dispatchArgs("publish.yml", true)})
	r := retry(t, p, false)
	requireExit(t, r, 0)
	commit := p.Git("rev-parse", "v0.4.0")
	requireContains(t, r.Stdout, "Dispatched ci.yml, publish.yml at v0.4.0", "rlsbl watch "+commit)
	for _, argv := range [][]string{dispatchArgs("ci.yml", false), dispatchArgs("publish.yml", true)} {
		if _, ok := called(gh.Calls(), argv); !ok {
			t.Fatalf("%q was not dispatched: %+v", argv, gh.Calls())
		}
	}
	if p.exists(runstate.RetryPath("portal")) {
		t.Fatal("the retry file was kept after every workflow was dispatched")
	}
}

func TestARetryFileNamingAnotherRefIsRefusedUntilItNamesTheTag(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithWorkflows(t)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"), testsupport.GHAnswer{Args: dispatchArgs("publish.yml", true)})
	p.Write(runstate.RetryPath("portal"), "format_version = 1\nref = \"main\"\nworkflows = [\"publish.yml\"]\n")
	r := retry(t, p, false)
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, `sets ref = "main"`, `Set ref = "v0.4.0"`)
	for _, c := range gh.Calls() {
		if c.Args[0] == "workflow" {
			t.Fatalf("a refused retry dispatched: %q", c.Args)
		}
	}
	p.Write(runstate.RetryPath("portal"), "format_version = 1\nref = \"v0.4.0\"\nworkflows = [\"publish.yml\"]\n")
	requireExit(t, retry(t, p, false), 0)
	if _, ok := called(gh.Calls(), dispatchArgs("publish.yml", true)); !ok {
		t.Fatalf("calls %+v", gh.Calls())
	}
}

func TestRetryRefusesWithoutAGitHubReleaseUntilOneExists(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithWorkflows(t)
	gh := testsupport.FakeGH(t, authStatus, releaseMissing("v0.4.0"))
	r := retry(t, p, false)
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "has no GitHub Release for v0.4.0", "rlsbl release reconcile")
	for _, c := range gh.Calls() {
		if c.Args[0] == "workflow" {
			t.Fatalf("a refused retry dispatched: %q", c.Args)
		}
	}
	testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"),
		testsupport.GHAnswer{Args: dispatchArgs("ci.yml", false)},
		testsupport.GHAnswer{Args: dispatchArgs("publish.yml", true)})
	requireExit(t, retry(t, p, false), 0)
}

func TestAFailedDispatchLeavesOnlyTheRestToRetry(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithWorkflows(t)
	first := testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"),
		testsupport.GHAnswer{Args: dispatchArgs("ci.yml", false)},
		testsupport.GHAnswer{Args: dispatchArgs("publish.yml", true), Stderr: "HTTP 500\n", Exit: 1})
	r := retry(t, p, false)
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "Dispatched before it: ci.yml", "holds the workflows not yet dispatched")
	if _, ok := called(first.Calls(), dispatchArgs("ci.yml", false)); !ok {
		t.Fatal("ci.yml was not dispatched")
	}
	requireContains(t, p.read(runstate.RetryPath("portal")), `workflows = ["publish.yml"]`)
	second := testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"), testsupport.GHAnswer{Args: dispatchArgs("publish.yml", true)})
	requireExit(t, retry(t, p, false), 0)
	var dispatched [][]string
	for _, c := range second.Calls() {
		if c.Args[0] == "workflow" {
			dispatched = append(dispatched, c.Args)
		}
	}
	if len(dispatched) != 1 || !slices.Equal(dispatched[0], dispatchArgs("publish.yml", true)) {
		t.Fatalf("the second run dispatched %q", dispatched)
	}
}

func TestAnUnreadableRetryFileIsRefusedAndKept(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithWorkflows(t)
	testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"))
	p.Write(runstate.RetryPath("portal"), "format_version = 1\nref = \"v0.4.0\"\n")
	r := retry(t, p, false)
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "remove it and run the retry again")
	if !p.exists(runstate.RetryPath("portal")) {
		t.Fatal("the retry file was removed")
	}
}

func TestARetryFileNamingAWorkflowTheTaggedTreeLacksIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithWorkflows(t)
	testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"))
	p.Write(runstate.RetryPath("portal"), "format_version = 1\nref = \"v0.4.0\"\nworkflows = [\"deploy.yml\"]\n")
	r := retry(t, p, false)
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "names deploy.yml", "ci.yml, publish.yml")
}

func TestADryRunRetryDispatchesNothing(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithWorkflows(t)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"))
	r := retry(t, p, true)
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "Would dispatch ci.yml, publish.yml at v0.4.0")
	for _, c := range gh.Calls() {
		if c.Args[0] == "workflow" {
			t.Fatalf("a dry run dispatched: %q", c.Args)
		}
	}
	if p.exists(runstate.RetryPath("portal")) {
		t.Fatal("a dry run wrote the retry file")
	}
}

func TestRetryNeedsARelease(t *testing.T) {
	hygiene.Isolate(t)
	p := newPortal(t, "package.json")
	r := retry(t, p, false)
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "record no release", "rlsbl release resume")
	if strings.Contains(r.Stderr, "panic") {
		t.Fatal(r.Stderr)
	}
}
