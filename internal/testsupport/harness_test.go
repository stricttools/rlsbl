package testsupport

import (
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"
)

func TestMain(m *testing.M) {
	if IsFakeGH() {
		os.Exit(FakeGHMain())
	}
	os.Exit(m.Run())
}

func TestARepositoryWithABareRemoteTakesAFewLines(t *testing.T) {
	hygiene.Isolate(t)
	r := NewRepo(t)
	head := r.CommitFile("README.md", "# hi\n", "initial")
	bare := r.AddBareRemote("origin")
	r.Git("push", "-q", "origin", "main")
	if got := Refs(t, bare)["refs/heads/main"]; got != head {
		t.Fatalf("the bare remote's main is %q, want %q", got, head)
	}
	if url := r.Git("remote", "get-url", "origin"); url != "file://"+bare {
		t.Fatalf("origin is %q, want a file:// URL", url)
	}
}

func TestGitReturnsStdoutTrimmedAndGitResultKeepsAFailure(t *testing.T) {
	hygiene.Isolate(t)
	r := NewRepo(t)
	r.CommitFile("a.txt", "a\n", "first")
	if branch := r.Git("rev-parse", "--abbrev-ref", "HEAD"); branch != "main" {
		t.Fatalf("branch = %q", branch)
	}
	_, stderr, code := r.GitResult("show", "HEAD:missing.txt")
	if code == 0 || stderr == "" {
		t.Fatalf("showing a missing path exited %d with stderr %q", code, stderr)
	}
}

func TestCommitFileCreatesParentsAndReturnsHead(t *testing.T) {
	hygiene.Isolate(t)
	r := NewRepo(t)
	head := r.CommitFile("deep/er/file.txt", "x\n", "nested")
	if head != r.Head() || len(head) != 40 {
		t.Fatalf("CommitFile returned %q, HEAD is %q", head, r.Head())
	}
	if _, err := os.Stat(r.Path("deep/er/file.txt")); err != nil {
		t.Fatal(err)
	}
	if author := r.Git("log", "-1", "--format=%an"); author == "" {
		t.Fatal("the commit carries no author")
	}
}

func TestAddBareRemotePushesNothing(t *testing.T) {
	hygiene.Isolate(t)
	r := NewRepo(t)
	r.CommitFile("a.txt", "a\n", "first")
	bare := r.AddBareRemote("origin")
	if refs := Refs(t, bare); len(refs) != 0 {
		t.Fatalf("a fresh bare remote holds refs: %v", refs)
	}
}

func TestCloneHasTheRepositoryAsOrigin(t *testing.T) {
	hygiene.Isolate(t)
	r := NewRepo(t)
	head := r.CommitFile("a.txt", "a\n", "first")
	c := r.Clone()
	if c.Head() != head {
		t.Fatalf("the clone's HEAD is %q, want %q", c.Head(), head)
	}
}

func TestRunCommandHandsTheHandlerAnEffectsHandle(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	r := RunCommand(t, CommandOptions{Effect: strictcli.EffectMutating}, func(ctx *strictcli.Context) error {
		_, err := ctx.Effects().Write(filepath.Join(dir, "out.txt"), "written")
		return err
	})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	if data, err := os.ReadFile(filepath.Join(dir, "out.txt")); err != nil || string(data) != "written" {
		t.Fatalf("the write did not happen: %q, %v", data, err)
	}
}

func TestRunCommandUnderDryRunRecordsTheWrite(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	r := RunCommand(t, CommandOptions{Effect: strictcli.EffectMutating, DryRun: true}, func(ctx *strictcli.Context) error {
		_, err := ctx.Effects().Write(filepath.Join(dir, "out.txt"), "written")
		return err
	})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	if _, err := os.Stat(filepath.Join(dir, "out.txt")); !os.IsNotExist(err) {
		t.Fatalf("a dry run performed the write: %v", err)
	}
}

func TestRunCommandReportsTheHandlersError(t *testing.T) {
	hygiene.Isolate(t)
	r := RunCommand(t, CommandOptions{Effect: strictcli.EffectReadOnly}, func(*strictcli.Context) error {
		return io.ErrUnexpectedEOF
	})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, io.ErrUnexpectedEOF.Error()) {
		t.Fatalf("exit %d, stderr %q", r.ExitCode, r.Stderr)
	}
}

func TestFakeGHAnswersRecordsAndRefusesTheUnanswered(t *testing.T) {
	hygiene.Isolate(t)
	gh := FakeGH(t,
		GHAnswer{Args: []string{"repo", "view"}, Stdout: "first\n"},
		GHAnswer{Args: []string{"repo", "view"}, Stdout: "second\n"},
		GHAnswer{Args: []string{"run", "list"}, Stderr: "boom\n", Exit: 2},
	)
	run := func(stdin string, args ...string) (string, string, int) {
		cmd := exec.Command("gh", args...)
		cmd.Stdin = strings.NewReader(stdin)
		var out, errOut strings.Builder
		cmd.Stdout = &out
		cmd.Stderr = &errOut
		_ = cmd.Run()
		return out.String(), errOut.String(), cmd.ProcessState.ExitCode()
	}
	if out, _, code := run("", "repo", "view"); out != "first\n" || code != 0 {
		t.Fatalf("first answer: %q, %d", out, code)
	}
	if out, _, _ := run("", "repo", "view"); out != "second\n" {
		t.Fatalf("second answer: %q", out)
	}
	if out, _, _ := run("", "repo", "view"); out != "second\n" {
		t.Fatalf("the last answer does not keep answering: %q", out)
	}
	if _, stderr, code := run("token", "run", "list"); stderr != "boom\n" || code != 2 {
		t.Fatalf("failing answer: %q, %d", stderr, code)
	}
	if _, stderr, code := run("", "release", "create", "v1.0.0"); code != ghUnanswered || !strings.Contains(stderr, "no answer") {
		t.Fatalf("unanswered argv: %q, %d", stderr, code)
	}
	calls := gh.Calls()
	if len(calls) != 5 {
		t.Fatalf("recorded %d calls, want 5: %+v", len(calls), calls)
	}
	if calls[3].Stdin != "token" || strings.Join(calls[4].Args, " ") != "release create v1.0.0" {
		t.Fatalf("calls = %+v", calls)
	}
}

// A race-instrumented fake gh pauses a second at every exit unless GORACE
// says otherwise; the fake starts without the pause, keeping the test's own
// race options.
func TestTheFakeGHStartsWithoutTheRaceDetectorsExitPause(t *testing.T) {
	hygiene.Isolate(t)
	t.Setenv("GORACE", "halt_on_error=1")
	FakeGH(t)
	if got := os.Getenv("GORACE"); got != "halt_on_error=1 atexit_sleep_ms=0" {
		t.Fatalf("GORACE is %q", got)
	}
	if got := fakeGHRaceOptions(""); got != "atexit_sleep_ms=0" {
		t.Fatalf("without race options of its own the fake starts under %q", got)
	}
}

func TestFakeHTTPAnswersAndRecords(t *testing.T) {
	hygiene.Isolate(t)
	f := NewFakeHTTP(t, HTTPAnswer{Method: "GET", URL: "https://registry.npmjs.org/widget", Status: 200, Body: `{"name":"widget"}`})
	resp, err := f.Client().Get("https://registry.npmjs.org/widget")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || string(body) != `{"name":"widget"}` {
		t.Fatalf("answer = %d %q", resp.StatusCode, body)
	}
	if urls := f.URLs(); len(urls) != 1 || urls[0] != "https://registry.npmjs.org/widget" {
		t.Fatalf("recorded %v", urls)
	}
}
