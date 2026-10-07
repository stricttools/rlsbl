package checks

import (
	"fmt"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// githubPortal declares portal on GitHub as acme/portal; the publish mode
// is filled in.
var githubPortal = strings.Replace(standalonePortal, "release_branches = [\"main\"]\n", "release_branches = [\"main\"]\ngithub_repository = \"acme/portal\"\n", 1)

// npmPortal is portal publishing an npm package from CI, on GitHub.
var npmPortal = fmt.Sprintf(githubPortal, "ci") + `targets = [{ name = "npm" }]

[[members.pipelines]]
name = "npm"
type = "npm"
target = "npm"
local = false
artifact = "package"
`

func npmRepo(t *testing.T, record string) *testsupport.Repo {
	t.Helper()
	return newRepo(t, npmPortal, map[string]string{
		"package.json": fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"),
		recordFile:     record,
	})
}

func TestARefMissingOnOriginFailsUntilItIsPushed(t *testing.T) {
	hygiene.Isolate(t)
	r, _, _ := releasedPortal(t)
	r.AddBareRemote("origin")
	r.Git("push", "-q", "origin", "main")
	got := runCheck(t, inputs(t, r.Dir), "unpublished-refs")
	mustStatus(t, got, "fail")
	mustMention(t, got, "v1.0.0", "not on origin", "rlsbl release reconcile")
	// What the reconcile pushes.
	r.Git("push", "-q", "origin", "v1.0.0")
	got = runCheck(t, inputs(t, r.Dir), "unpublished-refs")
	mustStatus(t, got, "pass")
	mustMention(t, got, "every ref exists for 1 released version(s)")
}

func TestAMissingOrMovedLocalRefFails(t *testing.T) {
	hygiene.Isolate(t)
	r, released, next := releasedPortal(t)
	r.Git("tag", "-d", "v1.0.0")
	got := runCheck(t, inputs(t, r.Dir), "unpublished-refs")
	mustStatus(t, got, "fail")
	mustMention(t, got, "does not exist locally")
	r.Git("tag", "v1.0.0", next)
	got = runCheck(t, inputs(t, r.Dir), "unpublished-refs")
	mustStatus(t, got, "fail")
	mustMention(t, got, "the release recorded "+released)
	r.Git("tag", "-f", "v1.0.0", released)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "unpublished-refs"), "pass")
}

func TestANeverReleasedVersionOwesNoRefs(t *testing.T) {
	hygiene.Isolate(t)
	r, _, _ := releasedPortal(t)
	r.Write(".strictmetadata/releases/portal/v1.1.0.toml", "format_version = 2\nbump = \"minor\"\ninclude = []\nexclude = []\ndescription = \"abandoned\"\nnever_released = true\n")
	got := runCheck(t, inputs(t, r.Dir), "unpublished-refs")
	mustStatus(t, got, "pass")
	mustMention(t, got, "never released", "1.1.0")
}

func TestATagOnOriginWithoutAGitHubReleaseFails(t *testing.T) {
	hygiene.Isolate(t)
	r, _, _ := releasedPortal(t)
	r.Write(".strictmetadata/releasables/releasables.toml", fmt.Sprintf(githubPortal, "none"))
	r.AddBareRemote("origin")
	r.Git("push", "-q", "origin", "main", "v1.0.0")
	list := []string{"release", "list", "--repo", "acme/portal", "--limit", "1000", "--json", "tagName", "--jq", ".[].tagName"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: list, Stdout: ""},
		testsupport.GHAnswer{Args: list, Stdout: "v1.0.0\n"},
	)
	got := runCheck(t, inputs(t, r.Dir), "unpublished-refs")
	mustStatus(t, got, "fail")
	mustMention(t, got, "carries no GitHub Release")
	// After the reconcile creates the Release, the listing names its tag.
	got = runCheck(t, inputs(t, r.Dir), "unpublished-refs")
	mustStatus(t, got, "pass")
	mustMention(t, got, "every ref and GitHub Release")
}

func TestBranchSyncAgainstTheRemoteTrackingBranch(t *testing.T) {
	hygiene.Isolate(t)
	origin := portalRepo(t, "none", nil)
	clone := origin.Clone()
	mustStatus(t, runCheck(t, inputs(t, clone.Dir), "branch-sync"), "pass")
	origin.CommitFile("theirs.txt", "theirs\n", "A commit only origin has")
	clone.Git("fetch", "-q", "origin")
	got := runCheck(t, inputs(t, clone.Dir), "branch-sync")
	mustStatus(t, got, "fail")
	mustMention(t, got, "1 commit(s) behind origin/main", "git pull --ff-only")
	clone.Git("pull", "-q", "--ff-only")
	mustStatus(t, runCheck(t, inputs(t, clone.Dir), "branch-sync"), "pass")
	clone.CommitFile("ours.txt", "ours\n", "A commit only the clone has")
	mustStatus(t, runCheck(t, inputs(t, clone.Dir), "branch-sync"), "warn")
}

func TestBranchSyncSkipsWithoutARemoteTrackingBranch(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "branch-sync"), "skip")
}

func TestAMissingCIPublishSecretFailsNamingTheCommandThatSetsIt(t *testing.T) {
	hygiene.Isolate(t)
	r := npmRepo(t, publicRecord)
	get := []string{"api", "--method", "GET", "repos/acme/portal/actions/secrets/NPM_TOKEN"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: get, Stderr: "gh: Not Found (HTTP 404)\n", Exit: 1},
		testsupport.GHAnswer{Args: get, Stdout: `{"name":"NPM_TOKEN","updated_at":"2026-10-01T00:00:00Z"}`},
	)
	got := runCheck(t, inputs(t, r.Dir), "ci-publish-secrets")
	mustStatus(t, got, "fail")
	mustMention(t, got, "gh secret set NPM_TOKEN --repo acme/portal", "root/npm")
	// After the secret is set, GitHub reports it.
	got = runCheck(t, inputs(t, r.Dir), "ci-publish-secrets")
	mustStatus(t, got, "pass")
	mustMention(t, got, "acme/portal carries NPM_TOKEN")
}

func TestAnUnansweredSecretProbeIsAnError(t *testing.T) {
	hygiene.Isolate(t)
	r := npmRepo(t, publicRecord)
	testsupport.FakeGH(t, testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/acme/portal/actions/secrets/NPM_TOKEN"}, Stderr: "gh: Forbidden (HTTP 403)\n", Exit: 1})
	got := runCheck(t, inputs(t, r.Dir), "ci-publish-secrets")
	mustStatus(t, got, "fail")
	mustMention(t, got, "unanswered probe")
}

func TestCICredentialChecksSkipWhatPublishesNothingFromCI(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	for _, name := range []string{"ci-publish-secrets", "npm-token-synced"} {
		got := runCheck(t, inputs(t, r.Dir), name)
		mustStatus(t, got, "skip")
		mustMention(t, got, `"none"`)
	}
}

func TestNpmTokenSyncedNamesAMissingLocalToken(t *testing.T) {
	hygiene.Isolate(t)
	r := npmRepo(t, publicRecord)
	testsupport.FakeGH(t)
	got := runCheck(t, inputs(t, r.Dir), "npm-token-synced")
	mustStatus(t, got, "fail")
	mustMention(t, got, "CI cannot publish to npm")
}

func TestAProprietaryReleasablePublishesNothing(t *testing.T) {
	hygiene.Isolate(t)
	r := npmRepo(t, proprietaryRecord)
	testsupport.FakeGH(t)
	got := runCheck(t, inputs(t, r.Dir), "private-repo-publishing")
	mustStatus(t, got, "fail")
	mustMention(t, got, "proprietary")
}

func TestPrivateRepoPublishingSkipsWhenNothingPublishes(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "private-repo-publishing"), "skip")
}

// movedRecord is the public record of portal, which left acme/oldportal
// (repository and module path) on 2026-05-01.
const movedRecord = publicRecord + `
[[identities]]
subject = "portal"
facet = "repository-url"
value = "https://github.com/acme/oldportal"
registry = ""
tag_patterns = []
from = 2026-01-01
until = 2026-05-01
reason = "the first home"

[[identities]]
subject = "portal"
facet = "repository-url"
value = "https://github.com/acme/portal"
registry = ""
tag_patterns = []
from = 2026-05-01
reason = "moved"

[[identities]]
subject = "portal"
facet = "go-module-path"
value = "github.com/acme/oldportal"
registry = "go"
tag_patterns = ["v*"]
from = 2026-01-01
until = 2026-05-01
reason = "the first home"

[[identities]]
subject = "portal"
facet = "go-module-path"
value = "github.com/acme/portal"
registry = "go"
tag_patterns = ["v*"]
from = 2026-05-01
reason = "moved"
`

func TestAnEarlierRepositoryMustBeArchived(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{recordFile: movedRecord})
	get := []string{"api", "--method", "GET", "repos/acme/oldportal"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: get, Stdout: `{"full_name":"acme/oldportal","visibility":"public","private":false,"archived":false}`},
		testsupport.GHAnswer{Args: get, Stdout: `{"full_name":"acme/oldportal","visibility":"public","private":false,"archived":true}`},
	)
	got := runCheck(t, inputs(t, r.Dir), "old-repo-archived")
	mustStatus(t, got, "fail")
	mustMention(t, got, "gh repo archive acme/oldportal")
	// After `gh repo archive`, GitHub reports it archived.
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "old-repo-archived"), "pass")
}

func TestOldRepoArchivedSkipsWithoutAClosedRepositoryIdentity(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "old-repo-archived"), "skip")
}

func TestASupersededModulePathMustServeADeprecation(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{recordFile: movedRecord})
	list := testsupport.HTTPAnswer{Method: "GET", URL: "https://proxy.golang.org/github.com/acme/oldportal/@v/list", Status: 200, Body: "v1.0.0\n"}
	plain := testsupport.NewFakeHTTP(t, list,
		testsupport.HTTPAnswer{Method: "GET", URL: "https://proxy.golang.org/github.com/acme/oldportal/@v/v1.0.0.mod", Status: 200, Body: "module github.com/acme/oldportal\n"})
	got := runCheckWith(t, inputs(t, r.Dir), "go-deprecation-published", runOptions{http: plain.Client()})
	mustStatus(t, got, "fail")
	mustMention(t, got, "// Deprecated: moved to github.com/acme/portal")
	// After the old repository releases its deprecation.
	deprecated := testsupport.NewFakeHTTP(t, list,
		testsupport.HTTPAnswer{Method: "GET", URL: "https://proxy.golang.org/github.com/acme/oldportal/@v/v1.0.0.mod", Status: 200, Body: "// Deprecated: moved to github.com/acme/portal\nmodule github.com/acme/oldportal\n"})
	mustStatus(t, runCheckWith(t, inputs(t, r.Dir), "go-deprecation-published", runOptions{http: deprecated.Client()}), "pass")
	for _, req := range append(plain.Requests(), deprecated.Requests()...) {
		if strings.HasSuffix(req.URL, "@latest") {
			t.Errorf("the check asked for @latest: %s", req.URL)
		}
	}
}
