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

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// passingChecks passes every check it is asked to run.
type passingChecks struct{ asked []string }

func (p *passingChecks) RunChecks(_ strictcli.CheckContext, opts strictcli.RunChecksOptions) ([]strictcli.CheckRunResult, []string, int, error) {
	p.asked = append(p.asked, opts.NameGlob)
	return []strictcli.CheckRunResult{{Name: opts.NameGlob, Outcome: (&strictcli.ErrorReporter{}).Passed("ok")}}, nil, 0, nil
}

// runDeclarations declare portal, published nowhere, on GitHub as
// acme/portal; %s adds keys to the releasable.
const runDeclarations = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "none"
%s
[[members]]
path = "."
name = "root"
releasable = "portal"
`

// runRecord is portal's lifecycle-and-license record under license, its
// package name portal, and portal-client pending for 0.5.0.
func runRecord(license string) string {
	return fmt.Sprintf(`format_version = 1

[[lifecycle]]
subject = "portal"
status = "active"
from = 2026-01-01
reason = "the plan"

[[licenses]]
subject = "portal"
license = %q
from = 2026-01-01
reason = "the license"

[[identities]]
subject = "portal"
facet = "package-name"
value = "portal"
registry = "npm"
tag_patterns = ["v*"]
from = 2026-01-01
reason = "the first name"

[[identities]]
subject = "portal"
facet = "package-name"
value = "portal-client"
registry = "npm"
tag_patterns = ["v*"]
effective_version = "0.5.0"
reason = "the client's name"
`, license)
}

// ciWorkflow is a workflow triggering on push.
const ciWorkflow = "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"

// runRepo is portal released at 0.4.0, then a feature the changelog
// describes, a minor release file, and the lifecycle-and-license record
// under license, with origin holding main. releasableKeys are added to the
// releasable's declaration; extra files are committed with the project.
func runRepo(t *testing.T, releasableKeys, license string, extra map[string]string) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(declarationsPath, fmt.Sprintf(runDeclarations, releasableKeys))
	repo.Write("package.json", packageJSON("portal", "0.4.0"))
	paths := []string{declarationsPath, "package.json"}
	for p, content := range extra {
		repo.Write(p, content)
		paths = append(paths, p)
	}
	repo.Commit("the project", paths...)
	recordRelease(t, repo, "portal", "0.4.0", "v0.4.0", "recorded")
	feature := repo.CommitFile("feature.txt", "a feature\n", "add a feature")
	unreleased := changelog.Dir("portal") + "/" + changelog.UnreleasedName
	repo.Write(unreleased, changelog.Serialize(changelog.Entry{ID: strings.Repeat("ef", 24), Commits: []string{feature}, UserFacing: true, Type: "feature", Description: "A feature."})+"\n")
	repo.Write(releaseFilePath, releaseFile("minor", `"npm"`, ""))
	repo.Write(recordPath, runRecord(license))
	repo.Commit("prepare the release", unreleased, releaseFilePath, recordPath)
	repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	return repo
}

// validationAnswers are gh's answers to the questions validation asks about
// acme/portal.
func validationAnswers(visibility string) []testsupport.GHAnswer {
	return []testsupport.GHAnswer{
		{Args: []string{"--version"}, Stdout: "gh version 2.0.0\n"},
		{Args: []string{"auth", "status", "--hostname", "github.com"}},
		{Args: []string{"api", "--method", "GET", "repos/acme/portal"}, Stdout: fmt.Sprintf(`{"full_name":"acme/portal","visibility":%q,"private":%t,"archived":false,"permissions":{"push":true}}`, visibility, visibility != "public")},
	}
}

// releaseCreation are gh's answers to the release's GitHub Release of tag:
// none exists, and the creation succeeds.
func releaseCreation(tag string) []testsupport.GHAnswer {
	return []testsupport.GHAnswer{
		{Args: []string{"release", "view", tag, "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}, Stderr: "release not found\n", Exit: 1},
		{Args: []string{"release", "create", tag, "--repo", "acme/portal", "--title", tag, "--notes-file", "-", "--verify-tag"}},
	}
}

// ciRun are gh's answers about the CI run id on any commit: it concluded
// with conclusion, and a passing run's job test passed.
func ciRun(id int, conclusion string) []testsupport.GHAnswer {
	exit := 0
	if conclusion != "success" {
		exit = 1
	}
	run := fmt.Sprintf("repos/acme/portal/actions/runs/%d", id)
	// A failing run lists no job, so its failure log is empty, which reads
	// as a failure below the code: the run's failed jobs are run again once.
	jobs := `{"jobs":[]}`
	if conclusion == "success" {
		jobs = fmt.Sprintf(`{"jobs":[{"id":7,"name":"test","status":"completed","conclusion":"success","started_at":"2026-10-07T12:00:00Z","html_url":"https://github.com/acme/portal/actions/runs/%d/job/7"}]}`, id)
	}
	return []testsupport.GHAnswer{
		{Args: []string{"run", "list", "--repo", "acme/portal", "--commit", testsupport.GHAnyArg, "--limit", "100", "--json", "databaseId,name,workflowName,status,conclusion,headBranch,headSha,event"},
			Stdout: fmt.Sprintf(`[{"databaseId":%d,"name":"CI","workflowName":"CI","status":"completed","conclusion":%q,"headBranch":"main","headSha":"","event":"push"}]`, id, conclusion)},
		{Args: []string{"run", "watch", fmt.Sprint(id), "--repo", "acme/portal", "--exit-status"}, Exit: exit},
		{Args: []string{"run", "rerun", fmt.Sprint(id), "--repo", "acme/portal", "--failed"}},
		{Args: []string{"api", "--method", "GET", run}, Stdout: fmt.Sprintf(`{"status":"completed","conclusion":%q,"run_attempt":1}`, conclusion)},
		{Args: []string{"api", "--method", "GET", "--paginate", run + "/attempts/1/jobs?per_page=100"}, Stdout: jobs},
	}
}

func answers(groups ...[]testsupport.GHAnswer) []testsupport.GHAnswer {
	var out []testsupport.GHAnswer
	for _, g := range groups {
		out = append(out, g...)
	}
	return out
}

// releaseCommand runs `release run` (or `release resume`) in repo's root
// and returns what it printed and its error.
func releaseCommand(t *testing.T, repo *testsupport.Repo, resume, dryRun bool) (string, error) {
	t.Helper()
	var ferr error
	var out strings.Builder
	say := func(s string) { out.WriteString(s + "\n") }
	indexPath := filepath.Join(t.TempDir(), "confidential-names.toml")
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes(), HTTPClient: testsupport.NewFakeHTTP(t).Client()}, func(ctx *strictcli.Context) error {
		req := release.RunRequest{
			Dir: repo.Dir, LiveRoot: repo.Dir, DryRun: dryRun, RlsblVersion: "0.0.0-test",
			Checks: &passingChecks{}, IndexPath: indexPath,
			Now:   func() time.Time { return validationDay },
			Sleep: func(time.Duration) {},
			Log:   say, Warn: say,
		}
		if resume {
			ferr = release.Resume(ctx.Effects(), req)
		} else {
			ferr = release.Run(ctx.Effects(), req)
		}
		return ferr
	})
	return out.String(), ferr
}

// changes are the working tree's changes, the run state's own directory
// left out.
func changes(t *testing.T, dir string) string {
	t.Helper()
	var kept []string
	for _, line := range strings.Split(status(t, dir), "\n") {
		if line != "" && !strings.Contains(line, ".strictmetadata/.release-state/") {
			kept = append(kept, line)
		}
	}
	return strings.Join(kept, "\n")
}

func loadState(t *testing.T, repo *testsupport.Repo) (runstate.InProgress, bool) {
	t.Helper()
	s, found, err := runstate.LoadInProgress(repo.Dir, "portal")
	mustNotFail(t, err)
	return s, found
}

func remoteRef(t *testing.T, repo *testsupport.Repo, ref string) string {
	t.Helper()
	out, _, code := testsupport.RunGit(t, repo.Dir, "ls-remote", "origin", ref)
	if code != 0 {
		t.Fatalf("ls-remote origin %s failed", ref)
	}
	sha, _, _ := strings.Cut(strings.TrimSpace(out), "\t")
	return sha
}

func TestAReleaseRunsToItsGitHubReleaseAndConvertsThePendingIdentity(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t, answers(validationAnswers("public"), releaseCreation("v0.5.0"))...)
	repo := runRepo(t, "", "MIT", nil)
	out, err := releaseCommand(t, repo, false, false)
	if err != nil {
		t.Fatalf("the release failed: %v\n%s", err, out)
	}
	tag := strings.TrimSpace(repo.Git("rev-parse", "v0.5.0^{commit}"))
	if subject := repo.Git("log", "-1", "--format=%s", tag); subject != "v0.5.0" {
		t.Errorf("v0.5.0 tags %q, not the version-bump commit", subject)
	}
	if remoteRef(t, repo, "refs/tags/v0.5.0") != tag {
		t.Errorf("origin's v0.5.0 is not the release commit %s", tag)
	}
	if remoteRef(t, repo, "refs/heads/main") != repo.Head() {
		t.Errorf("origin's main is not the working tree's")
	}
	if !strings.Contains(read(t, repo.Path("package.json")), `"version": "0.5.0"`) {
		t.Errorf("the working tree's package.json was not bumped:\n%s", read(t, repo.Path("package.json")))
	}
	archive := read(t, repo.Path(".strictmetadata/releases/portal/v0.5.0.toml"))
	if !strings.Contains(archive, `release_commit = "`+tag+`"`) {
		t.Errorf("the archive does not record the release commit %s:\n%s", tag, archive)
	}
	if _, err := os.Stat(repo.Path(releaseFilePath)); !os.IsNotExist(err) {
		t.Errorf("the release file is still there")
	}
	if _, err := os.Stat(repo.Path(".strictmetadata/changelog/portal/0.5.0.jsonl")); err != nil {
		t.Errorf("the changelog was not finalized: %v", err)
	}
	// The pending identity of 0.5.0 is a dated period from the release
	// commit's committer date, and the identity it replaces closes then.
	day := repo.Git("log", "-1", "--format=%cs", tag)
	record := read(t, repo.Path(recordPath))
	if strings.Contains(record, "effective_version") || !strings.Contains(record, "until = "+day) || strings.Count(record, "from = "+day) != 1 {
		t.Errorf("the pending identity was not converted on %s:\n%s", day, record)
	}
	if _, found := loadState(t, repo); found {
		t.Errorf("the in-progress state was left behind")
	}
	if changes(t, repo.Dir) != "" {
		t.Errorf("the release left the working tree changed:\n%s", changes(t, repo.Dir))
	}
	var created *testsupport.GHCall
	for _, c := range gh.Calls() {
		if len(c.Args) > 1 && c.Args[0] == "release" && c.Args[1] == "create" {
			c := c
			created = &c
		}
	}
	if created == nil || !strings.Contains(created.Stdin, "<!-- rlsbl-ci-sha: "+tag+" -->") || !strings.Contains(created.Stdin, "A feature.") {
		t.Fatalf("the GitHub Release was not created with its notes and marker: %+v", created)
	}
	if !strings.Contains(out, "NOTICE: the publish outcome was not verified") {
		t.Errorf("--no-watch did not say the outcome was not verified:\n%s", out)
	}
}

func TestADryRunReleaseWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, validationAnswers("public")...)
	repo := runRepo(t, "", "MIT", nil)
	head := repo.Head()
	out, err := releaseCommand(t, repo, false, true)
	if err != nil {
		t.Fatalf("the preview failed: %v\n%s", err, out)
	}
	if repo.Head() != head || changes(t, repo.Dir) != "" || remoteRef(t, repo, "refs/heads/main") != head {
		t.Errorf("the preview wrote something:\n%s", changes(t, repo.Dir))
	}
	if _, found := loadState(t, repo); found {
		t.Errorf("the preview saved an in-progress state")
	}
	if !strings.Contains(out, "write-target-versions") || !strings.Contains(out, `Would commit`) {
		t.Errorf("the preview did not show the plan:\n%s", out)
	}
}

func TestAResumeAfterARedVerdictAdoptsTheRecordedFixAndTagsWhatCIVerified(t *testing.T) {
	hygiene.Isolate(t)
	fakeSelfdoc(t, filepath.Join(t.TempDir(), "selfdoc-log"))
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(42, "failure"))...)
	repo := runRepo(t, "", "MIT", map[string]string{".github/workflows/ci.yml": ciWorkflow, "selfdoc.json": "{}\n"})
	out, err := releaseCommand(t, repo, false, false)
	if err == nil || !strings.Contains(err.Error(), "CI did not pass on the release candidate of 0.5.0") {
		t.Fatalf("a red verdict did not stop the release: %v\n%s", err, out)
	}
	state, found := loadState(t, repo)
	if !found || !state.Completed(release.StepCandidatePushed) || state.FailedSteps[release.StepCIVerified] == "" {
		t.Fatalf("the stopped release's state: %+v", state)
	}
	// The selfdoc commit is the release's own, with the version-bump commit.
	if len(state.CreatedCommits) != 2 {
		t.Fatalf("the release's own commits: %v", state.CreatedCommits)
	}
	if subject := repo.Git("log", "-1", "--format=%s", state.CreatedCommits[0]); subject != release.SelfdocCommitMessage {
		t.Errorf("the first commit the release made is %q, not the selfdoc commit", subject)
	}
	if subject := repo.Git("log", "-1", "--format=%s", state.CreatedCommits[1]); subject != "v0.5.0" {
		t.Errorf("the second commit the release made is %q", subject)
	}
	if remoteRef(t, repo, "refs/heads/main") != state.ReleaseCommit || remoteRef(t, repo, "refs/tags/v0.5.0") != "" {
		t.Errorf("the candidate is not on origin untagged")
	}
	// The fix forward, not yet recorded: the resume refuses it, naming the
	// command that records it.
	fix := repo.CommitFile("fix.txt", "the fix\n", "fix the failure")
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(43, "success"), releaseCreation("v0.5.0"))...)
	out, err = releaseCommand(t, repo, true, false)
	if err == nil || !strings.Contains(err.Error(), "rlsbl changelog add --commits "+fix[:12]) {
		t.Fatalf("an unrecorded adopted commit was not refused: %v\n%s", err, out)
	}
	// The fix the refusal names: the commit recorded.
	addEntry(t, repo, "portal", fix)
	tip := repo.Head()
	out, err = releaseCommand(t, repo, true, false)
	if err != nil {
		t.Fatalf("the resume failed: %v\n%s", err, out)
	}
	if got := strings.TrimSpace(repo.Git("rev-parse", "v0.5.0^{commit}")); got != tip {
		t.Errorf("v0.5.0 tags %s, not the fixed tip CI verified (%s)", got, tip)
	}
	if remoteRef(t, repo, "refs/tags/v0.5.0") != tip {
		t.Errorf("origin's v0.5.0 is not the fixed tip")
	}
	if _, found := loadState(t, repo); found {
		t.Errorf("the in-progress state was left behind")
	}
	if !strings.Contains(read(t, repo.Path(".strictmetadata/changelog/portal/0.5.0.jsonl")), fix) {
		t.Errorf("the released changelog does not carry the fix's entry")
	}
}

// failingDeploy is a deploy command that fails until the file ready
// exists, appending its arguments to the file calls on every run; keys
// declare it on the releasable.
func failingDeploy(t *testing.T) (keys, calls, ready string) {
	t.Helper()
	dir := t.TempDir()
	calls = filepath.Join(dir, "calls")
	ready = filepath.Join(dir, "ready")
	deploy := filepath.Join(dir, "deploy")
	if err := os.WriteFile(deploy, []byte(fmt.Sprintf("#!/bin/sh\necho \"$*\" >> %s\n[ -f %s ] || { echo 'the host is down' >&2; exit 1; }\n", calls, ready)), 0o755); err != nil {
		t.Fatal(err)
	}
	return fmt.Sprintf("deploy_command = [%q, \"--version\", \"{version}\"]\n", deploy), calls, ready
}

func TestAFailingDeployLeavesAResumableStateAndResumeRunsItAgain(t *testing.T) {
	hygiene.Isolate(t)
	keys, calls, ready := failingDeploy(t)
	testsupport.FakeGH(t, answers(validationAnswers("private"), releaseCreation("v0.5.0"))...)
	repo := runRepo(t, keys, "proprietary", nil)
	out, err := releaseCommand(t, repo, false, false)
	if err == nil || !strings.Contains(err.Error(), "the deploy command") || !strings.Contains(err.Error(), "rlsbl release resume") {
		t.Fatalf("a failing deploy did not stop the release resumably: %v\n%s", err, out)
	}
	state, found := loadState(t, repo)
	if !found || !state.Completed(release.StepGitHubReleaseCreated) || state.FailedSteps[release.StepDeployed] == "" {
		t.Fatalf("the stopped release's state: %+v", state)
	}
	// The fix: the deploy target is up again, and the resume deploys.
	if err := os.WriteFile(ready, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	testsupport.FakeGH(t, validationAnswers("private")...)
	out, err = releaseCommand(t, repo, true, false)
	if err != nil {
		t.Fatalf("the resume failed: %v\n%s", err, out)
	}
	if got := read(t, calls); got != "--version 0.5.0\n--version 0.5.0\n" {
		t.Errorf("the deploy ran:\n%s", got)
	}
	if _, found := loadState(t, repo); found {
		t.Errorf("the in-progress state was left behind")
	}
}

func TestAResumeWithNothingNewAsksCIAgainOnTheSameCandidate(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(42, "failure"))...)
	repo := runRepo(t, "", "MIT", map[string]string{".github/workflows/ci.yml": ciWorkflow})
	if out, err := releaseCommand(t, repo, false, false); err == nil {
		t.Fatalf("the red verdict did not stop the release:\n%s", out)
	}
	// Resume with nothing new: CI is asked again on the same candidate, and
	// still fails; the state stays.
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(43, "failure"))...)
	out, err := releaseCommand(t, repo, true, false)
	if err == nil || !strings.Contains(err.Error(), "CI did not pass") {
		t.Fatalf("the second red verdict did not stop the resume: %v\n%s", err, out)
	}
	if _, found := loadState(t, repo); !found {
		t.Fatalf("the state of the stopped release is gone")
	}
}

func TestResumeWithNothingInProgressIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, validationAnswers("public")...)
	repo := runRepo(t, "", "MIT", nil)
	_, err := releaseCommand(t, repo, true, false)
	if err == nil || !strings.Contains(err.Error(), "no release of portal is in progress") || !strings.Contains(err.Error(), "`rlsbl release run --watch` starts a release") {
		t.Fatalf("a resume with nothing in progress was not refused: %v", err)
	}
}

// foreignOriginCommit puts a commit on origin's main, on top of what origin
// holds, that the release's repository never saw: someone else's push.
func foreignOriginCommit(t *testing.T, repo *testsupport.Repo) string {
	t.Helper()
	parent := remoteRef(t, repo, "refs/heads/main")
	tree := strings.TrimSpace(repo.Git("rev-parse", parent+"^{tree}"))
	foreign := strings.TrimSpace(repo.Git("commit-tree", tree, "-p", parent, "-m", "someone else's commit"))
	repo.Git("push", "-q", "origin", foreign+":refs/heads/main")
	return foreign
}

func TestAReleasePushNeverOverwritesACommitOriginGainedDuringTheRelease(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(42, "failure"))...)
	repo := runRepo(t, "", "MIT", map[string]string{".github/workflows/ci.yml": ciWorkflow})
	if out, err := releaseCommand(t, repo, false, false); err == nil {
		t.Fatalf("the red verdict did not stop the release:\n%s", out)
	}
	foreign := foreignOriginCommit(t, repo)
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(43, "success"), releaseCreation("v0.5.0"))...)
	out, err := releaseCommand(t, repo, true, false)
	if got := remoteRef(t, repo, "refs/heads/main"); got != foreign {
		t.Fatalf("the release overwrote origin's main (%s, was the foreign commit %s): %v\n%s", got, foreign, err, out)
	}
	if err == nil {
		t.Fatalf("the resume pushed past a commit origin gained:\n%s", out)
	}
	requireContains(t, err.Error(), "only fast-forwards")
}

func TestAResumeAfterTheChangelogIsFinalizedAdoptsNothing(t *testing.T) {
	hygiene.Isolate(t)
	keys, _, ready := failingDeploy(t)
	testsupport.FakeGH(t, answers(validationAnswers("private"), releaseCreation("v0.5.0"))...)
	repo := runRepo(t, keys, "proprietary", nil)
	if out, err := releaseCommand(t, repo, false, false); err == nil {
		t.Fatalf("the failing deploy did not stop the release:\n%s", out)
	}
	released := repo.Head()
	late := repo.CommitFile("late.txt", "late\n", "a commit after the release stopped")
	addEntry(t, repo, "portal", late)
	if err := os.WriteFile(ready, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	testsupport.FakeGH(t, validationAnswers("private")...)
	out, err := releaseCommand(t, repo, true, false)
	if err == nil {
		t.Fatalf("a resume past the finalized changelog adopted commits:\n%s", out)
	}
	requireContains(t, err.Error(), "changelog-finalized", late[:12], "move them off the release branch")
	// The fix the refusal names: the commits moved off the release branch.
	repo.Git("reset", "-q", "--hard", released)
	out, err = releaseCommand(t, repo, true, false)
	if err != nil {
		t.Fatalf("the resume failed once the commits were moved off: %v\n%s", err, out)
	}
}
