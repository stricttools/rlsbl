package checks

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/targets"
)

func TestAStaleSupportMatrixFailsUntilItIsRegenerated(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	got := runCheck(t, inputs(t, r.Dir), "target-matrix-fresh")
	mustStatus(t, got, "skip")
	mustMention(t, got, targets.MatrixPath)
	fresh, err := targets.RenderMatrix()
	if err != nil {
		t.Fatal(err)
	}
	stale := strings.Replace(string(fresh), `"Python / PyPI"`, `"Python"`, 1)
	if stale == string(fresh) {
		t.Fatal("the fixture's edit changed nothing")
	}
	r.Write(targets.MatrixPath, stale)
	got = runCheck(t, inputs(t, r.Dir), "target-matrix-fresh")
	mustStatus(t, got, "fail")
	mustMention(t, got, targets.RegenerateCommand, `the target "pypi", axis "ecosystem"`)
	// What the regeneration writes.
	r.Write(targets.MatrixPath, string(fresh))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "target-matrix-fresh"), "pass")
}

func TestACommittedMatrixThatIsNotJSONIsNamed(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	r.Write(targets.MatrixPath, "{")
	got := runCheck(t, inputs(t, r.Dir), "target-matrix-fresh")
	mustStatus(t, got, "fail")
	mustMention(t, got, "not JSON")
}
