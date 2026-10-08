package cli

import (
	"encoding/json"
	"slices"
	"testing"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/checks"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// options.FrameworkChecks is the set of checks strictcli registers into the
// app beside rlsbl's own registry, each with the severity strictcli gives
// it: the app's check listing, run outside any repository so no member
// declares an external check, holds the list to the framework.
func TestTheFrameworkChecksAreTheOnesStrictcliRegisters(t *testing.T) {
	hygiene.Isolate(t)
	t.Chdir(t.TempDir())
	registry, err := tomledit.Unmarshal[struct {
		App    string         `toml:"app"`
		Checks map[string]any `toml:"checks"`
		Hooks  map[string]any `toml:"hooks"`
	}](checks.Registry)
	if err != nil {
		t.Fatal(err)
	}
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"check", "--list", "--json"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	var listing struct {
		Payload []struct {
			Name     string `json:"name"`
			Severity string `json:"severity"`
		} `json:"payload"`
	}
	if err := json.Unmarshal([]byte(r.Stdout), &listing); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, r.Stdout)
	}
	var registered []string
	for _, c := range listing.Payload {
		if _, own := registry.Checks[c.Name]; !own {
			registered = append(registered, c.Name+" "+c.Severity)
		}
	}
	var declared []string
	for _, f := range options.FrameworkChecks {
		declared = append(declared, f.Name+" "+f.Severity)
	}
	slices.Sort(registered)
	slices.Sort(declared)
	if !slices.Equal(registered, declared) {
		t.Fatalf("strictcli registers the framework checks %q, and options.FrameworkChecks declares %q: bring internal/options/render.go in line, then run `go run ./internal/options/gen` from the repository root and commit the result", registered, declared)
	}
}

// failing-checks runs the same registry as check: its listing names the
// checks check lists.
func TestFailingChecksListsTheChecksCheckLists(t *testing.T) {
	hygiene.Isolate(t)
	t.Chdir(t.TempDir())
	names := func(command string) []string {
		r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{command, "--list", "--json"})
		if r.ExitCode != 0 {
			t.Fatalf("%s --list exited %d:\n%s%s", command, r.ExitCode, r.Stdout, r.Stderr)
		}
		var listing struct {
			Payload []struct {
				Name string `json:"name"`
			} `json:"payload"`
		}
		if err := json.Unmarshal([]byte(r.Stdout), &listing); err != nil {
			t.Fatalf("not JSON: %v\n%s", err, r.Stdout)
		}
		var out []string
		for _, c := range listing.Payload {
			out = append(out, c.Name)
		}
		slices.Sort(out)
		return out
	}
	if checkNames, failingNames := names("check"), names("failing-checks"); len(checkNames) == 0 || !slices.Equal(checkNames, failingNames) {
		t.Fatalf("check lists %q, failing-checks lists %q", checkNames, failingNames)
	}
}
