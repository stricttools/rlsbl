package testsupport

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"

	"github.com/stricttools/rlsbl/internal/testsupport/fakegh"
)

// GHAnswer is one canned gh answer: the argv after "gh" it answers, and what
// gh prints and exits with.
type GHAnswer = fakegh.Answer

// GHAnyArg, as an element of GHAnswer.Args, matches any one argument: an
// answer to a question whose argv carries a commit the test cannot know
// before the code under test makes it.
const GHAnyArg = fakegh.AnyArg

// GHCall is one invocation the fake gh received.
type GHCall = fakegh.Call

// fakeGHPackage is the import path of the fake gh program.
const fakeGHPackage = ModulePath + "/internal/testsupport/fakegh/gh"

// fakeGHBuild is the fake gh program of this test binary, built on the first
// FakeGH and removed by RunTests once the tests ran.
var fakeGHBuild struct {
	once sync.Once
	// env is the environment RunTests found, before any test isolated it,
	// so the build uses the real Go caches; nil until RunTests ran.
	env []string
	// goTool is the go command on the PATH RunTests found, and workDir the
	// working directory it found, inside the module.
	goTool  string
	workDir string
	dir     string
	path    string
	err     error
}

// RunTests runs a package's tests and returns the exit status for os.Exit.
// Every package whose tests call FakeGH runs them through it:
//
//	func TestMain(m *testing.M) {
//		os.Exit(testsupport.RunTests(m))
//	}
func RunTests(m *testing.M) int {
	fakeGHBuild.env = os.Environ()
	fakeGHBuild.goTool, _ = exec.LookPath("go")
	fakeGHBuild.workDir, _ = os.Getwd()
	code := m.Run()
	if fakeGHBuild.dir != "" {
		if err := os.RemoveAll(fakeGHBuild.dir); err != nil {
			fmt.Fprintf(os.Stderr, "testsupport: removing the fake gh build: %v\n", err)
			return 1
		}
	}
	return code
}

// fakeGHProgram is the fake gh program, built on the first call.
func fakeGHProgram(t testing.TB) string {
	t.Helper()
	if fakeGHBuild.env == nil {
		t.Fatal("testsupport: FakeGH needs the package's TestMain to run the tests through testsupport.RunTests")
	}
	fakeGHBuild.once.Do(func() {
		if fakeGHBuild.goTool == "" {
			fakeGHBuild.err = fmt.Errorf("no go command was on PATH when the tests started")
			return
		}
		dir, err := os.MkdirTemp("", "rlsbl-fake-gh-")
		if err != nil {
			fakeGHBuild.err = err
			return
		}
		fakeGHBuild.dir = dir
		path := filepath.Join(dir, "gh")
		cmd := exec.Command(fakeGHBuild.goTool, "build", "-o", path, fakeGHPackage)
		cmd.Env = fakeGHBuild.env
		cmd.Dir = fakeGHBuild.workDir
		if out, err := cmd.CombinedOutput(); err != nil {
			fakeGHBuild.err = fmt.Errorf("go build %s: %v\n%s", fakeGHPackage, err, out)
			return
		}
		fakeGHBuild.path = path
	})
	if fakeGHBuild.err != nil {
		t.Fatalf("testsupport: building the fake gh: %v", fakeGHBuild.err)
	}
	return fakeGHBuild.path
}

// GH is a fake gh installed for one test.
type GH struct {
	t   testing.TB
	dir string
}

// FakeGH puts a gh first on PATH for the rest of the test: a symlink named gh
// to the fake gh program (package fakegh, built once per test binary), with
// the answers in a file beside it. The package's TestMain runs the tests
// through RunTests.
//
// Answers are matched on the whole argv, an argument GHAnyArg matching any
// one argument. When several answers match one argv, each call of that argv
// takes the next, and the last one keeps answering. An argv no answer covers
// exits fakegh.Unanswered and names itself on stderr, and Calls records it
// like every other invocation.
func FakeGH(t testing.TB, answers ...GHAnswer) *GH {
	t.Helper()
	program := fakeGHProgram(t)
	dir := t.TempDir()
	if err := os.Symlink(program, filepath.Join(dir, "gh")); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(answers)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, fakegh.AnswersFile), data, 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return &GH{t: t, dir: dir}
}

// Calls is every invocation the fake received, in order.
func (g *GH) Calls() []GHCall {
	g.t.Helper()
	calls, err := fakegh.ReadCalls(g.dir)
	if err != nil {
		g.t.Fatal(err)
	}
	return calls
}
