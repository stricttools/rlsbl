package cli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// commandPaths are the app's leaf command paths, dot-separated, without the
// check commands the framework injects: the set strictcli's
// cli-test-coverage check requires to be covered.
func commandPaths(app *strictcli.App) []string {
	var paths []string
	for name := range app.Commands() {
		if name != "check" && name != "failing-checks" {
			paths = append(paths, name)
		}
	}
	var walk func(g *strictcli.Group, prefix string)
	walk = func(g *strictcli.Group, prefix string) {
		for name := range g.Commands {
			paths = append(paths, prefix+name)
		}
		for name, sub := range g.Groups {
			walk(sub, prefix+name+".")
		}
	}
	for name, g := range app.Groups() {
		walk(g, name+".")
	}
	sort.Strings(paths)
	return paths
}

// The committed CLI test-coverage manifest lists every command of the app
// and nothing else, in the rendering strictcli writes: the check strictcli
// runs adds a newly covered command but never removes a deleted one, so a
// stale entry is found here.
func TestTheCLITestCoverageManifestIsFresh(t *testing.T) {
	hygiene.Isolate(t)
	path := filepath.Join("..", "..", ".strictmetadata", ".cli-test-coverage", "manifest.json")
	committed, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	rendered, err := json.MarshalIndent(commandPaths(appWith(t, testsupport.NewFakeHTTP(t))), "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if want := string(rendered) + "\n"; string(committed) != want {
		t.Fatalf("%s does not list the app's commands; write it as below (every command covered by a test, nothing else) and commit it:\n%s", strings.TrimPrefix(path, "../../"), want)
	}
}
