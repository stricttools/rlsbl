package releaseops_test

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaseops"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func edit(t *testing.T, p *project, dryRun bool, version string) strictcli.Result {
	t.Helper()
	return run(t, nil, dryRun, func(ctx *strictcli.Context) error {
		return releaseops.Edit(ctx, releaseops.EditRequest{Dir: p.Dir, Version: version, IndexPath: p.indexPath})
	})
}

func TestEditRewritesTheLatestReleaseFromTheRecord(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.4.0", false)})
	r := edit(t, p, false, "")
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "Rewrote the GitHub Release of v0.4.0")
	c, ok := called(gh.Calls(), rewriteArgs("v0.4.0", false))
	if !ok {
		t.Fatalf("the Release was not rewritten: %+v", gh.Calls())
	}
	commit := p.Git("rev-parse", "v0.4.0")
	if !strings.Contains(c.Stdin, "shipped in 0.4.0") || !strings.Contains(c.Stdin, "<!-- rlsbl-ci-sha: "+commit+" -->") {
		t.Fatalf("the body is %q", c.Stdin)
	}
}

func TestEditKeepsTheRecordedNoticesOnTopAndThePreReleaseFlag(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	testsupport.FakeSafegit(t)
	testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.3.0", true)})
	requireExit(t, deprecate(t, p, false, releaseops.NoticeRequest{Version: "0.3.0", Reason: "Broken"}), 0)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.3.0", true)})
	requireExit(t, edit(t, p, false, "0.3.0"), 0)
	c, ok := called(gh.Calls(), rewriteArgs("v0.3.0", true))
	if !ok || !strings.HasPrefix(c.Stdin, "> **Deprecated:** Broken.\n\n") {
		t.Fatalf("calls %+v", gh.Calls())
	}
}

func TestEditRefusesAMissingReleaseUntilItExists(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	testsupport.FakeGH(t, authStatus, releaseMissing("v0.4.0"))
	r := edit(t, p, false, "")
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "has no GitHub Release for v0.4.0", "rlsbl release reconcile")
	testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.4.0", false)})
	requireExit(t, edit(t, p, false, ""), 0)
}

func TestEditRefusesAVersionTheRecordHoldsNoReleaseOf(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	gh := testsupport.FakeGH(t, authStatus)
	r := edit(t, p, false, "0.9.0")
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "has no release archive")
	if len(gh.Calls()) != 0 {
		t.Fatalf("gh was asked: %+v", gh.Calls())
	}
}

func TestADryRunEditRewritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.4.0"))
	r := edit(t, p, true, "0.4.0")
	requireExit(t, r, 0)
	if _, ok := called(gh.Calls(), rewriteArgs("v0.4.0", false)); ok {
		t.Fatal("a dry run rewrote the Release")
	}
}
