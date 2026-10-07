package testsupport

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// GHAnswer is one canned gh answer: the argv after "gh" it answers, and what
// gh prints and exits with.
type GHAnswer struct {
	Args   []string `json:"args"`
	Stdout string   `json:"stdout"`
	Stderr string   `json:"stderr"`
	Exit   int      `json:"exit"`
}

// GHAnyArg, as an element of GHAnswer.Args, matches any one argument: an
// answer to a question whose argv carries a commit the test cannot know
// before the code under test makes it.
const GHAnyArg = "\x00any-argument"

// argsMatch reports whether args is the answer's argv, GHAnyArg matching
// any one argument.
func argsMatch(pattern, args []string) bool {
	if len(pattern) != len(args) {
		return false
	}
	for i := range pattern {
		if pattern[i] != GHAnyArg && pattern[i] != args[i] {
			return false
		}
	}
	return true
}

// GHCall is one invocation the fake gh received.
type GHCall struct {
	Args  []string `json:"args"`
	Stdin string   `json:"stdin"`
	Dir   string   `json:"dir"`
}

// The files the fake keeps beside its gh entry.
const (
	ghAnswersFile = "gh-answers.json"
	ghCallsFile   = "gh-calls.jsonl"
)

// ghUnanswered is the fake's exit status for an argv no answer covers.
const ghUnanswered = 97

// GH is a fake gh installed for one test.
type GH struct {
	t   testing.TB
	dir string
}

// FakeGH puts a gh first on PATH for the rest of the test: a symlink named gh
// to the running test binary, with the answers in a file beside it. The
// package's TestMain must hand the process to FakeGHMain when IsFakeGH
// reports the binary was started as gh:
//
//	func TestMain(m *testing.M) {
//		if testsupport.IsFakeGH() {
//			os.Exit(testsupport.FakeGHMain())
//		}
//		os.Exit(m.Run())
//	}
//
// Answers are matched on the whole argv, an argument GHAnyArg matching any
// one argument. When several answers match one argv, each call of that argv
// takes the next, and the last one keeps answering. An argv
// no answer covers exits 97 and names itself on stderr, and Calls records it
// like every other invocation.
func FakeGH(t testing.TB, answers ...GHAnswer) *GH {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		t.Fatalf("testsupport: locating the test binary: %v", err)
	}
	dir := t.TempDir()
	if err := os.Symlink(self, filepath.Join(dir, "gh")); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(answers)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ghAnswersFile), data, 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return &GH{t: t, dir: dir}
}

// Calls is every invocation the fake received, in order.
func (g *GH) Calls() []GHCall {
	g.t.Helper()
	calls, err := readGHCalls(g.dir)
	if err != nil {
		g.t.Fatal(err)
	}
	return calls
}

// IsFakeGH reports whether this process is the test binary started as gh.
func IsFakeGH() bool {
	return filepath.Base(os.Args[0]) == "gh"
}

// FakeGHMain answers one gh invocation from the answers file beside the gh
// entry that started this process, records the invocation, and returns the
// exit status.
func FakeGHMain() int {
	dir, err := fakeGHDir()
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return ghUnanswered
	}
	args := os.Args[1:]
	stdin, err := io.ReadAll(os.Stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh: reading stdin:", err)
		return ghUnanswered
	}
	cwd, err := os.Getwd()
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return ghUnanswered
	}
	previous, err := readGHCalls(dir)
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return ghUnanswered
	}
	if err := appendGHCall(dir, GHCall{Args: args, Stdin: string(stdin), Dir: cwd}); err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return ghUnanswered
	}
	data, err := os.ReadFile(filepath.Join(dir, ghAnswersFile))
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return ghUnanswered
	}
	var answers []GHAnswer
	if err := json.Unmarshal(data, &answers); err != nil {
		fmt.Fprintln(os.Stderr, "fake gh: reading the answers:", err)
		return ghUnanswered
	}
	var matching []GHAnswer
	for _, a := range answers {
		if argsMatch(a.Args, args) {
			matching = append(matching, a)
		}
	}
	if len(matching) == 0 {
		fmt.Fprintf(os.Stderr, "fake gh: no answer for argv %q\n", args)
		return ghUnanswered
	}
	seen := 0
	for _, c := range previous {
		if slices.Equal(c.Args, args) {
			seen++
		}
	}
	answer := matching[min(seen, len(matching)-1)]
	fmt.Fprint(os.Stdout, answer.Stdout)
	fmt.Fprint(os.Stderr, answer.Stderr)
	return answer.Exit
}

// fakeGHDir is the directory of the gh entry that started this process: the
// one named by argv[0] when that is a path, else the first gh on PATH, which
// FakeGH put there.
func fakeGHDir() (string, error) {
	if strings.ContainsRune(os.Args[0], os.PathSeparator) {
		return filepath.Dir(os.Args[0]), nil
	}
	path, err := exec.LookPath("gh")
	if err != nil {
		return "", fmt.Errorf("locating the gh entry on PATH: %w", err)
	}
	return filepath.Dir(path), nil
}

func readGHCalls(dir string) ([]GHCall, error) {
	data, err := os.ReadFile(filepath.Join(dir, ghCallsFile))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var calls []GHCall
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if line == "" {
			continue
		}
		var c GHCall
		if err := json.Unmarshal([]byte(line), &c); err != nil {
			return nil, fmt.Errorf("reading %s: %w", ghCallsFile, err)
		}
		calls = append(calls, c)
	}
	return calls, nil
}

func appendGHCall(dir string, c GHCall) error {
	line, err := json.Marshal(c)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(dir, ghCallsFile), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(line, '\n')); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}
