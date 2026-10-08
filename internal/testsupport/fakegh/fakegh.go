// Package fakegh is the fake gh of rlsbl's test harness: the answers a test
// gives it, the calls it records, and Serve, which answers one invocation.
// The program in its gh directory is built once per test binary by
// testsupport.FakeGH, without the race detector, so a call costs a few
// milliseconds instead of a start of the whole instrumented test binary.
//
// It imports nothing of rlsbl, so the program stays small.
package fakegh

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
)

// Answer is one canned gh answer: the argv after "gh" it answers, and what
// gh prints and exits with.
type Answer struct {
	Args   []string `json:"args"`
	Stdout string   `json:"stdout"`
	Stderr string   `json:"stderr"`
	Exit   int      `json:"exit"`
}

// AnyArg, as an element of Answer.Args, matches any one argument: an answer
// to a question whose argv carries a commit the test cannot know before the
// code under test makes it.
const AnyArg = "\x00any-argument"

// Call is one invocation the fake gh received.
type Call struct {
	Args  []string `json:"args"`
	Stdin string   `json:"stdin"`
	Dir   string   `json:"dir"`
}

// The files the fake keeps beside its gh entry.
const (
	AnswersFile = "gh-answers.json"
	CallsFile   = "gh-calls.jsonl"
)

// Unanswered is the fake's exit status for an argv no answer covers.
const Unanswered = 97

// argsMatch reports whether args is the answer's argv, AnyArg matching any
// one argument.
func argsMatch(pattern, args []string) bool {
	if len(pattern) != len(args) {
		return false
	}
	for i := range pattern {
		if pattern[i] != AnyArg && pattern[i] != args[i] {
			return false
		}
	}
	return true
}

// Serve answers one gh invocation from the answers file beside the gh entry
// that started this process, records the invocation, and returns the exit
// status. When several answers match one argv, each call of that argv takes
// the next, and the last one keeps answering. An argv no answer covers exits
// Unanswered and names itself on stderr, and is recorded like every other
// invocation.
func Serve() int {
	dir, err := entryDir()
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return Unanswered
	}
	args := os.Args[1:]
	stdin, err := io.ReadAll(os.Stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh: reading stdin:", err)
		return Unanswered
	}
	cwd, err := os.Getwd()
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return Unanswered
	}
	previous, err := ReadCalls(dir)
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return Unanswered
	}
	if err := appendCall(dir, Call{Args: args, Stdin: string(stdin), Dir: cwd}); err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return Unanswered
	}
	data, err := os.ReadFile(filepath.Join(dir, AnswersFile))
	if err != nil {
		fmt.Fprintln(os.Stderr, "fake gh:", err)
		return Unanswered
	}
	var answers []Answer
	if err := json.Unmarshal(data, &answers); err != nil {
		fmt.Fprintln(os.Stderr, "fake gh: reading the answers:", err)
		return Unanswered
	}
	var matching []Answer
	for _, a := range answers {
		if argsMatch(a.Args, args) {
			matching = append(matching, a)
		}
	}
	if len(matching) == 0 {
		fmt.Fprintf(os.Stderr, "fake gh: no answer for argv %q\n", args)
		return Unanswered
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

// entryDir is the directory of the gh entry that started this process: the
// one named by argv[0] when that is a path, else the first gh on PATH, which
// testsupport.FakeGH put there.
func entryDir() (string, error) {
	if strings.ContainsRune(os.Args[0], os.PathSeparator) {
		return filepath.Dir(os.Args[0]), nil
	}
	path, err := exec.LookPath("gh")
	if err != nil {
		return "", fmt.Errorf("locating the gh entry on PATH: %w", err)
	}
	return filepath.Dir(path), nil
}

// ReadCalls is every invocation the fake whose entry is in dir received, in
// order.
func ReadCalls(dir string) ([]Call, error) {
	data, err := os.ReadFile(filepath.Join(dir, CallsFile))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var calls []Call
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if line == "" {
			continue
		}
		var c Call
		if err := json.Unmarshal([]byte(line), &c); err != nil {
			return nil, fmt.Errorf("reading %s: %w", CallsFile, err)
		}
		calls = append(calls, c)
	}
	return calls, nil
}

func appendCall(dir string, c Call) error {
	line, err := json.Marshal(c)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(dir, CallsFile), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(line, '\n')); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}
