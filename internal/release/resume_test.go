package release_test

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/releaseops"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// writeExecutable writes an executable script at path.
func writeExecutable(t *testing.T, path, script string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
}

// liveCommitScript is shell that puts an empty commit with message onto
// the main branch of the repository at live, once: marker records that it
// did. The variables git sets for a hook are dropped, so the commit is made
// in live whatever runs it.
func liveCommitScript(t *testing.T, live, message string) string {
	t.Helper()
	marker := filepath.Join(t.TempDir(), "committed")
	return fmt.Sprintf("[ -f %[1]s ] || { touch %[1]s && env -u GIT_DIR -u GIT_QUARANTINE_PATH -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES git -C %[2]s commit -q --allow-empty -m %[3]s; }", marker, live, message)
}

// bareOrigin is the directory of repo's origin.
func bareOrigin(t *testing.T, repo *testsupport.Repo) string {
	t.Helper()
	return strings.TrimPrefix(strings.TrimSpace(repo.Git("remote", "get-url", "origin")), "file://")
}

// commitBySubject is the commit on main whose subject is subject.
func commitBySubject(t *testing.T, repo *testsupport.Repo, subject string) string {
	t.Helper()
	sha := strings.TrimSpace(repo.Git("log", "-1", "--format=%H", "--grep=^"+subject+"$", "main"))
	if sha == "" {
		t.Fatalf("no commit on main has the subject %q", subject)
	}
	return sha
}

func TestAForeignCommitAtTheMutatingEntryIsRefusedUntilRecorded(t *testing.T) {
	hygiene.Isolate(t)
	livePath := filepath.Join(t.TempDir(), "live")
	marker := filepath.Join(t.TempDir(), "committed")
	hook := fmt.Sprintf(`[ -f %[1]s ] || { touch %[1]s && git -C "$(cat %[2]s)" commit -q --allow-empty -m foreign-at-entry; }`, marker, livePath)
	testsupport.FakeGH(t, answers(validationAnswers("public"), releaseCreation("v0.5.0"))...)
	repo := runRepo(t, fmt.Sprintf("hooks = { pre_release = [%q] }\n", hook), "MIT", nil)
	testsupport.WriteFile(t, livePath, repo.Dir)
	out, err := releaseCommand(t, repo, false, false)
	if err == nil {
		t.Fatalf("a commit made after the pin was not refused:\n%s", out)
	}
	requireContains(t, err.Error(), "the mutating entry", "foreign-at-entry", "rlsbl changelog add", "rlsbl release run --watch")
	if remoteRef(t, repo, "refs/tags/v0.5.0") != "" {
		t.Fatal("the refused release tagged")
	}
	// The fix the refusal names: the commit recorded, and the release run
	// again.
	addEntry(t, repo, "portal", commitBySubject(t, repo, "foreign-at-entry"))
	out, err = releaseCommand(t, repo, false, false)
	if err != nil {
		t.Fatalf("the release failed once the commit was recorded: %v\n%s", err, out)
	}
}

func TestAForeignCommitDuringTheCIWaitIsRefusedAtTheCIGateUntilRecorded(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(42, "success"))...)
	repo := runRepo(t, "", "MIT", map[string]string{".github/workflows/ci.yml": ciWorkflow})
	// gh, while it watches the CI run, lets a commit land on main.
	wrap := func() {
		fake, err := exec.LookPath("gh")
		if err != nil {
			t.Fatal(err)
		}
		dir := t.TempDir()
		writeExecutable(t, filepath.Join(dir, "gh"), fmt.Sprintf("#!/bin/sh\nif [ \"$1\" = run ] && [ \"$2\" = watch ]; then %s; fi\nexec %s \"$@\"\n", liveCommitScript(t, repo.Dir, "foreign-during-ci"), fake))
		t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	}
	wrap()
	out, err := releaseCommand(t, repo, false, false)
	if err == nil {
		t.Fatalf("a commit made during the CI wait was not refused:\n%s", out)
	}
	requireContains(t, err.Error(), "the CI gate", "foreign-during-ci", "rlsbl changelog add", "rlsbl release resume --watch")
	if remoteRef(t, repo, "refs/tags/v0.5.0") != "" {
		t.Fatal("the refused release tagged")
	}
	// The fix the refusal names: the commit recorded, and the release
	// resumed, which adopts it and asks CI again.
	addEntry(t, repo, "portal", commitBySubject(t, repo, "foreign-during-ci"))
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(43, "success"), releaseCreation("v0.5.0"))...)
	out, err = releaseCommand(t, repo, true, false)
	if err != nil {
		t.Fatalf("the resume failed once the commit was recorded: %v\n%s", err, out)
	}
}

func TestTheForeignCommitGuardNamesItsCheckpointAndTheCommits(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	pin := repo.CommitFile("a.txt", "a\n", "the pin")
	ours := repo.CommitFile("b.txt", "b\n", "the release commit")
	err := run(t, nil, func(e *strictcli.Effects) error {
		live, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		if err := release.GuardForeignCommits(live, "refs/heads/main", pin, []string{ours}, release.CheckpointFinalPush, release.RerunResume); err != nil {
			return fmt.Errorf("the release's own commit was refused: %w", err)
		}
		theirs := repo.CommitFile("c.txt", "c\n", "somebody else's work")
		for _, checkpoint := range []string{release.CheckpointEntry, release.CheckpointCandidatePush, release.CheckpointCIGate, release.CheckpointFinalPush} {
			err := release.GuardForeignCommits(live, "refs/heads/main", pin, []string{ours}, checkpoint, release.RerunResume)
			var foreign *release.ForeignCommitError
			if !errors.As(err, &foreign) {
				return fmt.Errorf("at the %s, a commit the release did not make was not refused: %v", checkpoint, err)
			}
			for _, want := range []string{"the " + checkpoint, theirs[:12], "somebody else's work", "rlsbl changelog add", "rlsbl release resume --watch", "move them off the release branch"} {
				if !strings.Contains(err.Error(), want) {
					return fmt.Errorf("at the %s the refusal lacks %q:\n%v", checkpoint, want, err)
				}
			}
			if strings.Contains(err.Error(), "the release commit") {
				return fmt.Errorf("the release's own commit was named:\n%v", err)
			}
		}
		return nil
	})
	mustNotFail(t, err)
}

// stoppedRelease is portal's release of 0.5.0 stopped by a red CI verdict,
// its candidate on origin and its state kept.
func stoppedRelease(t *testing.T) *testsupport.Repo {
	t.Helper()
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(42, "failure"))...)
	repo := runRepo(t, "", "MIT", map[string]string{".github/workflows/ci.yml": ciWorkflow})
	if out, err := releaseCommand(t, repo, false, false); err == nil {
		t.Fatalf("the red verdict did not stop the release:\n%s", out)
	}
	if _, found := loadState(t, repo); !found {
		t.Fatal("the stopped release kept no state")
	}
	return repo
}

func TestAResumeOfAStateRecordingUnknownStepsIsRefusedUntilAbandoned(t *testing.T) {
	hygiene.Isolate(t)
	repo := stoppedRelease(t)
	state, _ := loadState(t, repo)
	state.CompletedSteps = append(state.CompletedSteps, "a-later-step")
	testsupport.WriteFile(t, repo.Path(runstate.InProgressPath("portal")), string(state.Render()))
	_, err := releaseCommand(t, repo, true, false)
	if err == nil {
		t.Fatal("a state naming a step this rlsbl lacks was resumed")
	}
	requireContains(t, err.Error(), "records steps this rlsbl does not have (a-later-step)", "rlsbl release abandon --approve-consequential")
	// The way out the refusal names: the attempt given up, its version
	// recorded never released and its state gone.
	testsupport.FakeSafegit(t)
	testsupport.FakeGH(t, testsupport.GHAnswer{Args: []string{"auth", "status", "--hostname", "github.com"}},
		testsupport.GHAnswer{Args: []string{"release", "view", "v0.5.0", "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}, Stderr: "release not found\n", Exit: 1})
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		err := releaseops.Abandon(ctx, releaseops.AbandonRequest{Dir: repo.Dir})
		mustNotFail(t, err)
		return err
	})
	if _, found := loadState(t, repo); found {
		t.Fatal("the abandon left the state")
	}
	requireContains(t, read(t, repo.Path(".strictmetadata/releases/portal/v0.5.0.toml")), "never_released = true")
}

func TestAResumeOnAnotherBranchIsRefusedUntilTheReleaseBranchIsCheckedOut(t *testing.T) {
	hygiene.Isolate(t)
	repo := stoppedRelease(t)
	repo.Git("checkout", "-q", "-b", "elsewhere")
	_, err := releaseCommand(t, repo, true, false)
	if err == nil {
		t.Fatal("a resume on another branch was not refused")
	}
	requireContains(t, err.Error(), "runs on main, and the working tree is on elsewhere", "check out main")
	// The fix the refusal names.
	repo.Git("checkout", "-q", "main")
	testsupport.FakeGH(t, answers(validationAnswers("public"), ciRun(43, "success"), releaseCreation("v0.5.0"))...)
	out, err := releaseCommand(t, repo, true, false)
	if err != nil {
		t.Fatalf("the resume failed on main: %v\n%s", err, out)
	}
}

// previewResumeIn previews the resume of portal through ResumeIn, which
// takes the releasable from its caller (a batch release's second pass), and
// returns its error.
func previewResumeIn(t *testing.T, repo *testsupport.Repo) error {
	t.Helper()
	var ferr error
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: true, Allowlist: previewapply.Prefixes(), HTTPClient: testsupport.NewFakeHTTP(t).Client()}, func(ctx *strictcli.Context) error {
		e := ctx.Effects()
		live, err := git.Open(e, repo.Dir)
		if err != nil {
			ferr = err
			return err
		}
		s, err := release.Enter(e, live, nil, release.EnterOptions{DryRun: true, What: "The resume", Rerun: release.RerunResume})
		if err != nil {
			ferr = err
			return err
		}
		say := func(string) {}
		ferr = release.ResumeIn(e, s, release.RunRequest{
			Dir: repo.Dir, LiveRoot: repo.Dir, DryRun: true, RlsblVersion: "0.0.0-test",
			Checks: &passingChecks{}, IndexPath: filepath.Join(t.TempDir(), "confidential-names.toml"),
			Now: func() time.Time { return validationDay }, Sleep: func(time.Duration) {}, Log: say, Warn: say,
		}, "portal")
		return ferr
	})
	return ferr
}

func TestAResumeOfAReleasableNoLongerDeclaredIsRefusedUntilDeclaredAgain(t *testing.T) {
	hygiene.Isolate(t)
	repo := stoppedRelease(t)
	declared := read(t, repo.Path(declarationsPath))
	renamed := strings.Replace(strings.Replace(declared, `name = "portal"`, `name = "portal-two"`, 1), `releasable = "portal"`, `releasable = "portal-two"`, 1)
	testsupport.WriteFile(t, repo.Path(declarationsPath), renamed)
	err := previewResumeIn(t, repo)
	if err == nil || !strings.Contains(err.Error(), `which .strictmetadata/releasables/releasables.toml no longer declares; declare it again to resume`) {
		t.Fatalf("an undeclared releasable was resumed: %v", err)
	}
	// The fix the refusal names: declared again.
	testsupport.WriteFile(t, repo.Path(declarationsPath), declared)
	testsupport.FakeGH(t, validationAnswers("public")...)
	if err := previewResumeIn(t, repo); err != nil {
		t.Fatalf("the releasable declared again: %v", err)
	}
}

func TestAResumeThroughAMemberNoLongerTheReleasablesIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo := stoppedRelease(t)
	state, _ := loadState(t, repo)
	path := repo.Path(runstate.InProgressPath("portal"))
	kept := read(t, path)
	state.RepresentativeMember = "core"
	testsupport.WriteFile(t, path, string(state.Render()))
	err := previewResumeIn(t, repo)
	if err == nil || !strings.Contains(err.Error(), `through the member "core", which is no longer a member of it; declare it again to resume`) {
		t.Fatalf("a resume through a member the releasable lacks was not refused: %v", err)
	}
	testsupport.WriteFile(t, path, kept)
	testsupport.FakeGH(t, validationAnswers("public")...)
	if err := previewResumeIn(t, repo); err != nil {
		t.Fatalf("the resume through its member: %v", err)
	}
}
func TestACandidatePushThatTimesOutStopsNamingALongerTimeout(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, answers(validationAnswers("public"), releaseCreation("v0.5.0"))...)
	repo := runRepo(t, "\n[timeouts]\npush_seconds = 1\n", "MIT", nil)
	writeExecutable(t, filepath.Join(bareOrigin(t, repo), "hooks", "pre-receive"), "#!/bin/sh\nsleep 2\n")
	out, err := releaseCommand(t, repo, false, false)
	if err == nil {
		t.Fatalf("a push slower than its timeout did not stop the release:\n%s", out)
	}
	requireContains(t, err.Error(), "timed out", "whether it reached origin is unknown", "rlsbl release resume --watch --push-timeout 900")
	if _, found := loadState(t, repo); !found {
		t.Fatal("the state of a release whose push may have reached origin was not kept")
	}
	// The fix the refusal names: the resume with a longer push timeout.
	out, err = releaseCommandWith(t, repo, true, false, func(r *release.RunRequest) {
		r.Timeouts.Push, r.Timeouts.PushGiven = 900, true
	})
	if err != nil {
		t.Fatalf("the resume with a longer push timeout failed: %v\n%s", err, out)
	}
}

// A push origin refuses with a message that merely reads like a timeout is
// not a push that timed out: the attempt is discarded, as for any push that
// did not reach origin, and no longer push timeout is offered.
func TestAPushRefusalReadingLikeATimeoutIsNotATimeout(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, validationAnswers("public")...)
	repo := runRepo(t, "", "MIT", nil)
	writeExecutable(t, filepath.Join(bareOrigin(t, repo), "hooks", "pre-receive"), "#!/bin/sh\necho 'policy check timed out: try later' >&2\nexit 1\n")
	out, err := releaseCommand(t, repo, false, false)
	if err == nil {
		t.Fatalf("a push origin refused did not stop the release:\n%s", out)
	}
	if strings.Contains(err.Error(), "--push-timeout") || !strings.Contains(err.Error(), "nothing reached origin") {
		t.Fatalf("a refused push was taken for a timed-out one: %v", err)
	}
	if _, found := loadState(t, repo); found {
		t.Fatal("the state of a release whose push was refused was kept")
	}
}

func TestADiscardThatCannotTakeTheAdvanceBackKeepsTheStateNamingAbandon(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, answers(validationAnswers("public"), releaseCreation("v0.5.0"))...)
	repo := runRepo(t, "", "MIT", nil)
	// origin refuses the candidate, and a commit lands on main meanwhile.
	hook := filepath.Join(bareOrigin(t, repo), "hooks", "pre-receive")
	writeExecutable(t, hook, "#!/bin/sh\n"+liveCommitScript(t, repo.Dir, "landed-during-the-push")+"\necho refused >&2\nexit 1\n")
	out, err := releaseCommand(t, repo, false, false)
	if err == nil {
		t.Fatalf("a refused candidate push did not stop the release:\n%s", out)
	}
	requireContains(t, err.Error(), "could not be taken back", "rlsbl release resume --watch", "rlsbl release abandon --approve-consequential")
	if _, found := loadState(t, repo); !found {
		t.Fatal("the state was not kept")
	}
	// The fix the refusal names: the cause dealt with, and the release
	// resumed.
	if err := os.Remove(hook); err != nil {
		t.Fatal(err)
	}
	out, err = releaseCommand(t, repo, true, false)
	if err != nil {
		t.Fatalf("the resume failed once origin accepted pushes: %v\n%s", err, out)
	}
}
