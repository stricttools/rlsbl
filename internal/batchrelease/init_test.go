package batchrelease_test

import (
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/batchrelease"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func initBatch(t *testing.T, repo *testsupport.Repo, names []string) (batchrelease.InitResult, error) {
	t.Helper()
	var res batchrelease.InitResult
	var ferr error
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(ctx *strictcli.Context) error {
		res, ferr = batchrelease.Init(ctx.Effects(), repo.Dir, names, release.Fork{})
		return ferr
	})
	return res, ferr
}

// withWidgetFeature commits a feature in widget, described by a user-facing
// entry.
func withWidgetFeature(t *testing.T, repo *testsupport.Repo) {
	t.Helper()
	feature := repo.CommitFile("widget/feature.txt", "a feature\n", "add a widget feature")
	addEntry(t, repo, "widget", true, feature)
}

func TestInitWritesATablePerReleasableWithUnreleasedCommitsAndCommitsIt(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := workspaceRepo(t, nil)
	withWidgetFeature(t, repo)
	res, err := initBatch(t, repo, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Written || res.Path != batchFilePath || strings.Join(res.Releasables, ",") != "widget" || strings.Join(res.Idle, ",") != "gadget" {
		t.Fatalf("init: %+v", res)
	}
	text := read(t, repo.Path(batchFilePath))
	requireContains(t, text, "format_version = 2", "[releasables.widget]", `bump = ""`, `description = ""`, `include = ["npm"]`, "# [releasables.gadget]", "no commit needing a changelog entry since 0.2.0")
	if !releaserecord.IsPristineBatchReleaseFile([]byte(text)) {
		t.Errorf("the scaffolded batch release file is not pristine:\n%s", text)
	}
	if subject := repo.Git("log", "-1", "--format=%s"); subject != batchrelease.InitCommitMessage || changes(t, repo) != "" {
		t.Errorf("the batch release file was not committed: %q\n%s", subject, changes(t, repo))
	}
	// A batch release refuses it until bump and description are filled in.
	if _, err := releaserecord.ReadBatchReleaseFile(repo.Dir); err == nil {
		t.Errorf("a blank batch release file was accepted")
	}
	// Running init again on the blank file does nothing.
	head := repo.Head()
	res, err = initBatch(t, repo, nil)
	if err != nil || res.Written || repo.Head() != head {
		t.Errorf("init rewrote a blank batch release file: %+v %v", res, err)
	}
}

func TestInitRefusesAFilledInBatchReleaseFileUntilItIsDeleted(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := workspaceRepo(t, nil)
	withWidgetFeature(t, repo)
	repo.CommitFile(batchFilePath, batchFile, "the next batch")
	_, err := initBatch(t, repo, nil)
	if err == nil || !strings.Contains(err.Error(), "somebody filled it in") || !strings.Contains(err.Error(), "saferm delete") {
		t.Fatalf("a filled-in batch release file was overwritten: %v", err)
	}
	if read(t, repo.Path(batchFilePath)) != batchFile {
		t.Fatalf("the filled-in batch release file changed")
	}
	// The fix the refusal names: the file deleted.
	if err := os.Remove(repo.Path(batchFilePath)); err != nil {
		t.Fatal(err)
	}
	res, err := initBatch(t, repo, nil)
	if err != nil || !res.Written {
		t.Fatalf("init did not write the batch release file again: %+v %v", res, err)
	}
}

func TestInitOfAnUndeclaredReleasableIsRefusedNamingTheDeclaredOnes(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := workspaceRepo(t, nil)
	withWidgetFeature(t, repo)
	_, err := initBatch(t, repo, []string{"portal"})
	if err == nil || !strings.Contains(err.Error(), "--releasables names portal") || !strings.Contains(err.Error(), "the declared releasables are widget, gadget") {
		t.Fatalf("an undeclared releasable was not refused: %v", err)
	}
	// The fix: a declared releasable named.
	res, err := initBatch(t, repo, []string{"widget"})
	if err != nil || !res.Written || strings.Join(res.Releasables, ",") != "widget" || len(res.Idle) != 0 {
		t.Fatalf("init: %+v %v", res, err)
	}
	if strings.Contains(read(t, repo.Path(batchFilePath)), "gadget") {
		t.Errorf("init wrote a table for gadget, which --releasables left out")
	}
}

func TestInitWithNothingToReleaseIsRefusedUntilSomethingIsCommitted(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := workspaceRepo(t, nil)
	_, err := initBatch(t, repo, nil)
	if err == nil || !strings.Contains(err.Error(), "since the latest release of widget, gadget") || !strings.Contains(err.Error(), "commit the next changes first") {
		t.Fatalf("a batch with nothing to release was written: %v", err)
	}
	if exists(t, repo.Path(batchFilePath)) {
		t.Fatalf("the refused init wrote the batch release file")
	}
	// The fix the refusal names: the next change committed.
	withWidgetFeature(t, repo)
	res, err := initBatch(t, repo, nil)
	if err != nil || !res.Written || strings.Join(res.Releasables, ",") != "widget" {
		t.Fatalf("init: %+v %v", res, err)
	}
}

func TestInitOfAReleasableWithNoTargetIsRefusedUntilItIsLeftOut(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := workspaceRepo(t, map[string]string{"portal/README.md": "portal\n"})
	repo.CommitFile(declarationsPath, batchDeclarations+"\n[[releasables]]\nname = \"portal\"\ntag_format = \"{name}@v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \"portal\"\nname = \"portal\"\nreleasable = \"portal\"\n", "declare portal")
	withWidgetFeature(t, repo)
	_, err := initBatch(t, repo, nil)
	if err == nil || !strings.Contains(err.Error(), "no target (go, npm, or pypi) is declared or detected in any member of portal") || !strings.Contains(err.Error(), "--releasables") {
		t.Fatalf("a releasable with no target was not refused: %v", err)
	}
	// The fix the refusal names: portal left out with --releasables.
	res, err := initBatch(t, repo, []string{"widget", "gadget"})
	if err != nil || !res.Written {
		t.Fatalf("init: %+v %v", res, err)
	}
}

func TestABatchReleaseFileThatCannotBeCommittedNamesTheCommitThatFinishesIt(t *testing.T) {
	hygiene.Isolate(t)
	repo := workspaceRepo(t, nil)
	withWidgetFeature(t, repo)
	testsupport.PathOnly(t, "git")
	_, err := initBatch(t, repo, nil)
	if err == nil || !strings.Contains(err.Error(), "safegit commit -m") {
		t.Fatalf("a failed commit did not name the commit that finishes it: %v", err)
	}
	// The fix the error names: the commit, run as written.
	testsupport.FakeSafegit(t)
	cmd := exec.Command("safegit", "commit", "-m", batchrelease.InitCommitMessage, "--", batchFilePath, ".strictmetadata/batch-releases/manifest.toml")
	cmd.Dir = repo.Dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("the named commit failed: %v\n%s", err, out)
	}
	if c := changes(t, repo); c != "" {
		t.Fatalf("the named commit left changes:\n%s", c)
	}
	res, err := initBatch(t, repo, nil)
	if err != nil || res.Written {
		t.Errorf("init wrote the committed blank file again: %+v %v", res, err)
	}
}

func TestInitInAStandaloneProjectIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile(declarationsPath, "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n", "the project")
	_, err := initBatch(t, repo, nil)
	if err == nil || !strings.Contains(err.Error(), "`rlsbl release init`") {
		t.Fatalf("a standalone project's batch init was not refused: %v", err)
	}
}
