package ci

import (
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestWorkflowDispatchReadsEveryShapeOfOn(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		on       any
		ok       bool
		takesTag bool
	}{
		{"workflow_dispatch", true, false},
		{"push", false, false},
		{[]any{"push", "workflow_dispatch"}, true, false},
		{map[string]any{"push": nil}, false, false},
		{map[string]any{"workflow_dispatch": nil}, true, false},
		{map[string]any{"workflow_dispatch": map[string]any{"inputs": map[string]any{"tag": map[string]any{}}}}, true, true},
	}
	for _, c := range cases {
		inputs, ok := workflowDispatch(c.on)
		_, takesTag := inputs["tag"]
		if ok != c.ok || takesTag != c.takesTag {
			t.Errorf("%v: ok %v, tag %v", c.on, ok, takesTag)
		}
	}
}

// A dispatch returns no run id, so the run it created is the one that was
// not there before: the listing is asked again until it appears.
func TestTheDispatchedRunIsTheOneThatWasNotThereBefore(t *testing.T) {
	hygiene.Isolate(t)
	commit := strings.Repeat("a", 40)
	list := []string{"run", "list", "--repo", "acme/portal", "--commit", commit, "--workflow", "publish.yml", "--event", "workflow_dispatch", "--limit", "100", "--json", "databaseId,name,workflowName,status,conclusion,headBranch,headSha,event"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: list, Stdout: `[{"databaseId":5}]`},
		testsupport.GHAnswer{Args: list, Stdout: `[{"databaseId":5}]`},
		testsupport.GHAnswer{Args: list, Stdout: `[{"databaseId":9},{"databaseId":5}]`})
	var slept []time.Duration
	var id int64
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		gh, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		slug := github.Repository{Owner: "acme", Name: "portal"}
		before, err := DispatchedRuns(gh, slug, "publish.yml", commit)
		if err != nil {
			return err
		}
		id, err = NewDispatchedRun(gh, slug, "publish.yml", commit, before, func(d time.Duration) { slept = append(slept, d) })
		return err
	})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	if id != 9 || len(slept) != 1 {
		t.Fatalf("id %d after %d waits", id, len(slept))
	}
}
