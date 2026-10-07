package cli

import (
	"errors"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"
)

func scratchRegistry() *registry {
	return newRegistry(strictcli.NewApp("scratch", "0.0.0", "A throwaway application for registration tests"))
}

// mustPanic runs fn and fails the test unless it panics with a message
// containing want.
func mustPanic(t *testing.T, want string, fn func()) {
	t.Helper()
	defer func() {
		t.Helper()
		r := recover()
		if r == nil {
			t.Fatalf("no panic, want one naming %q", want)
		}
		if msg, ok := r.(string); !ok || !strings.Contains(msg, want) {
			t.Fatalf("panic %v, want one naming %q", r, want)
		}
	}()
	fn()
}

type statusPayload struct {
	Version string `json:"version"`
}

func statusSchema() map[string]any {
	return map[string]any{
		"type":                 "object",
		"properties":           map[string]any{"version": map[string]any{"type": "string"}},
		"required":             []any{"version"},
		"additionalProperties": false,
	}
}

func TestACommandsPayloadIsRenderedAndEmittedUnderJSON(t *testing.T) {
	hygiene.Isolate(t)
	r := scratchRegistry()
	r.add(command{
		path:    []string{"status"},
		help:    "Show the version",
		effect:  readOnly,
		payload: statusSchema(),
		render:  func(p any) string { return "version " + p.(map[string]any)["version"].(string) },
		run: func(*strictcli.Context, map[string]any) (any, error) {
			return statusPayload{Version: "0.4.0"}, nil
		},
	})
	human := r.app.Test([]string{"status"})
	if human.ExitCode != 0 || !strings.Contains(human.Stdout, "version 0.4.0") {
		t.Fatalf("human: exit %d, %q, %q", human.ExitCode, human.Stdout, human.Stderr)
	}
	machine := r.app.Test([]string{"status", "--json"})
	if machine.ExitCode != 0 || !strings.Contains(machine.Stdout, `"version"`) || !strings.Contains(machine.Stdout, `"0.4.0"`) {
		t.Fatalf("machine: exit %d, %q, %q", machine.ExitCode, machine.Stdout, machine.Stderr)
	}
}

func TestAHandlerErrorEndsTheCommandWithExitOne(t *testing.T) {
	hygiene.Isolate(t)
	r := scratchRegistry()
	r.add(command{
		path:   []string{"fail"},
		help:   "Fail",
		effect: mutating,
		run: func(*strictcli.Context, map[string]any) (any, error) {
			return nil, errors.New("the release file is missing")
		},
	})
	res := r.app.Test([]string{"fail"})
	if res.ExitCode != 1 || !strings.Contains(res.Stderr, "the release file is missing") {
		t.Fatalf("exit %d, %q", res.ExitCode, res.Stderr)
	}
}

// A command whose exit status is part of its answer ends with the status it
// states, printing its message only when it has one.
func TestAnExitStatusEndsTheCommandWithItsOwnCode(t *testing.T) {
	hygiene.Isolate(t)
	r := scratchRegistry()
	r.add(command{
		path:   []string{"silent"},
		help:   "End with status 2 and no message",
		effect: readOnly,
		run: func(*strictcli.Context, map[string]any) (any, error) {
			return nil, &exitStatus{code: 2}
		},
	})
	r.add(command{
		path:   []string{"loud"},
		help:   "End with status 3 and a message",
		effect: readOnly,
		run: func(*strictcli.Context, map[string]any) (any, error) {
			return nil, &exitStatus{code: 3, message: "the registry did not answer"}
		},
	})
	if res := r.app.Test([]string{"silent"}); res.ExitCode != 2 || strings.Contains(res.Stderr, "error:") {
		t.Fatalf("silent: exit %d, %q", res.ExitCode, res.Stderr)
	}
	if res := r.app.Test([]string{"loud"}); res.ExitCode != 3 || !strings.Contains(res.Stderr, "the registry did not answer") {
		t.Fatalf("loud: exit %d, %q", res.ExitCode, res.Stderr)
	}
}

func TestAPayloadFromACommandDeclaringNoneIsAnError(t *testing.T) {
	hygiene.Isolate(t)
	r := scratchRegistry()
	r.add(command{
		path:   []string{"leaky"},
		help:   "Return an undeclared payload",
		effect: readOnly,
		run:    func(*strictcli.Context, map[string]any) (any, error) { return statusPayload{}, nil },
	})
	if res := r.app.Test([]string{"leaky"}); res.ExitCode != 1 || !strings.Contains(res.Stderr, "declares none") {
		t.Fatalf("exit %d, %q", res.ExitCode, res.Stderr)
	}
}

func TestGroupsAndTheirCommands(t *testing.T) {
	hygiene.Isolate(t)
	r := scratchRegistry()
	r.group([]string{"release"}, "Release commands")
	r.group([]string{"release", "batch"}, "Batch release commands")
	ran := ""
	r.add(command{
		path:     []string{"release", "batch", "order"},
		help:     "Print the order",
		effect:   mutating,
		noDryRun: "it reads state an earlier recorded write would have changed",
		run: func(*strictcli.Context, map[string]any) (any, error) {
			ran = "order"
			return nil, nil
		},
	})
	if res := r.app.Test([]string{"release", "batch", "order"}); res.ExitCode != 0 || ran != "order" {
		t.Fatalf("exit %d, %q", res.ExitCode, res.Stderr)
	}
	if res := r.app.Test([]string{"release", "batch", "order", "--dry-run"}); res.ExitCode == 0 || !strings.Contains(res.Stdout+res.Stderr, "earlier recorded write") {
		t.Fatalf("--dry-run on a command refusing it: exit %d, %q %q", res.ExitCode, res.Stdout, res.Stderr)
	}
}

func TestRegistrationRules(t *testing.T) {
	hygiene.Isolate(t)
	run := func(*strictcli.Context, map[string]any) (any, error) { return nil, nil }
	mustPanic(t, "states no effect", func() {
		scratchRegistry().add(command{path: []string{"x"}, help: "X", run: run})
	})
	mustPanic(t, "needs a help text", func() {
		scratchRegistry().add(command{path: []string{"x"}, effect: readOnly, run: run})
	})
	mustPanic(t, "has no handler", func() {
		scratchRegistry().add(command{path: []string{"x"}, help: "X", effect: readOnly})
	})
	mustPanic(t, "together", func() {
		scratchRegistry().add(command{path: []string{"x"}, help: "X", effect: readOnly, run: run, payload: statusSchema()})
	})
	mustPanic(t, "before its group", func() {
		scratchRegistry().add(command{path: []string{"g", "x"}, help: "X", effect: readOnly, run: run})
	})
	mustPanic(t, "before its parent", func() {
		scratchRegistry().group([]string{"a", "b"}, "B")
	})
	mustPanic(t, "declared twice", func() {
		r := scratchRegistry()
		r.group([]string{"a"}, "A")
		r.group([]string{"a"}, "A")
	})
}
