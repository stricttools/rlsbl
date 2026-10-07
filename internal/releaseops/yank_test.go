package releaseops_test

import (
	"regexp"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaseops"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func yank(t *testing.T, p *project, fake *testsupport.FakeHTTP, dryRun bool, req releaseops.NoticeRequest) strictcli.Result {
	t.Helper()
	req.Dir, req.IndexPath = p.Dir, p.indexPath
	return run(t, fake, dryRun, func(ctx *strictcli.Context) error { return releaseops.Yank(ctx, req) })
}

// packageLevel are the only registry URLs a yank or an undo may request:
// the npm package document, the PyPI project document, and the Go proxy's
// version list. None names a version.
var packageLevel = []*regexp.Regexp{
	regexp.MustCompile(`^https://registry\.npmjs\.org/[^/@]+$`),
	regexp.MustCompile(`^https://pypi\.org/pypi/[^/]+/json$`),
	regexp.MustCompile(`^https://proxy\.golang\.org/[^@]+/@v/list$`),
}

// requirePackageLevel fails the test when a request named one version.
func requirePackageLevel(t *testing.T, fake *testsupport.FakeHTTP, versions ...string) {
	t.Helper()
	for _, r := range fake.Requests() {
		if r.Method != "GET" {
			t.Fatalf("a registry request was %s %s", r.Method, r.URL)
		}
		matched := false
		for _, re := range packageLevel {
			matched = matched || re.MatchString(r.URL)
		}
		if !matched {
			t.Fatalf("%s is not a package-level listing", r.URL)
		}
		for _, v := range versions {
			if strings.Contains(r.URL, v) {
				t.Fatalf("%s names the version %s", r.URL, v)
			}
		}
	}
}

const proprietaryRecord = `format_version = 1

[[licenses]]
subject = "portal"
license = "proprietary"
from = 2026-01-01
reason = "the server's logic"
`

func TestYankRefusesEveryRegistryWriteForAProprietaryReleasable(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	p.Write(lifecycle.RecordFile, proprietaryRecord)
	p.Commit("classify portal", lifecycle.RecordFile)
	npm := fakeNpm(t)
	testsupport.FakeSafegit(t)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.3.0", true)})
	fake := testsupport.NewFakeHTTP(t, npmDocument("0.3.0", "0.4.0"))
	head := p.Head()
	r := yank(t, p, fake, false, releaseops.NoticeRequest{Version: "0.3.0", Reason: "Broken"})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "no registry write is made for a proprietary releasable", "rlsbl release deprecate 0.3.0")
	if len(npm()) != 0 || len(fake.Requests()) != 0 || len(gh.Calls()) != 0 || p.Head() != head {
		t.Fatalf("a refused yank acted: npm %q, http %q, gh %+v", npm(), fake.URLs(), gh.Calls())
	}
	// The way the refusal names: mark the Release without a registry write.
	requireExit(t, deprecate(t, p, false, releaseops.NoticeRequest{Version: "0.3.0", Reason: "Broken"}), 0)
	if len(npm()) != 0 || len(fake.Requests()) != 0 {
		t.Fatal("deprecate wrote to a registry")
	}
}

func TestYankDeprecatesOnNpmAndMarksTheReleaseWithoutUnpublishing(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	npm := fakeNpm(t)
	testsupport.FakeSafegit(t)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.3.0", true)})
	fake := testsupport.NewFakeHTTP(t, npmDocument("0.3.0", "0.4.0"))
	r := yank(t, p, fake, false, releaseops.NoticeRequest{Version: "0.3.0", Reason: "Broken parser", Use: "0.4.0"})
	requireExit(t, r, 0)
	calls := npm()
	if len(calls) != 1 || calls[0] != "deprecate portal@0.3.0 Broken parser. Use v0.4.0 instead." {
		t.Fatalf("npm calls %q", calls)
	}
	for _, c := range calls {
		if strings.Contains(c, "unpublish") {
			t.Fatalf("npm was asked to unpublish: %q", c)
		}
	}
	for _, c := range gh.Calls() {
		if c.Args[0] == "release" && c.Args[1] == "delete" {
			t.Fatalf("the Release was deleted: %q", c.Args)
		}
	}
	edit, ok := called(gh.Calls(), rewriteArgs("v0.3.0", true))
	if !ok || !strings.HasPrefix(edit.Stdin, "> **Yanked:** Broken parser. Use v0.4.0 instead.\n\n") {
		t.Fatalf("calls %+v", gh.Calls())
	}
	if got := p.subjects(1)[0]; got != "release: yank v0.3.0" {
		t.Fatalf("the last commit is %q", got)
	}
	requirePackageLevel(t, fake, "0.3.0")
}

func TestYankLeavesAnUnpublishedPackageAlone(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	npm := fakeNpm(t)
	testsupport.FakeSafegit(t)
	testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.3.0", true)})
	fake := testsupport.NewFakeHTTP(t, npmDocument("0.4.0"))
	requireExit(t, yank(t, p, fake, false, releaseops.NoticeRequest{Version: "0.3.0"}), 0)
	if len(npm()) != 0 {
		t.Fatalf("npm was run for an unpublished version: %q", npm())
	}
}

func TestYankRefusesAPackageWhosePublicationCannotBeRead(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	npm := fakeNpm(t)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"))
	fake := testsupport.NewFakeHTTP(t, get("https://registry.npmjs.org/portal", 500, "down"))
	head := p.Head()
	r := yank(t, p, fake, false, releaseops.NoticeRequest{Version: "0.3.0"})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "could not be read for every package", "Nothing was changed", "HTTP 500")
	if len(npm()) != 0 || p.Head() != head {
		t.Fatal("a refused yank acted")
	}
	if _, ok := called(gh.Calls(), rewriteArgs("v0.3.0", true)); ok {
		t.Fatal("a refused yank rewrote the Release")
	}
}

func TestYankRetractsAGoModuleInACommitOfItsOwn(t *testing.T) {
	hygiene.Isolate(t)
	p := newPortal(t, "go.mod")
	p.release("portal", "0.3.0")
	p.release("portal", "0.4.0")
	testsupport.FakeSafegit(t)
	testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.3.0", true)})
	listing := get("https://proxy.golang.org/github.com/acme/portal/@v/list", 200, "v0.3.0\nv0.4.0\n")
	fake := testsupport.NewFakeHTTP(t, listing)
	requireExit(t, yank(t, p, fake, false, releaseops.NoticeRequest{Version: "0.3.0", Reason: "Broken parser"}), 0)
	requireContains(t, p.read("go.mod"), "retract v0.3.0 // Broken parser\n")
	subjects := p.subjects(2)
	if subjects[0] != "release: yank v0.3.0" || !strings.HasPrefix(subjects[1], "Retract v0.3.0 of github.com/acme/portal") {
		t.Fatalf("the last commits are %q", subjects)
	}
	if strings.Contains(p.Git("log", "-1", "--format=%B", "HEAD~1"), "Autogenerated") {
		t.Fatal("the retraction is exempt from changelog coverage; its release should say it")
	}
	requirePackageLevel(t, fake, "0.3.0")
	// A second yank finds the retraction and the notice already there.
	head := p.Head()
	requireExit(t, yank(t, p, testsupport.NewFakeHTTP(t, listing), false, releaseops.NoticeRequest{Version: "0.3.0", Reason: "Broken parser"}), 0)
	if p.Head() != head || strings.Count(p.read("go.mod"), "retract") != 1 {
		t.Fatal("the second yank wrote again")
	}
}

func TestYankRefusesAGoModWithUncommittedChangesUntilTheyAreCommitted(t *testing.T) {
	hygiene.Isolate(t)
	p := newPortal(t, "go.mod")
	p.release("portal", "0.3.0")
	p.release("portal", "0.4.0")
	testsupport.FakeSafegit(t)
	testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.3.0", true)})
	listing := get("https://proxy.golang.org/github.com/acme/portal/@v/list", 200, "v0.3.0\n")
	p.Write("go.mod", "module github.com/acme/portal\n\ngo 1.26\n\nrequire golang.org/x/mod v0.20.0\n")
	r := yank(t, p, testsupport.NewFakeHTTP(t, listing), false, releaseops.NoticeRequest{Version: "0.3.0"})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "go.mod has uncommitted changes", "commit or discard them")
	p.Commit("require x/mod", "go.mod")
	requireExit(t, yank(t, p, testsupport.NewFakeHTTP(t, listing), false, releaseops.NoticeRequest{Version: "0.3.0"}), 0)
	requireContains(t, p.read("go.mod"), "retract v0.3.0\n")
}

func TestYankOfAPyPIReleaseFailsUntilItsFilesAreYankedByHand(t *testing.T) {
	hygiene.Isolate(t)
	p := newPortal(t, "pyproject.toml")
	p.release("portal", "0.3.0")
	p.release("portal", "0.4.0")
	testsupport.FakeSafegit(t)
	testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"), testsupport.GHAnswer{Args: rewriteArgs("v0.3.0", true)})
	listed := get("https://pypi.org/pypi/portal/json", 200, `{"releases":{"0.3.0":[{"yanked":false}],"0.4.0":[{"yanked":false}]}}`)
	fake := testsupport.NewFakeHTTP(t, listed)
	r := yank(t, p, fake, false, releaseops.NoticeRequest{Version: "0.3.0"})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "PyPI is not done", "https://pypi.org/manage/project/portal/release/0.3.0/", "run this yank again")
	requireContains(t, p.read(".strictmetadata/releases/portal/v0.3.0.toml"), "> **Yanked.**")
	requirePackageLevel(t, fake, "0.3.0")
	// The steps done by hand: PyPI now marks the release's files yanked.
	yanked := get("https://pypi.org/pypi/portal/json", 200, `{"releases":{"0.3.0":[{"yanked":true}],"0.4.0":[{"yanked":false}]}}`)
	requireExit(t, yank(t, p, testsupport.NewFakeHTTP(t, yanked), false, releaseops.NoticeRequest{Version: "0.3.0"}), 0)
}

func TestADryRunYankWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	p := portalWithTwoReleases(t)
	npm := fakeNpm(t)
	safegit := testsupport.FakeSafegit(t)
	gh := testsupport.FakeGH(t, authStatus, releaseExists("v0.3.0"))
	head := p.Head()
	r := yank(t, p, testsupport.NewFakeHTTP(t, npmDocument("0.3.0", "0.4.0")), true, releaseops.NoticeRequest{Version: "0.3.0"})
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "Would yank v0.3.0")
	if len(npm()) != 0 || len(safegit.Calls()) != 0 || p.Head() != head {
		t.Fatal("a dry run acted")
	}
	if _, ok := called(gh.Calls(), rewriteArgs("v0.3.0", true)); ok {
		t.Fatal("a dry run rewrote the Release")
	}
}
