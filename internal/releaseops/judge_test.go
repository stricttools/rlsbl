package releaseops

import (
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func evidence(source string, f Finding) Evidence {
	return Evidence{Source: source, Subject: "portal", Finding: f, Message: string(f)}
}

func TestTheJudgmentClearsOnlyOnObservedAbsenceWithNothingPublished(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		name     string
		evidence []Evidence
		verdict  Verdict
		reason   string
	}{
		{"no evidence at all", nil, Blocked, "no source observed"},
		{"only inconclusive", []Evidence{evidence("npm", Inconclusive), evidence("go proxy", Inconclusive)}, Blocked, "no source observed"},
		{"unpublished", []Evidence{evidence("npm", Unpublished)}, Cleared, "observed its absence"},
		{"unpublished beside inconclusive", []Evidence{evidence("go tag", Unpublished), evidence("go proxy", Inconclusive)}, Cleared, "observed its absence"},
		{"published overrides unpublished", []Evidence{evidence("npm", Unpublished), evidence("go proxy", Published)}, Blocked, "published: go proxy portal"},
		{"a running publish blocks", []Evidence{evidence("npm", Unpublished), evidence("publish runs", Publishing)}, Blocked, "still running"},
	}
	for _, c := range cases {
		got := judge(c.evidence)
		if got.Verdict != c.verdict || !strings.Contains(got.Reason, c.reason) {
			t.Errorf("%s: %+v", c.name, got)
		}
		if len(got.Evidence) != len(c.evidence) {
			t.Errorf("%s: the result carries %d pieces of evidence, not %d", c.name, len(got.Evidence), len(c.evidence))
		}
	}
}

// The proxy's silence is observed and clears nothing on its own, and an
// unanswered question leaves a package's publication unknown.
func TestAPackagesFindingForAYank(t *testing.T) {
	hygiene.Isolate(t)
	absentFromProxy := Evidence{Source: "go proxy", Finding: Inconclusive, observed: true}
	proxyDown := Evidence{Source: "go proxy", Finding: Inconclusive}
	cases := []struct {
		name     string
		evidence []Evidence
		want     Finding
	}{
		{"listed", []Evidence{evidence("npm", Published)}, Published},
		{"not listed", []Evidence{evidence("npm", Unpublished)}, Unpublished},
		{"a registry down", []Evidence{evidence("npm", Inconclusive)}, Inconclusive},
		{"no tag, the proxy silent", []Evidence{evidence("go tag", Unpublished), absentFromProxy}, Unpublished},
		{"no tag, the proxy down", []Evidence{evidence("go tag", Unpublished), proxyDown}, Inconclusive},
		{"a tag", []Evidence{evidence("go tag", Published), absentFromProxy}, Published},
		{"only the proxy's silence", []Evidence{absentFromProxy}, Inconclusive},
	}
	for _, c := range cases {
		if got := packageFinding(c.evidence); got != c.want {
			t.Errorf("%s: %s, want %s", c.name, got, c.want)
		}
	}
}

func TestAGoModulesTagFollowsItsDirectory(t *testing.T) {
	hygiene.Isolate(t)
	v, _ := semver.Parse("0.4.0")
	if got := goModuleTag(".", v); got != "v0.4.0" {
		t.Errorf("root: %s", got)
	}
	if got := goModuleTag("widget/cmd", v); got != "widget/cmd/v0.4.0" {
		t.Errorf("below the root: %s", got)
	}
}

func TestTheDerivedBumpReadsTheHighestDifferingComponent(t *testing.T) {
	hygiene.Isolate(t)
	parse := func(s string) semver.Version {
		v, err := semver.Parse(s)
		if err != nil {
			t.Fatal(err)
		}
		return v
	}
	cases := []struct {
		v, latest string
		released  bool
		want      semver.Bump
	}{
		{"0.4.0", "0.3.0", true, semver.Minor},
		{"0.7.0", "0.3.2", true, semver.Minor},
		{"1.0.0", "0.9.9", true, semver.Major},
		{"0.3.1", "0.3.0", true, semver.Patch},
		{"0.1.0", "", false, semver.Minor},
	}
	for _, c := range cases {
		latest := semver.Version{}
		if c.latest != "" {
			latest = parse(c.latest)
		}
		if got := derivedBump(parse(c.v), latest, c.released); got != c.want {
			t.Errorf("%s from %s: %s, want %s", c.v, c.latest, got, c.want)
		}
	}
}

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
		before, err := dispatchedRuns(gh, slug, "publish.yml", commit)
		if err != nil {
			return err
		}
		id, err = newDispatchedRun(gh, slug, "publish.yml", commit, before, func(d time.Duration) { slept = append(slept, d) })
		return err
	})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
	if id != 9 || len(slept) != 1 {
		t.Fatalf("id %d after %d waits", id, len(slept))
	}
}
