package monorepo

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// emptyRepository is a repository with one commit and no declarations.
func emptyRepository(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.CommitFile("README.md", "portal\n", "the first commit")
	hygiene.Chdir(t, repo.Dir)
	return repo
}

func initWith(repo *testsupport.Repo, r *declarations.Releasable, autoCommit bool) func(e *strictcli.Effects, say func(string)) error {
	return func(e *strictcli.Effects, say func(string)) error {
		return Init(e, InitRequest{Root: repo.Dir, ReleaseBranches: []string{"main"}, RootReleasable: r, AutoCommit: autoCommit, Say: say})
	}
}

func TestInitDeclaresADevNodeRootAndCommits(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := emptyRepository(t)
	mustRun(t, strictcli.EffectMutating, false, initWith(repo, nil, true))
	d, err := declarations.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	root := d.RootMember()
	if !d.IsWorkspace() || len(d.Members) != 1 || !root.DevNode() || len(d.Releasables) != 0 {
		t.Fatalf("declarations: %+v", d)
	}
	if subject := repo.Git("log", "-1", "--format=%s"); subject != InitCommitMessage {
		t.Errorf("the last commit is %q", subject)
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("the init left changes uncommitted:\n%s", status)
	}
}

func TestInitWithoutAutoCommitWritesAndCommitsNothing(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := emptyRepository(t)
	text := mustRun(t, strictcli.EffectMutating, false, initWith(repo, nil, false))
	if !strings.Contains(text, "Not committed (--no-auto-commit)") || commitCount(repo) != "1" {
		t.Fatalf("commits %s:\n%s", commitCount(repo), text)
	}
	if !exists(repo, declarations.ReleasablesFile) {
		t.Fatal("nothing was written")
	}
}

func TestInitVersionsTheRootUnderTheReleasableItCreates(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := emptyRepository(t)
	r := &declarations.Releasable{Name: "portal", TagFormat: "v{version}", PublishMode: declarations.PublishNone}
	mustRun(t, strictcli.EffectMutating, false, initWith(repo, r, true))
	d, err := declarations.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	got, ok := d.Releasable("portal")
	if !ok || got.TagFormat != "v{version}" || d.RootMember().Releasable != "portal" || d.RootMember().DevOnly {
		t.Fatalf("declarations: %+v", d)
	}
}

// A root releasable publishing from CI must name the pattern of its CI's
// check runs; naming it clears the refusal.
func TestInitRequiresTheCheckPatternOfARootPublishingFromCI(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := emptyRepository(t)
	r := &declarations.Releasable{Name: "portal", TagFormat: "v{version}", PublishMode: declarations.PublishCI}
	mustFail(t, strictcli.EffectMutating, false, initWith(repo, r, true), "publish_ci_check_pattern")
	if exists(repo, declarations.ReleasablesFile) {
		t.Fatal("a refused init wrote the declarations")
	}
	r.PublishCICheckPattern = "^(test|lint)$"
	mustRun(t, strictcli.EffectMutating, false, initWith(repo, r, true))
}

func TestInitRefusesAPatternThatIsNoRegularExpression(t *testing.T) {
	hygiene.Isolate(t)
	repo := emptyRepository(t)
	r := &declarations.Releasable{Name: "portal", TagFormat: "v{version}", PublishMode: declarations.PublishCI, PublishCICheckPattern: "^(test"}
	mustFail(t, strictcli.EffectMutating, false, initWith(repo, r, true), "--publish-ci-check-pattern", "not a regular expression")
}

func TestInitRefusesARepositoryThatDeclaresItsLayout(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := emptyRepository(t)
	mustRun(t, strictcli.EffectMutating, false, initWith(repo, nil, true))
	before := readText(t, repo, declarations.ReleasablesFile)
	mustFail(t, strictcli.EffectMutating, false, initWith(repo, nil, true), declarations.ReleasablesFile+" already exists")
	if readText(t, repo, declarations.ReleasablesFile) != before {
		t.Error("the refused init changed the declarations")
	}
}

func TestInitRefusesATagFormatWithoutItsVersion(t *testing.T) {
	hygiene.Isolate(t)
	repo := emptyRepository(t)
	r := &declarations.Releasable{Name: "portal", TagFormat: "release", PublishMode: declarations.PublishNone}
	mustFail(t, strictcli.EffectMutating, false, initWith(repo, r, true), "{version}")
}

// A commit that fails takes back what the init created, so running it again
// after fixing what the commit reported starts afresh and succeeds.
func TestAFailedInitCommitTakesTheDeclarationsBack(t *testing.T) {
	hygiene.Isolate(t)
	repo := emptyRepository(t)
	testsupport.PathOnly(t, "git")
	deletingSaferm(t)
	mustFail(t, strictcli.EffectMutating, false, initWith(repo, nil, true), "safegit is not on PATH", "the workspace is not initialized", "run `rlsbl monorepo init` again")
	if exists(repo, declarations.ReleasablesFile) || exists(repo, declarations.ReleasablesDir+"/manifest.toml") {
		t.Fatal("the failed init left its files behind")
	}
	testsupport.FakeSafegit(t)
	mustRun(t, strictcli.EffectMutating, false, initWith(repo, nil, true))
	if subject := repo.Git("log", "-1", "--format=%s"); subject != InitCommitMessage {
		t.Errorf("the last commit is %q", subject)
	}
}
