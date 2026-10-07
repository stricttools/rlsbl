package release_test

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

// releaseDeclarations declare the standalone project portal on GitHub as
// acme/portal; %s adds keys to its root member.
const releaseDeclarations = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "ci"

[[members]]
path = "."
name = "root"
releasable = "portal"
%s`

const releaseFilePath = ".strictmetadata/releases/portal/unreleased.toml"

func releaseFile(bump, include, exclude string) string {
	return fmt.Sprintf("format_version = 2\nbump = %q\ninclude = [%s]\nexclude = [%s]\ndescription = \"the next release\"\n", bump, include, exclude)
}

// readyRepo is portal released at 0.4.0, then a feature the changelog
// describes as user-facing, a minor release file, and origin holding main.
func readyRepo(t *testing.T, memberKeys string) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(declarationsPath, fmt.Sprintf(releaseDeclarations, memberKeys))
	repo.Write("package.json", packageJSON("portal", "0.4.0"))
	repo.Commit("the project", declarationsPath, "package.json")
	recordRelease(t, repo, "portal", "0.4.0", "v0.4.0", "recorded")
	feature := repo.CommitFile("feature.txt", "a feature\n", "add a feature")
	unreleased := changelog.Dir("portal") + "/" + changelog.UnreleasedName
	repo.Write(unreleased, changelog.Serialize(changelog.Entry{ID: strings.Repeat("ab", 24), Commits: []string{feature}, UserFacing: true, Type: "feature", Description: "A feature."})+"\n")
	repo.Write(releaseFilePath, releaseFile("minor", `"npm"`, ""))
	repo.Commit("prepare the release", unreleased, releaseFilePath)
	repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	return repo
}

// gitHub answers the questions validation asks gh about acme/portal.
func gitHub(t *testing.T, visibility string, push bool) *testsupport.GH {
	t.Helper()
	private := visibility != "public"
	return testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: []string{"--version"}, Stdout: "gh version 2.0.0\n"},
		testsupport.GHAnswer{Args: []string{"auth", "status", "--hostname", "github.com"}},
		testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/acme/portal"}, Stdout: fmt.Sprintf(`{"full_name":"acme/portal","visibility":%q,"private":%t,"archived":false,"permissions":{"push":%t}}`, visibility, private, push)},
		testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "user", "--jq", ".login"}, Stdout: "octo\n"},
	)
}

var validationDay = time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)

func validate(t *testing.T, repo *testsupport.Repo, fake *testsupport.FakeHTTP, adjust func(*release.Request)) (*release.Validated, error) {
	t.Helper()
	return validateIn(t, false, repo, fake, adjust)
}

// validateIn validates as validate does, under --dry-run when dryRun is set.
func validateIn(t *testing.T, dryRun bool, repo *testsupport.Repo, fake *testsupport.FakeHTTP, adjust func(*release.Request)) (*release.Validated, error) {
	t.Helper()
	if fake == nil {
		fake = testsupport.NewFakeHTTP(t)
	}
	var v *release.Validated
	var verr error
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes(), HTTPClient: fake.Client()}, func(ctx *strictcli.Context) error {
		e := ctx.Effects()
		gh, err := github.New(e)
		if err != nil {
			return err
		}
		reg, err := registry.New(registry.Reads(e))
		if err != nil {
			return err
		}
		req := release.Request{Root: repo.Dir, LiveRoot: repo.Dir, Dir: repo.Dir, Now: validationDay, GitHub: gh, Registry: reg}
		if adjust != nil {
			adjust(&req)
		}
		v, verr = release.Validate(e, req)
		return verr
	})
	return v, verr
}

func TestAReleaseMeetingEveryGuardIsValidated(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	v, err := validate(t, repo, nil, nil)
	mustNotFail(t, err)
	if v.Decision.Version.String() != "0.5.0" || v.Decision.Tag != "v0.5.0" || v.CommitMessage != "v0.5.0" || v.Branch != "main" || v.Primary != "npm" || v.Releasable.Name != "portal" || v.Representative.Name != "root" {
		t.Errorf("validated: %+v", v)
	}
	if v.GitHub.String() != "acme/portal" || v.CompletedState != nil || v.PublishesFromCI {
		t.Errorf("validated: %+v", v)
	}
}

func TestAnUnfinishedReleaseRefusesUntilItsStateIsGone(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	state := runstate.InProgress{Releasable: "portal", RepresentativeMember: "root", Version: "0.5.0", Tag: "v0.5.0", Branch: "main", Bump: "minor", CommitMessage: "v0.5.0", Description: "the next release", Include: []string{"npm"}, Exclude: []string{}, PreReleaseCommit: repo.Head(), PinCommit: repo.Head(), CompletedSteps: []string{release.StepVersionBumped}}
	statePath := repo.Path(runstate.InProgressPath("portal"))
	testsupport.WriteFile(t, statePath, string(state.Render()))
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "rlsbl release resume") || !strings.Contains(err.Error(), "rlsbl release abandon") || !strings.Contains(err.Error(), "1 of 12 steps done") {
		t.Fatalf("the unfinished release was not refused naming its ways on: %v", err)
	}
	// What `release abandon` does to the state: it is gone.
	if err := os.Remove(statePath); err != nil {
		t.Fatal(err)
	}
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
	// A state whose every step is done is handed back, to be cleared.
	state.CompletedSteps = release.StepNames()
	testsupport.WriteFile(t, statePath, string(state.Render()))
	v, err := validate(t, repo, nil, nil)
	mustNotFail(t, err)
	if v.CompletedState == nil || v.CompletedState.Version != "0.5.0" {
		t.Errorf("the complete state was not handed back: %+v", v.CompletedState)
	}
}

func lifecycleRecord(status string) string {
	return fmt.Sprintf("format_version = 1\n\n[[lifecycle]]\nsubject = \"portal\"\nstatus = %q\nfrom = 2026-01-01\nreason = \"the plan\"\n\n[[licenses]]\nsubject = \"portal\"\nlicense = \"MIT\"\nfrom = 2026-01-01\nreason = \"open\"\n", status)
}

const recordPath = ".strictmetadata/lifecycle-and-license/lifecycle-and-license.toml"

func TestAReleasableOnHoldIsRefusedUntilActiveAgain(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	repo.CommitFile(recordPath, lifecycleRecord("on-hold"), "hold portal")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "on-hold") {
		t.Fatalf("an on-hold releasable was released: %v", err)
	}
	// The fix the refusal names: an active lifecycle period.
	repo.CommitFile(recordPath, lifecycleRecord("active"), "resume portal")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

func TestAProprietaryReleasableInAPublicRepositoryIsRefusedUntilItIsPrivate(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	repo.CommitFile(recordPath, strings.Replace(lifecycleRecord("active"), `license = "MIT"`, `license = "proprietary"`, 1), "classify portal")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "proprietary-requires-private") {
		t.Fatalf("a proprietary releasable in a public repository was released: %v", err)
	}
	// The fix: the repository is made private on GitHub.
	gitHub(t, "private", true)
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

func TestATargetTheReleaseFileDoesNotNameIsRefusedUntilNamed(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	repo.CommitFile("pyproject.toml", "[project]\nname = \"portal\"\nversion = \"0.4.0\"\n", "a python package too")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "neither include nor exclude: pypi") {
		t.Fatalf("an unnamed target was released: %v", err)
	}
	repo.CommitFile(releaseFilePath, releaseFile("minor", `"npm"`, `"pypi"`), "exclude pypi")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

func TestAReleaseFileNamingATargetTheProjectLacksIsRefusedUntilRemoved(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	repo.CommitFile(releaseFilePath, releaseFile("minor", `"npm"`, `"go"`), "exclude go")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "no member of the releasable \"portal\" has: go") {
		t.Fatalf("a target the project lacks was accepted: %v", err)
	}
	repo.CommitFile(releaseFilePath, releaseFile("minor", `"npm"`, ""), "drop go")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

const localNpm = `targets = [{ name = "npm" }]

[[members.pipelines]]
name = "npm"
type = "npm"
target = "npm"
local = true
artifact = "package"
`

func TestALocalPipelineLackingItsCredentialIsRefusedUntilItIsSet(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, localNpm)
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "needs NPM_TOKEN") {
		t.Fatalf("a missing credential was not refused: %v", err)
	}
	// The fix the refusal names: the variable, here through the
	// environment file.
	repo.Write("release.env", "NPM_TOKEN=token\n")
	repo.CommitFile(declarationsPath, strings.Replace(read(t, repo.Path(declarationsPath)), "github_repository", "environment_file = \"release.env\"\ngithub_repository", 1), "declare the environment file")
	v, err := validate(t, repo, nil, nil)
	mustNotFail(t, err)
	if v.Environment["NPM_TOKEN"] != "token" {
		t.Errorf("the environment file was not loaded: %v", v.Environment)
	}
}

func TestALocalPipelineOfAReleasablePublishingNothingIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, localNpm)
	repo.CommitFile(declarationsPath, strings.Replace(read(t, repo.Path(declarationsPath)), `publish_mode = "ci"`, `publish_mode = "none"`, 1), "publish nothing")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "publishes nothing") {
		t.Fatalf("a local pipeline of a releasable publishing nothing was accepted: %v", err)
	}
}

func TestTheBumpDecidesWhetherAUserFacingEntryIsRequired(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	repo.CommitFile(releaseFilePath, releaseFile("infra", `"npm"`, ""), "an infra release")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "an infra release has no user-facing changelog entry") {
		t.Fatalf("an infra release with a user-facing entry was accepted: %v", err)
	}
	unreleased := changelog.Dir("portal") + "/" + changelog.UnreleasedName
	repo.CommitFile(unreleased, entryLine(repo.Head()), "an internal entry")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
	repo.CommitFile(releaseFilePath, releaseFile("patch", `"npm"`, ""), "a patch release")
	_, err = validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "a patch release needs at least one user-facing changelog entry") {
		t.Fatalf("a patch release without a user-facing entry was accepted: %v", err)
	}
}

func TestAnExistingTagIsRefusedUntilDeleted(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	repo.Git("tag", "v0.5.0")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "git tag -d v0.5.0") {
		t.Fatalf("an existing tag was not refused naming its deletion: %v", err)
	}
	repo.Git("tag", "-d", "v0.5.0")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

func TestATagOnOriginIsRefusedUntilDeletedThere(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	repo.Git("tag", "v0.5.0")
	repo.Git("push", "-q", "origin", "v0.5.0")
	repo.Git("tag", "-d", "v0.5.0")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "git push origin :refs/tags/v0.5.0") || strings.Contains(err.Error(), "git tag -d") {
		t.Fatalf("a tag on origin was not refused naming its deletion there, and only there: %v", err)
	}
	// The fetch of origin copies no tag here, so the deletion on origin
	// alone clears the refusal.
	repo.Git("push", "-q", "origin", ":refs/tags/v0.5.0")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

// The validation's fetch of origin follows no tag, so a dry run against an
// origin holding a tag this repository lacks (on a commit the fetch brings)
// creates no tag here.
func TestADryRunCreatesNoTagFromOrigin(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	repo.Git("tag", "-a", "-m", "a marker", "marker")
	repo.Git("push", "-q", "origin", "marker")
	repo.Git("tag", "-d", "marker")
	before := repo.Git("tag", "--list")
	_, err := validateIn(t, true, repo, nil, nil)
	mustNotFail(t, err)
	if after := repo.Git("tag", "--list"); after != before {
		t.Fatalf("a dry run created tags here: before %q, after %q", before, after)
	}
}

func TestABranchBehindOriginIsRefusedUntilPulled(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	other := filepath.Join(t.TempDir(), "other")
	if _, stderr, code := testsupport.RunGit(t, filepath.Dir(other), "clone", "-q", repo.Git("remote", "get-url", "origin"), other); code != 0 {
		t.Fatal(stderr)
	}
	testsupport.WriteFile(t, filepath.Join(other, "theirs.txt"), "theirs\n")
	commitIn(t, other, "someone else's push")
	if _, stderr, code := testsupport.RunGit(t, other, "push", "-q", "origin", "main"); code != 0 {
		t.Fatal(stderr)
	}
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "1 commit(s) behind origin/main") || !strings.Contains(err.Error(), "git pull --ff-only") {
		t.Fatalf("a branch behind origin was not refused: %v", err)
	}
	repo.Git("pull", "-q", "--ff-only", "origin", "main")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

func TestABranchThatIsNotAReleaseBranchIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	repo.Git("checkout", "-q", "-b", "topic")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), `cannot release from "topic"`) {
		t.Fatalf("a topic branch was released: %v", err)
	}
	repo.Git("checkout", "-q", "main")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

func TestAnAccountThatMayNotPushIsRefusedNamingItAndAToken(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", false)
	repo := readyRepo(t, "")
	t.Setenv("GH_TOKEN", "a-token")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "octo") || !strings.Contains(err.Error(), "unset GH_TOKEN") {
		t.Fatalf("an account without push access was not refused: %v", err)
	}
	var ve *release.ValidationError
	if !errors.As(err, &ve) {
		t.Errorf("not a validation refusal: %T", err)
	}
}

// overlay declares and records the dev overlay of gadget from checkout.
func overlay(t *testing.T, repo *testsupport.Repo, checkout string) {
	t.Helper()
	text := fmt.Sprintf("[[overlay]]\npackage = \"gadget\"\npath = %q\n", checkout)
	repo.Write("dev-sources.toml.local-only", text)
	repo.Write("dev-overlays-state.toml.local-only", text)
	repo.CommitFile(".gitignore", "*.local-only\n", "ignore local state")
}

func pypiAnswer(latest string) testsupport.HTTPAnswer {
	return testsupport.HTTPAnswer{Method: "GET", URL: "https://pypi.org/pypi/gadget/json", Status: 200, Body: fmt.Sprintf(`{"info":{"version":%q},"releases":{%q:[]}}`, latest, latest)}
}

func TestADevOverlayAheadOfPyPIIsRefusedUntilTheDependencyIsReleased(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	checkout := filepath.Join(t.TempDir(), "gadget")
	testsupport.WriteFile(t, filepath.Join(checkout, "pyproject.toml"), "[project]\nname = \"gadget\"\nversion = \"0.3.0\"\n")
	overlay(t, repo, checkout)
	_, err := validate(t, repo, testsupport.NewFakeHTTP(t, pypiAnswer("0.2.0")), nil)
	if err == nil || !strings.Contains(err.Error(), "release the dependency first") || !strings.Contains(err.Error(), "gadget 0.3.0") {
		t.Fatalf("an overlay ahead of PyPI was not refused: %v", err)
	}
	// The fix the refusal names: the dependency is released.
	_, err = validate(t, repo, testsupport.NewFakeHTTP(t, pypiAnswer("0.3.0")), nil)
	mustNotFail(t, err)
}

func TestARecordDroppingWhatTheReleaseCommitHeldIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := testsupport.NewRepo(t)
	repo.Write(declarationsPath, fmt.Sprintf(releaseDeclarations, ""))
	repo.Write("package.json", packageJSON("portal", "0.4.0"))
	held := lifecycleRecord("active") + "\n[[registry_names]]\nregistry = \"npm\"\nname = \"portal\"\nsubject = \"portal\"\nrecorded_since = 2026-01-01\n"
	repo.Write(recordPath, held)
	repo.Commit("the project", declarationsPath, "package.json", recordPath)
	recordRelease(t, repo, "portal", "0.4.0", "v0.4.0", "recorded")
	feature := repo.CommitFile("feature.txt", "a feature\n", "add a feature")
	unreleased := changelog.Dir("portal") + "/" + changelog.UnreleasedName
	repo.Write(unreleased, changelog.Serialize(changelog.Entry{ID: strings.Repeat("cd", 24), Commits: []string{feature}, UserFacing: true, Type: "feature", Description: "A feature."})+"\n")
	repo.Write(releaseFilePath, releaseFile("minor", `"npm"`, ""))
	repo.Write(recordPath, lifecycleRecord("active"))
	repo.Commit("prepare the release, dropping the held name", unreleased, releaseFilePath, recordPath)
	repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "against the lifecycle-and-license record at") {
		t.Fatalf("a record dropping a held registry name was released: %v", err)
	}
	repo.CommitFile(recordPath, held, "keep the held name")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

func TestADeployCommandOnAReleasableThatIsNoServerIsRefusedUntilDeleted(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "public", true)
	repo := readyRepo(t, "")
	declared := read(t, repo.Path(declarationsPath))
	repo.CommitFile(declarationsPath, strings.Replace(declared, `publish_mode = "ci"`, "publish_mode = \"ci\"\ndeploy_command = [\"true\"]", 1), "deploy portal")
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "declares deploy_command, which only a server releasable declares") || !strings.Contains(err.Error(), "or delete deploy_command") {
		t.Fatalf("a deploy command on a releasable that is no server was released: %v", err)
	}
	// The fix the refusal names: deploy_command deleted.
	repo.CommitFile(declarationsPath, declared, "no deploy")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}

const goLibrary = `targets = [{ name = "npm" }, { name = "go" }]

[[members.pipelines]]
name = "go"
type = "go"
target = "go"
local = false
artifact = "library"
`

func TestAGoLibraryOfAProprietaryReleasableIsRefusedUntilItPublishesNoLibrary(t *testing.T) {
	hygiene.Isolate(t)
	gitHub(t, "private", true)
	repo := readyRepo(t, goLibrary)
	repo.Write("go.mod", "module github.com/acme/portal\n\ngo 1.22\n")
	repo.Write(releaseFilePath, releaseFile("minor", `"npm"`, `"go"`))
	repo.Write(recordPath, strings.Replace(lifecycleRecord("active"), `license = "MIT"`, `license = "proprietary"`, 1))
	repo.Commit("a proprietary go library", "go.mod", releaseFilePath, recordPath)
	_, err := validate(t, repo, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "go-library") || !strings.Contains(err.Error(), `the go pipeline "go" of the member "root"`) {
		t.Fatalf("a proprietary releasable's go library was released: %v", err)
	}
	// The fix: the pipeline publishing the library is gone.
	declared := read(t, repo.Path(declarationsPath))
	repo.CommitFile(declarationsPath, strings.Replace(declared, goLibrary, `targets = [{ name = "npm" }, { name = "go" }]`+"\n", 1), "publish no go library")
	_, err = validate(t, repo, nil, nil)
	mustNotFail(t, err)
}
