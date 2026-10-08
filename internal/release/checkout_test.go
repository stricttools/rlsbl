package release_test

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// mutating runs fn with the effects handle of a mutating command carrying
// rlsbl's observe allowlist and returns fn's error.
func mutating(t *testing.T, dryRun bool, fn func(e *strictcli.Effects) error) (strictcli.Result, error) {
	t.Helper()
	var ferr error
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		ferr = fn(ctx.Effects())
		return ferr
	})
	return r, ferr
}

// inCheckout enters the release checkout of repo's main and runs fn with it.
func inCheckout(t *testing.T, repo *testsupport.Repo, fn func(co *release.Checkout) error) error {
	t.Helper()
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		live, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		co, err := release.EnterCheckout(e, live, "main", repo.Git("rev-parse", "refs/heads/main"))
		if err != nil {
			return err
		}
		return fn(co)
	})
	return err
}

// commitIn commits every change in dir and returns the new HEAD.
func commitIn(t *testing.T, dir, message string) string {
	t.Helper()
	for _, args := range [][]string{{"add", "-A"}, {"commit", "-q", "-m", message}} {
		if _, stderr, code := testsupport.RunGit(t, dir, args...); code != 0 {
			t.Fatalf("git %v in %s: %s", args, dir, stderr)
		}
	}
	out, _, _ := testsupport.RunGit(t, dir, "rev-parse", "HEAD")
	return strings.TrimSpace(out)
}

func status(t *testing.T, dir string) string {
	t.Helper()
	out, _, _ := testsupport.RunGit(t, dir, "status", "--porcelain", "--untracked-files=all")
	return strings.TrimRight(out, "\n")
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestTheCheckoutIsADetachedWorktreeUnderTheCommonDirectory(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	head := repo.Head()
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		want, _ := filepath.EvalSymlinks(filepath.Join(repo.Dir, ".git"))
		if co.Path != filepath.Join(want, "rlsbl", "release-checkout") {
			return fmt.Errorf("the checkout is at %s", co.Path)
		}
		if out, _, _ := testsupport.RunGit(t, co.Path, "rev-parse", "HEAD"); strings.TrimSpace(out) != head {
			return fmt.Errorf("the checkout is at %s, not %s", out, head)
		}
		if _, _, code := testsupport.RunGit(t, co.Path, "symbolic-ref", "-q", "HEAD"); code == 0 {
			return errors.New("the checkout's HEAD is not detached")
		}
		if status(t, co.Path) != "" {
			return fmt.Errorf("the checkout is not clean:\n%s", status(t, co.Path))
		}
		return nil
	}))
}

func TestTheCheckoutIsReusedResetAndCleanedKeepingIgnoredFiles(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.CommitFile(".gitignore", "node_modules/\n", "ignore")
	var path string
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		path = co.Path
		testsupport.WriteFile(t, filepath.Join(path, "package.json"), "scribbled\n")
		testsupport.WriteFile(t, filepath.Join(path, "stray.txt"), "left over\n")
		testsupport.WriteFile(t, filepath.Join(path, "node_modules", "cache"), "warm\n")
		return nil
	}))
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		if co.Path != path {
			return fmt.Errorf("a second checkout at %s", co.Path)
		}
		return nil
	}))
	if got := read(t, filepath.Join(path, "package.json")); got != packageJSON("portal", "0.4.0") {
		t.Errorf("the tracked file was not reset: %q", got)
	}
	if _, err := os.Stat(filepath.Join(path, "stray.txt")); !os.IsNotExist(err) {
		t.Errorf("the untracked file was kept: %v", err)
	}
	if got := read(t, filepath.Join(path, "node_modules", "cache")); got != "warm\n" {
		t.Errorf("the ignored file was removed: %q", got)
	}
}

func TestADirectoryThatIsNotTheCheckoutIsRefusedUntilDeleted(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	squatter := filepath.Join(repo.Dir, ".git", "rlsbl", "release-checkout")
	testsupport.WriteFile(t, filepath.Join(squatter, "something"), "not ours\n")
	err := inCheckout(t, repo, func(*release.Checkout) error { return nil })
	if err == nil || !strings.Contains(err.Error(), squatter) || !strings.Contains(err.Error(), "Delete") {
		t.Fatalf("the squatter was not refused by name: %v", err)
	}
	// The fix the refusal names.
	if err := os.RemoveAll(squatter); err != nil {
		t.Fatal(err)
	}
	mustNotFail(t, inCheckout(t, repo, func(*release.Checkout) error { return nil }))
}

func TestAdvanceWritesBackOnlyTheReleasesPaths(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.CommitFile("README.md", "# portal\n", "readme")
	repo.Write("README.md", "another session's edit\n")
	repo.Write("scratch.txt", "another session's new file\n")
	var released string
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		testsupport.WriteFile(t, filepath.Join(co.Path, "package.json"), packageJSON("portal", "0.5.0"))
		testsupport.WriteFile(t, filepath.Join(co.Path, "generated.txt"), "generated\n")
		released = commitIn(t, co.Path, "v0.5.0")
		return co.Advance("HEAD", "The release", "run the release again")
	}))
	if got := repo.Git("rev-parse", "refs/heads/main"); got != released {
		t.Errorf("main is at %s, not the release commit %s", got, released)
	}
	if got := read(t, repo.Path("package.json")); got != packageJSON("portal", "0.5.0") {
		t.Errorf("package.json: %q", got)
	}
	if got := read(t, repo.Path("generated.txt")); got != "generated\n" {
		t.Errorf("generated.txt: %q", got)
	}
	if got := read(t, repo.Path("README.md")); got != "another session's edit\n" {
		t.Errorf("another session's edit was overwritten: %q", got)
	}
	if got := status(t, repo.Dir); got != " M README.md\n?? scratch.txt" {
		t.Errorf("status:\n%s", got)
	}
}

func TestAnUncommittedEditToAWrittenPathRefusesTheAdvanceUntilTakenBack(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	pin := repo.Head()
	repo.Write("package.json", "another session's edit\n")
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		testsupport.WriteFile(t, filepath.Join(co.Path, "package.json"), packageJSON("portal", "0.5.0"))
		commitIn(t, co.Path, "v0.5.0")
		err := co.Advance("HEAD", "The release", "run the release again")
		var conflict *release.LiveTreeConflictError
		if !errors.As(err, &conflict) || !strings.Contains(err.Error(), "package.json") {
			return fmt.Errorf("the edited path was not refused by name: %v", err)
		}
		if got := repo.Git("rev-parse", "refs/heads/main"); got != pin {
			return fmt.Errorf("main moved to %s", got)
		}
		if got := read(t, repo.Path("package.json")); got != "another session's edit\n" {
			return fmt.Errorf("the edit was overwritten: %q", got)
		}
		// The fix the refusal names: take back the edit.
		repo.Git("checkout", "--", "package.json")
		return co.Advance("HEAD", "The release", "run the release again")
	}))
	if got := read(t, repo.Path("package.json")); got != packageJSON("portal", "0.5.0") {
		t.Errorf("package.json after the advance: %q", got)
	}
}

func TestABranchThatMovedIsRefusedNamingTheCommitsAndNothingIsWritten(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	pin := repo.Head()
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		testsupport.WriteFile(t, filepath.Join(co.Path, "package.json"), packageJSON("portal", "0.5.0"))
		commitIn(t, co.Path, "v0.5.0")
		moved := repo.CommitFile("notes.txt", "someone else's\n", "someone else's commit")
		err := co.Advance("HEAD", "The release", "run the release again")
		var movedErr *release.BranchMovedError
		if !errors.As(err, &movedErr) || !strings.Contains(err.Error(), moved[:12]) || !strings.Contains(err.Error(), "someone else's commit") {
			return fmt.Errorf("the moved branch was not refused naming the commit: %v", err)
		}
		if got := read(t, repo.Path("package.json")); got != packageJSON("portal", "0.4.0") {
			return fmt.Errorf("package.json was written: %q", got)
		}
		// The fix the refusal names: move the commits off the branch.
		repo.Git("reset", "-q", "--hard", pin)
		return co.Advance("HEAD", "The release", "run the release again")
	}))
	if got := read(t, repo.Path("package.json")); got != packageJSON("portal", "0.5.0") {
		t.Errorf("package.json after the advance: %q", got)
	}
}

func TestAWorkingTreeOffTheBranchRefusesTheAdvanceUntilCheckedOutAgain(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		testsupport.WriteFile(t, filepath.Join(co.Path, "package.json"), packageJSON("portal", "0.5.0"))
		commitIn(t, co.Path, "v0.5.0")
		repo.Git("checkout", "-q", "--detach")
		err := co.Advance("HEAD", "The release", "run the release again")
		if err == nil || !strings.Contains(err.Error(), "no longer on main") {
			return fmt.Errorf("a working tree off the branch was not refused: %v", err)
		}
		repo.Git("checkout", "-q", "main")
		return co.Advance("HEAD", "The release", "run the release again")
	}))
}

func TestUnwindTakesTheAdvanceBack(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.CommitFile("README.md", "# portal\n", "readme")
	pin := repo.Head()
	repo.Write("README.md", "another session's edit\n")
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		testsupport.WriteFile(t, filepath.Join(co.Path, "package.json"), packageJSON("portal", "0.5.0"))
		commitIn(t, co.Path, "v0.5.0")
		if err := co.Advance("HEAD", "The release", "run the release again"); err != nil {
			return err
		}
		ok, reason, err := co.UnwindLastAdvance()
		if err != nil || !ok {
			return fmt.Errorf("the unwind was refused: %s %v", reason, err)
		}
		return nil
	}))
	if got := repo.Git("rev-parse", "refs/heads/main"); got != pin {
		t.Errorf("main is at %s, not %s", got, pin)
	}
	if got := read(t, repo.Path("package.json")); got != packageJSON("portal", "0.4.0") {
		t.Errorf("package.json: %q", got)
	}
	if got := status(t, repo.Dir); got != " M README.md" {
		t.Errorf("status:\n%s", got)
	}
}

func TestUnwindRefusesWhenSomethingWasCommittedOnTop(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	var onTop string
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		testsupport.WriteFile(t, filepath.Join(co.Path, "package.json"), packageJSON("portal", "0.5.0"))
		commitIn(t, co.Path, "v0.5.0")
		if err := co.Advance("HEAD", "The release", "run the release again"); err != nil {
			return err
		}
		onTop = repo.CommitFile("notes.txt", "on top\n", "on top")
		ok, reason, err := co.UnwindLastAdvance()
		if err != nil || ok || !strings.Contains(reason, "committed on top") {
			return fmt.Errorf("the unwind was not refused: %v %q %v", ok, reason, err)
		}
		return nil
	}))
	if got := repo.Git("rev-parse", "refs/heads/main"); got != onTop {
		t.Errorf("main is at %s, not %s", got, onTop)
	}
}

func TestTheEnvironmentKeepsGoOffTheWorkingTreesWorkspace(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.Write("go.work", "go 1.26\n")
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		env := co.Environment()
		if env["GOWORK"] != "off" {
			return fmt.Errorf("an uncommitted go.work reached the release: GOWORK=%s", env["GOWORK"])
		}
		if env[release.ReleaseBinEnv] != co.ReleaseBin || !strings.HasPrefix(env["PATH"], co.ReleaseBin+string(os.PathListSeparator)) {
			return fmt.Errorf("the release's binaries do not come first: %v", env)
		}
		if entries, err := os.ReadDir(co.ReleaseBin); err != nil || len(entries) != 0 {
			return fmt.Errorf("the release's directory for binaries is not empty: %v %v", entries, err)
		}
		return nil
	}))
	repo.Commit("a committed workspace", "go.work")
	mustNotFail(t, inCheckout(t, repo, func(co *release.Checkout) error {
		if got := co.Environment()["GOWORK"]; got != filepath.Join(co.Path, "go.work") {
			return fmt.Errorf("the committed go.work is not the checkout's own: %s", got)
		}
		return nil
	}))
}

func TestTheWriteScopeAndThePartitionOfChanges(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	ws, err := workspace.Load(repo.Dir)
	mustNotFail(t, err)
	scope, err := release.WriteScope(ws, "portal")
	mustNotFail(t, err)
	joined := strings.Join(scope, ",")
	for _, want := range []string{"CHANGELOG.md", "package.json", "package-lock.json", "selfdoc.json", ".strictmetadata/releases/portal", ".strictmetadata/changelog/portal"} {
		if !strings.Contains(","+joined+",", ","+want+",") {
			t.Errorf("the scope lacks %s: %v", want, scope)
		}
	}
	blocking, ignored := release.PartitionChanges([]git.Change{
		{Status: " M", Path: "package.json"},
		{Status: " M", Path: "README.md"},
		{Status: "??", Path: ".strictmetadata/releases/portal/unreleased.toml"},
		{Status: "??", Path: "x/y.txt"},
	}, scope)
	if len(blocking) != 2 || blocking[0].Path != "package.json" || blocking[1].Path != ".strictmetadata/releases/portal/unreleased.toml" {
		t.Errorf("blocking: %v", blocking)
	}
	if len(ignored) != 2 || ignored[0].Path != "README.md" || ignored[1].Path != "x/y.txt" {
		t.Errorf("ignored: %v", ignored)
	}
}

func TestRunStateIsNeverCountedAsUncommittedWork(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.Write(".strictmetadata/.release-state/portal/in-progress.toml", "x\n")
	repo.Write(".strictmetadata/.release-state/lock", "")
	repo.Write(".strictmetadata/.release-state/.gitignore", "*\n!.gitignore\n")
	err := run(t, nil, func(e *strictcli.Effects) error {
		live, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		changes, err := release.LiveChanges(live)
		if err != nil {
			return err
		}
		if len(changes) != 0 {
			return fmt.Errorf("run state counted as changes: %v", changes)
		}
		return nil
	})
	mustNotFail(t, err)
}

func TestEnterRefusesUncommittedChangesToWrittenPathsUntilCommitted(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.Write("package.json", packageJSON("portal", "0.4.1"))
	repo.Write("notes.txt", "another session's\n")
	enter := func(dryRun bool) (*release.Session, error) {
		var s *release.Session
		_, err := mutating(t, dryRun, func(e *strictcli.Effects) error {
			live, err := git.Open(e, repo.Dir)
			if err != nil {
				return err
			}
			ws, err := workspace.Load(repo.Dir)
			if err != nil {
				return err
			}
			scope, err := release.WriteScope(ws, "portal")
			if err != nil {
				return err
			}
			s, err = release.Enter(e, live, scope, release.EnterOptions{DryRun: dryRun, What: "The release", Rerun: "run the release again", OnWait: func(string) {}})
			if err != nil {
				return err
			}
			return s.Close()
		})
		return s, err
	}
	preview, err := enter(true)
	mustNotFail(t, err)
	if len(preview.Blocking) != 1 || preview.Blocking[0].Path != "package.json" || preview.Checkout != nil {
		t.Errorf("the preview's report: %+v", preview)
	}
	_, err = enter(false)
	var conflict *release.LiveTreeConflictError
	if !errors.As(err, &conflict) || !strings.Contains(err.Error(), "package.json") || strings.Contains(err.Error(), "notes.txt") {
		t.Fatalf("the written path was not refused alone: %v", err)
	}
	// The fix the refusal names: commit the change.
	repo.Commit("bump by hand", "package.json")
	s, err := enter(false)
	mustNotFail(t, err)
	if s.Checkout == nil || s.Root != s.Checkout.Path || len(s.Ignored) != 1 || s.Ignored[0].Path != "notes.txt" {
		t.Errorf("the session: %+v", s)
	}
}

func TestADetachedHeadRefusesTheReleaseUntilTheBranchIsCheckedOut(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.Git("checkout", "-q", "--detach")
	check := func() error {
		return run(t, nil, func(e *strictcli.Effects) error {
			live, err := git.Open(e, repo.Dir)
			if err != nil {
				return err
			}
			_, err = release.LiveBranch(live)
			return err
		})
	}
	if err := check(); err == nil || !strings.Contains(err.Error(), "detached") {
		t.Fatalf("a detached HEAD was not refused: %v", err)
	}
	repo.Git("checkout", "-q", "main")
	mustNotFail(t, check())
}

// TestEveryProgramInTheSessionGetsTheReleaseEnvironment covers a program the
// release starts in its checkout without naming the release's environment:
// a check running `go list`, the packed-artifact listing. The working tree
// holds an uncommitted go.work naming its own module, and the checkout lies
// below the working tree, so such a program finds that go.work unless the
// session puts the release's environment on every program it starts. Closing
// the session puts the environment back as it was.
func TestEveryProgramInTheSessionGetsTheReleaseEnvironment(t *testing.T) {
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoCache))
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no Go toolchain on PATH")
	}
	t.Setenv("GOPROXY", "off")
	// Unset, with t.Setenv putting it back after the test.
	t.Setenv("GOWORK", "")
	os.Unsetenv("GOWORK")
	repo := testsupport.NewRepo(t)
	repo.Write(declarationsPath, `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
releasable = "portal"
`)
	repo.Write("go.mod", "module example.com/portal\n\ngo 1.21\n")
	repo.Write("main.go", "package main\n\nfunc main() {}\n")
	repo.Write(".gitignore", "/go.work\n")
	repo.Commit("the project", declarationsPath, "go.mod", "main.go", ".gitignore")
	repo.Write("go.work", "go 1.21\n\nuse .\n")

	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		live, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		s, err := release.Enter(e, live, nil, release.EnterOptions{What: "The release", Rerun: "run the release again", OnWait: func(string) {}})
		if err != nil {
			return err
		}
		done, err := e.Run([]interface{}{"go", "list", "-e", "-f", "{{.Name}}", "./..."}, strictcli.Cwd(s.Root), strictcli.Check(false), strictcli.Observe())
		if err != nil {
			return errors.Join(err, s.Close())
		}
		if got := strings.TrimSpace(done.Stdout()); got != "main" {
			return errors.Join(fmt.Errorf("go list in the checkout listed %q (%s)", got, strings.TrimSpace(done.Stderr())), s.Close())
		}
		if err := s.Close(); err != nil {
			return err
		}
		if value, set := os.LookupEnv("GOWORK"); set {
			return fmt.Errorf("closing the session left GOWORK=%s", value)
		}
		return nil
	})
	mustNotFail(t, err)
}
