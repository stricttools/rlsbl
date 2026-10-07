package runstate_test

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// mutating runs fn in a mutating command and returns the dispatch's result
// and fn's error.
func mutating(t *testing.T, dryRun bool, fn func(e *strictcli.Effects) error) (strictcli.Result, error) {
	t.Helper()
	var ferr error
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun}, func(ctx *strictcli.Context) error {
		ferr = fn(ctx.Effects())
		return nil
	})
	return res, ferr
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// fullState carries every field the in-progress state holds.
func fullState() runstate.InProgress {
	return runstate.InProgress{
		Releasable:           "portal",
		RepresentativeMember: "root",
		Version:              "0.4.0",
		Tag:                  "v0.4.0",
		CompanionTags:        []string{"cmd/portal/v0.4.0"},
		Branch:               "main",
		Registry:             "npm",
		Bump:                 "minor",
		CommitMessage:        "v0.4.0",
		Description:          "the release",
		Context:              "why it ships",
		Include:              []string{"npm"},
		Exclude:              []string{"pypi"},
		PreReleaseCommit:     strings.Repeat("a", 40),
		PinCommit:            strings.Repeat("b", 40),
		ReleaseCommit:        strings.Repeat("c", 40),
		CreatedCommits:       []string{strings.Repeat("d", 40)},
		RunAllDispatchFor:    strings.Repeat("c", 40),
		CompletedSteps:       []string{"version-bumped"},
		FailedSteps:          map[string]string{"pipelines-published": "npm said no"},
		PublishedTargets:     []string{"npm"},
		PublishedMembers:     []string{"widget"},
	}
}

func TestAnInProgressStateWithEveryFieldRoundTrips(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	want := fullState()
	_, err := mutating(t, false, func(e *strictcli.Effects) error { return runstate.SaveInProgress(e, root, want) })
	must(t, err)
	got, found, err := runstate.LoadInProgress(root, "portal")
	must(t, err)
	if !found || !reflect.DeepEqual(got, want) {
		t.Fatalf("read back\n%+v\nwrote\n%+v", got, want)
	}
	path := filepath.Join(root, ".strictmetadata/.release-state/portal/in-progress.toml")
	info, err := os.Stat(path)
	must(t, err)
	if info.Mode().Perm() != 0o600 {
		t.Errorf("mode %o", info.Mode().Perm())
	}
	gitignore, err := os.ReadFile(filepath.Join(root, ".strictmetadata/.release-state/.gitignore"))
	must(t, err)
	if string(gitignore) != "*\n!.gitignore\n" {
		t.Errorf(".gitignore %q", gitignore)
	}
	text, err := os.ReadFile(path)
	must(t, err)
	for _, key := range []string{"format_version", "releasable", "representative_member", "version", "tag", "companion_tags", "branch", "registry", "bump", "commit_message", "description", "context", "include", "exclude", "pre_release_commit", "pin_commit", "release_commit", "created_commits", "run_all_dispatch_for", "completed_steps", "published_targets", "published_members", "[failed_steps]"} {
		if !strings.Contains(string(text), key) {
			t.Errorf("the file lacks %s:\n%s", key, text)
		}
	}
}

func TestAFirstReleaseStateOmitsWhatItDoesNotHave(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	s := runstate.InProgress{
		Releasable: "portal", RepresentativeMember: "root", Version: "0.1.0", Tag: "v0.1.0", Branch: "main",
		CommitMessage: "v0.1.0", Description: "first", Include: []string{}, Exclude: []string{},
		PreReleaseCommit: strings.Repeat("a", 40), PinCommit: strings.Repeat("a", 40), CompletedSteps: []string{},
	}
	_, err := mutating(t, false, func(e *strictcli.Effects) error { return runstate.SaveInProgress(e, root, s) })
	must(t, err)
	got, _, err := runstate.LoadInProgress(root, "portal")
	must(t, err)
	if got.Bump != "" || got.Registry != "" || got.ReleaseCommit != "" || len(got.FailedSteps) != 0 {
		t.Fatalf("read back %+v", got)
	}
}

func TestInProgressRefusals(t *testing.T) {
	hygiene.Isolate(t)
	valid := string(fullState().Render())
	cases := []struct{ name, text, want string }{
		{"unknown key", "preid = \"rc\"\n" + valid, "preid"},
		{"missing tag", strings.Replace(valid, "tag = \"v0.4.0\"\n", "", 1), "tag"},
		{"other format", strings.Replace(valid, "format_version = 1", "format_version = 2", 1), "format_version 2"},
		{"empty branch", strings.Replace(valid, "branch = \"main\"", "branch = \"\"", 1), "branch is empty"},
		{"both markers", strings.Replace(valid, "completed_steps = [\"version-bumped\"]", "completed_steps = [\"pipelines-published\"]", 1), "both completed and failed"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hygiene.Isolate(t)
			_, err := runstate.ParseInProgress("in-progress.toml", []byte(c.text))
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("got %v", err)
			}
		})
	}
}

func TestAStateFileOfAnotherReleasableIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, ".strictmetadata/.release-state/widget/in-progress.toml"), string(fullState().Render()))
	if _, _, err := runstate.LoadInProgress(root, "widget"); err == nil || !strings.Contains(err.Error(), `names the releasable "portal"`) {
		t.Fatalf("got %v", err)
	}
}

func TestStepMarkers(t *testing.T) {
	hygiene.Isolate(t)
	steps := []string{"version-bumped", "committed", "post-release-hooks-run"}
	fatal := map[string]bool{"version-bumped": true, "committed": true}
	var s runstate.InProgress
	if got := s.MissingSteps(steps); !reflect.DeepEqual(got, steps) {
		t.Fatalf("missing %v", got)
	}
	s.Complete("version-bumped")
	s.Complete("version-bumped")
	if len(s.CompletedSteps) != 1 {
		t.Fatalf("a step was recorded twice: %v", s.CompletedSteps)
	}
	s.Fail("committed", "boom")
	if !s.FatalFailure(fatal) || s.IsComplete(steps, fatal) {
		t.Fatal("a fatal failure left the state complete")
	}
	s.Complete("committed")
	if _, failed := s.FailedSteps["committed"]; failed {
		t.Fatal("success left the failure marker")
	}
	s.Fail("post-release-hooks-run", "hook failed")
	if !s.IsComplete(steps, fatal) {
		t.Fatal("a non-fatal failure kept the state incomplete")
	}
	s.Fail("version-bumped", "again")
	if s.Completed("version-bumped") {
		t.Fatal("a failure left the success marker")
	}
	s.Complete("snapshot-published")
	if got := s.UnknownSteps(steps); !reflect.DeepEqual(got, []string{"snapshot-published"}) {
		t.Fatalf("unknown steps %v", got)
	}
}

func TestClearingRemovesTheStateAndItsEmptyDirectory(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := mutating(t, false, func(e *strictcli.Effects) error { return runstate.SaveInProgress(e, root, fullState()) })
	must(t, err)
	names, err := runstate.InProgressReleasables(root)
	must(t, err)
	if !reflect.DeepEqual(names, []string{"portal"}) {
		t.Fatalf("in progress %v", names)
	}
	_, err = mutating(t, false, func(e *strictcli.Effects) error { return runstate.ClearInProgress(e, root, "portal") })
	must(t, err)
	if _, err := os.Stat(filepath.Join(root, ".strictmetadata/.release-state/portal")); !os.IsNotExist(err) {
		t.Fatal("the empty directory is still there")
	}
	_, found, err := runstate.LoadInProgress(root, "portal")
	must(t, err)
	if found {
		t.Fatal("the state is still there")
	}
	_, err = mutating(t, false, func(e *strictcli.Effects) error { return runstate.ClearInProgress(e, root, "portal") })
	must(t, err)
}

func TestADryRunSaveWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := mutating(t, true, func(e *strictcli.Effects) error { return runstate.SaveInProgress(e, root, fullState()) })
	must(t, err)
	if _, err := os.Stat(filepath.Join(root, ".strictmetadata")); !os.IsNotExist(err) {
		t.Fatal("a dry run wrote run state")
	}
}

func TestRetryFiles(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	want := runstate.Retry{Ref: "v0.4.0", Workflows: []string{"publish.yml", "docs.yml"}}
	_, err := mutating(t, false, func(e *strictcli.Effects) error { return runstate.SaveRetry(e, root, "portal", want) })
	must(t, err)
	got, found, err := runstate.LoadRetry(root, "portal")
	must(t, err)
	if !found || !reflect.DeepEqual(got, want) {
		t.Fatalf("read back %+v", got)
	}
	for _, c := range []struct{ name, text, want string }{
		{"no ref", "format_version = 1\nref = \"\"\nworkflows = [\"publish.yml\"]\n", "is not a tag"},
		{"no workflow", "format_version = 1\nref = \"v1.0.0\"\nworkflows = []\n", "names no workflow"},
		{"a workflow twice", "format_version = 1\nref = \"v1.0.0\"\nworkflows = [\"a.yml\", \"a.yml\"]\n", "twice"},
		{"the first format", "version = \"1.0.0\"\ndispatch = [\"a.yml\"]\n", "retry.toml"},
	} {
		if _, err := runstate.ParseRetry("retry.toml", []byte(c.text)); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: got %v", c.name, err)
		}
	}
	_, err = mutating(t, false, func(e *strictcli.Effects) error { return runstate.RemoveRetry(e, root, "portal") })
	must(t, err)
	if _, found, _ := runstate.LoadRetry(root, "portal"); found {
		t.Fatal("the retry file is still there")
	}
}

func TestBatchPlans(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	want := runstate.BatchPlan{Items: []runstate.BatchPlanItem{
		{Name: "widget", BaseVersion: "0.1.0", TargetVersion: "0.2.0", Tag: "widget@v0.2.0", Registry: "npm", Bump: "minor"},
		{Name: "gadget", BaseVersion: "1.0.0", TargetVersion: "1.0.1", Tag: "gadget@v1.0.1", Registry: "", Bump: "patch"},
	}}
	_, err := mutating(t, false, func(e *strictcli.Effects) error { return runstate.SaveBatchPlan(e, root, want) })
	must(t, err)
	got, found, err := runstate.LoadBatchPlan(root)
	must(t, err)
	if !found || !reflect.DeepEqual(got, want) {
		t.Fatalf("read back %+v", got)
	}
	if it, ok := got.Item("gadget"); !ok || it.Tag != "gadget@v1.0.1" {
		t.Fatalf("item %+v", it)
	}
	twice := string(runstate.RenderBatchPlan(runstate.BatchPlan{Items: []runstate.BatchPlanItem{want.Items[0], want.Items[0]}}))
	if _, err := runstate.ParseBatchPlan("batch-plan.toml", []byte(twice)); err == nil || !strings.Contains(err.Error(), "planned twice") {
		t.Fatalf("got %v", err)
	}
}

func TestTheLockRefusesASecondHolder(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	var lock *runstate.Lock
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		var err error
		lock, err = runstate.Acquire(e, root, runstate.AcquireOptions{Wait: runstate.RefuseWhenHeld})
		return err
	})
	must(t, err)
	path := filepath.Join(root, ".strictmetadata/.release-state/lock")
	if _, err := os.Stat(path); err != nil {
		t.Fatal("the lock file was not created")
	}
	// Another holder: a descriptor of its own, as another process has.
	other, err := os.Open(path)
	must(t, err)
	defer other.Close()
	if err := syscall.Flock(int(other.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err == nil {
		t.Fatal("the lock was not held")
	}
	stale, err := runstate.IsStale(root)
	must(t, err)
	if stale {
		t.Fatal("a held lock was reported stale")
	}
	must(t, lock.Release())
	if err := lock.Release(); err == nil {
		t.Fatal("a lock was released twice")
	}
	stale, err = runstate.IsStale(root)
	must(t, err)
	if !stale {
		t.Fatal("a released lock's file was not reported stale")
	}
	must(t, syscall.Flock(int(other.Fd()), syscall.LOCK_EX|syscall.LOCK_NB))
	_, err = mutating(t, false, func(e *strictcli.Effects) error {
		_, err := runstate.Acquire(e, root, runstate.AcquireOptions{Wait: runstate.RefuseWhenHeld})
		return err
	})
	var held *runstate.HeldError
	if !errors.As(err, &held) || !strings.Contains(err.Error(), "another rlsbl process holds .strictmetadata/.release-state/lock") {
		t.Fatalf("got %v", err)
	}
	must(t, syscall.Flock(int(other.Fd()), syscall.LOCK_UN))
}

func TestANestedAcquireLeavesTheLockWithItsOuterHolder(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		outer, err := runstate.Acquire(e, root, runstate.AcquireOptions{Wait: runstate.RefuseWhenHeld})
		if err != nil {
			return err
		}
		inner, err := runstate.Acquire(e, root, runstate.AcquireOptions{Wait: runstate.RefuseWhenHeld})
		if err != nil {
			return err
		}
		if err := inner.Release(); err != nil {
			return err
		}
		if stale, err := runstate.IsStale(root); err != nil || stale {
			t.Errorf("the inner release dropped the outer holder's lock (stale %v, %v)", stale, err)
		}
		return outer.Release()
	})
	must(t, err)
}

func TestADryRunTakesNoLockWhereNoneWasEverTaken(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := mutating(t, true, func(e *strictcli.Effects) error {
		lock, err := runstate.Acquire(e, root, runstate.AcquireOptions{DryRun: true, Wait: runstate.WaitForHolder, OnWait: func(string) { t.Error("waited") }})
		if err != nil {
			return err
		}
		return lock.Release()
	})
	must(t, err)
	if _, err := os.Stat(filepath.Join(root, ".strictmetadata")); !os.IsNotExist(err) {
		t.Fatal("a dry run created the lock")
	}
}

func TestWaitingNeedsSomethingToTellTheOperator(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		_, err := runstate.Acquire(e, root, runstate.AcquireOptions{Wait: runstate.WaitForHolder})
		return err
	})
	if err == nil {
		t.Fatal("a wait with nobody told was accepted")
	}
}
