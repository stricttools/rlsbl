package checks

import (
	"fmt"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestAGoModuleWithoutAToolchainLineFailsUntilItDeclaresOne(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	got := runCheck(t, inputs(t, r.Dir), "go-toolchain-declared")
	mustStatus(t, got, "fail")
	mustMention(t, got, "go.mod declares no toolchain line", "go mod edit -toolchain=<version>")
	// What `go mod edit -toolchain=go1.26.6` writes.
	r.Write("go.mod", "module github.com/acme/portal\n\ngo 1.26\n\ntoolchain go1.26.6\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-toolchain-declared"), "pass")
}

func TestTheGoChecksSkipAProjectWithoutAGoTarget(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"go.mod":       "",
		"VERSION":      "",
		"package.json": fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"),
	})
	for _, name := range []string{"go-toolchain-declared", "ldflags-symbol", "go-module-major-suffix"} {
		got := runCheck(t, inputs(t, r.Dir), name)
		mustStatus(t, got, "skip")
		mustMention(t, got, noGoTarget)
	}
}

const goreleaserLdflags = "builds:\n  - main: .\n    ldflags:\n      - -s -w -X main.Version={{.Version}}\n"

func TestAnLdflagsSymbolTheSourceDoesNotDeclareFailsUntilItDoes(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		".goreleaser.yml": goreleaserLdflags,
		"main.go":         "package main\n\nvar version = \"dev\"\n\nfunc main() { println(version) }\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "ldflags-symbol")
	mustStatus(t, got, "fail")
	mustMention(t, got, "Version")
	// The fix the finding names: rename the variable to the injected name.
	r.Write("main.go", "package main\n\nvar Version = \"dev\"\n\nfunc main() { println(Version) }\n")
	got = runCheck(t, inputs(t, r.Dir), "ldflags-symbol")
	mustStatus(t, got, "pass")
	mustMention(t, got, "1 -X symbol(s) match")
}

func TestAnUntrackedBuildFileIsNotBuildConfiguration(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"main.go": "package main\n\nfunc main() {}\n",
	})
	r.Write(".goreleaser.yml", goreleaserLdflags)
	got := runCheck(t, inputs(t, r.Dir), "ldflags-symbol")
	mustStatus(t, got, "pass")
	mustMention(t, got, "no -X linker flag")
}
