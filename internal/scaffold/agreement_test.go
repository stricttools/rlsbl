package scaffold

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// A scaffold never creates a VERSION disagreeing with a manifest: a go
// target's VERSION is created at 0.0.0 when it does not exist, while npm
// carries its version in package.json, so an npm and go project with
// package.json at 0.1.0 and no VERSION would come out of scaffold carrying
// two versions. Each case runs with the targets declared in both orders.

const mismatchError = "Scaffolding would create VERSION, but the scaffolded targets disagree " +
	"on the project's version:\n" +
	"  npm: 0.1.0\n" +
	"  go: 0.0.0 (the value VERSION would be created with)\n" +
	"A project carries one version. Create VERSION holding the project's " +
	"version, then re-run `rlsbl scaffold`. Nothing was written."

// targetOrders are the two orders the npm and go targets are declared in.
var targetOrders = []string{
	`targets = [{ name = "npm" }, { name = "go" }]` + "\n",
	`targets = [{ name = "go" }, { name = "npm" }]` + "\n",
}

// npmAndGo is a project with package.json at version and a Go module
// without VERSION, its targets declared in the given order.
func npmAndGo(t *testing.T, order, version string) *testsupport.Repo {
	t.Helper()
	return newProject(t, standalone("none", order), map[string]string{
		"package.json": "{\n  \"name\": \"demo\",\n  \"version\": \"" + version + "\",\n  \"engines\": {\"node\": \">=22\"}\n}\n",
		"go.mod":       "module github.com/acme/gotool\n\ngo 1.23\n",
		"main.go":      "package main\n\nfunc main() {}\n",
	})
}

func TestANewVersionDisagreeingWithPackageJSONRefuses(t *testing.T) {
	hygiene.Isolate(t)
	for _, order := range targetOrders {
		repo := npmAndGo(t, order, "0.1.0")
		before := snapshot(t, repo.Dir)
		s := runScaffold(t, repo.Dir, nil)
		if s.result.ExitCode == 0 || !strings.Contains(s.result.Stderr, mismatchError) {
			t.Fatalf("%s: exit %d\n%s", order, s.result.ExitCode, s.text())
		}
		sameFiles(t, before, snapshot(t, repo.Dir))
	}
}

func TestCreatingVersionAsTheErrorSaysClearsTheRefusal(t *testing.T) {
	hygiene.Isolate(t)
	for _, order := range targetOrders {
		repo := npmAndGo(t, order, "0.1.0")
		if s := runScaffold(t, repo.Dir, nil); s.result.ExitCode == 0 {
			t.Fatalf("%s: the disagreement was not refused", order)
		}
		// The fix the error names: create VERSION holding the project's
		// version.
		repo.Write("VERSION", "0.1.0\n")
		mustScaffold(t, repo.Dir, nil)
		if got := readFile(t, repo, "VERSION"); strings.TrimSpace(got) != "0.1.0" {
			t.Fatalf("%s: VERSION = %q", order, got)
		}
		if !exists(t, repo, ".github/workflows/ci-go.yml") || !exists(t, repo, ".github/workflows/ci-npm.yml") {
			t.Fatalf("%s: the targets' CI workflows were not written", order)
		}
	}
}

func TestANewVersionAgreeingWithPackageJSONProceeds(t *testing.T) {
	hygiene.Isolate(t)
	for _, order := range targetOrders {
		repo := npmAndGo(t, order, "0.0.0")
		mustScaffold(t, repo.Dir, nil)
		if got := readFile(t, repo, "VERSION"); strings.TrimSpace(got) != "0.0.0" {
			t.Fatalf("%s: VERSION = %q", order, got)
		}
	}
}

// An existing VERSION is not compared, even when it disagrees: comparing
// it is version-consistency's question.
func TestAnExistingVersionIsNotCompared(t *testing.T) {
	hygiene.Isolate(t)
	for _, order := range targetOrders {
		repo := npmAndGo(t, order, "0.1.0")
		repo.Write("VERSION", "0.2.0\n")
		mustScaffold(t, repo.Dir, nil)
		if got := readFile(t, repo, "VERSION"); got != "0.2.0\n" {
			t.Fatalf("%s: VERSION = %q", order, got)
		}
	}
}

func TestASingleGoTargetScaffoldCreatesVersion(t *testing.T) {
	hygiene.Isolate(t)
	repo := newProject(t, standalone("none", ""), map[string]string{
		"go.mod":  "module github.com/acme/gotool\n\ngo 1.23\n",
		"main.go": "package main\n\nfunc main() {}\n",
	})
	mustScaffold(t, repo.Dir, nil)
	if got := readFile(t, repo, "VERSION"); strings.TrimSpace(got) != "0.0.0" {
		t.Fatalf("VERSION = %q", got)
	}
}
