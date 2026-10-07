package workflows

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// verdictCorpus is testdata/paths_filter_verdicts.json: every verdict in it
// was produced by running the pinned dorny/paths-filter action's own
// matching code (scripts/capture_paths_filter_verdicts.js), so the reader is
// held to what the action answers, not to its README.
type verdictCorpus struct {
	ActionVersion       string `json:"action_version"`
	PredicateQuantifier string `json:"predicate_quantifier"`
	Cases               []struct {
		Name        string          `json:"name"`
		Patterns    []string        `json:"patterns"`
		HasNegation bool            `json:"has_negation"`
		Verdicts    map[string]bool `json:"verdicts"`
	} `json:"cases"`
}

func readCorpus(t *testing.T) verdictCorpus {
	t.Helper()
	data, err := os.ReadFile("testdata/paths_filter_verdicts.json")
	if err != nil {
		t.Fatal(err)
	}
	var c verdictCorpus
	if err := json.Unmarshal(data, &c); err != nil {
		t.Fatal(err)
	}
	return c
}

func TestTheMatcherReproducesEveryCapturedVerdict(t *testing.T) {
	hygiene.Isolate(t)
	c := readCorpus(t)
	if c.PredicateQuantifier != PredicateQuantifier {
		t.Fatalf("the corpus was captured under %q, the router declares %q", c.PredicateQuantifier, PredicateQuantifier)
	}
	negations := 0
	for _, tc := range c.Cases {
		if tc.HasNegation {
			negations++
		}
		for path, want := range tc.Verdicts {
			if got := MatchesFilter(path, tc.Patterns); got != want {
				t.Errorf("%s: %s against %v: got %v, the action answered %v", tc.Name, path, tc.Patterns, got, want)
			}
		}
	}
	if negations == 0 {
		t.Fatal("the corpus covers no filter with a negated pattern")
	}
}

func TestAnyPathMatchesAsksTheWholeFilterPerPath(t *testing.T) {
	hygiene.Isolate(t)
	root := []string{"**", "!packages/core/**"}
	if AnyPathMatches([]string{"packages/core/a.go"}, root) {
		t.Fatal("a diff inside the excluded territory triggered the root member")
	}
	if !AnyPathMatches([]string{"packages/core/a.go", "README.md"}, root) {
		t.Fatal("a diff touching the root's own file did not trigger it")
	}
}

func TestTheRootMemberMatchesEverythingButTheOthers(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, threeMembers, nil)
	f, err := NewFilters(w)
	if err != nil {
		t.Fatal(err)
	}
	got, err := f.PatternsFor(member(t, w, "root"))
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"**", "!apps/web/**", "!packages/core/**"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	if MatchesFilter("packages/core/x.go", got) || !MatchesFilter("README.md", got) {
		t.Fatalf("the root filter %v does not read as the residual", got)
	}
}

func TestAMemberReactsToItsDependenciesTheRootFilesAndItsReleaseCommit(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, threeMembers, map[string]string{"go.sum": "", "uv.lock": "", "apps/web/go.sum": ""})
	f, err := NewFilters(w)
	if err != nil {
		t.Fatal(err)
	}
	got, err := f.PatternsFor(member(t, w, "web"))
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"apps/web/**", "packages/core/**", "go.sum", "uv.lock", ".github/workflows/ci-router.yml", ".strictmetadata/changelog/web/CHANGELOG.md"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	if MatchesFilter(".strictmetadata/changelog/web/unreleased.jsonl", got) {
		t.Fatal("adding a changelog entry triggers the member's CI")
	}
}

func TestDependencyTerritoriesAreTransitiveAndTheRootWidensToEverything(t *testing.T) {
	hygiene.Isolate(t)
	decls := strings.Replace(threeMembers, "path = \"packages/core\"\nname = \"core\"\nreleasable = \"core\"\n", "path = \"packages/core\"\nname = \"core\"\nreleasable = \"core\"\ndepends_on = [\"root\"]\n", 1)
	w := fixtureWorkspace(t, decls, nil)
	f, err := NewFilters(w)
	if err != nil {
		t.Fatal(err)
	}
	got, err := f.PatternsFor(member(t, w, "web"))
	if err != nil {
		t.Fatal(err)
	}
	if got[0] != "apps/web/**" || got[1] != "**" || got[2] != "packages/core/**" {
		t.Fatalf("got %v", got)
	}
}

const nestedMembers = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "draw"
tag_format = "draw/v{version}"
publish_mode = "none"

[[releasables]]
name = "cmd"
tag_format = "draw/cmd/v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "draw"
name = "draw"
releasable = "draw"

[[members]]
path = "draw/cmd"
name = "cmd"
releasable = "cmd"
`

func TestAParentExcludesItsNestedMemberUnlessItDependsOnIt(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, nestedMembers, nil)
	f, err := NewFilters(w)
	if err != nil {
		t.Fatal(err)
	}
	got, err := f.PatternsFor(member(t, w, "draw"))
	if err != nil {
		t.Fatal(err)
	}
	if got[len(got)-1] != "!draw/cmd/**" {
		t.Fatalf("the parent does not exclude its nested member: %v", got)
	}

	w = fixtureWorkspace(t, strings.Replace(nestedMembers, "name = \"draw\"\nreleasable = \"draw\"\n", "name = \"draw\"\nreleasable = \"draw\"\ndepends_on = [\"cmd\"]\n", 1), nil)
	f, err = NewFilters(w)
	if err != nil {
		t.Fatal(err)
	}
	got, err = f.PatternsFor(member(t, w, "draw"))
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range got {
		if p == "!draw/cmd/**" {
			t.Fatalf("the parent excludes the nested member it depends on: %v", got)
		}
	}
	if !MatchesFilter("draw/cmd/main.go", got) {
		t.Fatalf("the parent does not react to its nested dependency: %v", got)
	}
}

func TestAnUnreadableManifestRefusesTheFilters(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, threeMembers, map[string]string{"apps/web/package.json": "{not json"})
	_, err := NewFilters(w)
	if err == nil || !strings.Contains(err.Error(), "web: apps/web/package.json") {
		t.Fatalf("got %v", err)
	}
}

func TestTheBlockRendersOneEntryPerMember(t *testing.T) {
	hygiene.Isolate(t)
	w := fixtureWorkspace(t, threeMembers, nil)
	f, err := NewFilters(w)
	if err != nil {
		t.Fatal(err)
	}
	block, err := f.Block(w.Members())
	if err != nil {
		t.Fatal(err)
	}
	want := "root:\n  - '**'\n  - '!apps/web/**'\n  - '!packages/core/**'\n" +
		"core:\n  - 'packages/core/**'\n  - '.github/workflows/ci-router.yml'\n  - '.strictmetadata/changelog/core/CHANGELOG.md'\n" +
		"web:\n  - 'apps/web/**'\n  - 'packages/core/**'\n  - '.github/workflows/ci-router.yml'\n  - '.strictmetadata/changelog/web/CHANGELOG.md'\n"
	if block != want {
		t.Fatalf("got:\n%s\nwant:\n%s", block, want)
	}
}
