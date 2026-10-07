package release_test

import (
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func initRelease(t *testing.T, repo *testsupport.Repo, dir string) (release.InitResult, error) {
	t.Helper()
	var res release.InitResult
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		var err error
		res, err = release.Init(e, repo.Dir, dir)
		return err
	})
	return res, err
}

func TestReleaseInitWritesABlankReleaseFileWithEveryTargetAndCommitsIt(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := standalone(t, "none")
	repo.CommitFile("pyproject.toml", "[project]\nname = \"portal\"\nversion = \"0.4.0\"\n", "a python package too")
	res, err := initRelease(t, repo, repo.Dir)
	mustNotFail(t, err)
	if !res.Written || res.Path != releaseFilePath {
		t.Fatalf("init: %+v", res)
	}
	text := read(t, repo.Path(releaseFilePath))
	if !strings.Contains(text, `bump = ""`) || !strings.Contains(text, `description = ""`) || !strings.Contains(text, "format_version = 2") {
		t.Errorf("the release file:\n%s", text)
	}
	if !strings.Contains(text, `include = ["npm", "pypi"]`) && !strings.Contains(text, `include = ["pypi", "npm"]`) {
		t.Errorf("the release file does not include every target:\n%s", text)
	}
	if !releaserecord.IsPristineReleaseFile([]byte(text)) {
		t.Errorf("the scaffolded release file is not pristine")
	}
	if subject := repo.Git("log", "-1", "--format=%s"); subject != release.InitCommitMessage || changes(t, repo.Dir) != "" {
		t.Errorf("the release file was not committed: %q\n%s", subject, changes(t, repo.Dir))
	}
	// A release refuses it until bump and description are filled in.
	if _, err := releaserecord.ReadReleaseFile(repo.Dir, "portal"); err == nil {
		t.Errorf("a blank release file was accepted")
	}
	// Running init again on the blank file does nothing.
	head := repo.Head()
	res, err = initRelease(t, repo, repo.Dir)
	mustNotFail(t, err)
	if res.Written || repo.Head() != head {
		t.Errorf("init rewrote a blank release file: %+v", res)
	}
}

func TestReleaseInitRefusesAFilledInReleaseFileUntilItIsDeleted(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := standalone(t, "none")
	repo.CommitFile(releaseFilePath, releaseFile("minor", `"npm"`, ""), "the next release")
	_, err := initRelease(t, repo, repo.Dir)
	if err == nil || !strings.Contains(err.Error(), "somebody filled it in") || !strings.Contains(err.Error(), "saferm delete") {
		t.Fatalf("a filled-in release file was overwritten: %v", err)
	}
	if !strings.Contains(read(t, repo.Path(releaseFilePath)), `bump = "minor"`) {
		t.Fatalf("the filled-in release file changed")
	}
	// The fix the refusal names: the file deleted.
	if err := os.Remove(repo.Path(releaseFilePath)); err != nil {
		t.Fatal(err)
	}
	res, err := initRelease(t, repo, repo.Dir)
	mustNotFail(t, err)
	if !res.Written {
		t.Errorf("init did not write the release file again")
	}
}

func TestReleaseInitOfAMemberVersionedUnderNoneIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := workspaceRepo(t)
	_, err := initRelease(t, repo, repo.Dir)
	if err == nil || !strings.Contains(err.Error(), "versioned under no releasable") || !strings.Contains(err.Error(), "rlsbl monorepo release init") {
		t.Fatalf("init at a dev-node root was not refused: %v", err)
	}
	// From a member of a releasable, the releasable's file is written.
	res, err := initRelease(t, repo, repo.Path("widget"))
	mustNotFail(t, err)
	if res.Path != ".strictmetadata/releases/widget/unreleased.toml" {
		t.Errorf("init wrote %s", res.Path)
	}
}

func TestAReleaseFileThatCannotBeCommittedNamesTheCommitThatFinishesIt(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "none")
	testsupport.PathOnly(t, "git")
	_, err := initRelease(t, repo, repo.Dir)
	if err == nil || !strings.Contains(err.Error(), "safegit commit -m") {
		t.Fatalf("a failed commit did not name the commit that finishes it: %v", err)
	}
	// The fix the error names: the commit, run as written.
	testsupport.FakeSafegit(t)
	cmd := exec.Command("safegit", "commit", "-m", release.InitCommitMessage, "--", releaseFilePath, ".strictmetadata/releases/manifest.toml")
	cmd.Dir = repo.Dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("the named commit failed: %v\n%s", err, out)
	}
	if changes(t, repo.Dir) != "" {
		t.Fatalf("the named commit left changes:\n%s", changes(t, repo.Dir))
	}
	res, err := initRelease(t, repo, repo.Dir)
	mustNotFail(t, err)
	if res.Written {
		t.Errorf("init wrote the committed blank file again")
	}
}
