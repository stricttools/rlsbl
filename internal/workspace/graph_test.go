package workspace

import (
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// graphWorkspace declares a dev-node root and the named members, each at a
// directory of its name, versioned under none; extra holds lines added to
// members by name.
func graphWorkspace(t *testing.T, root string, members []string, extra map[string]string) *Workspace {
	t.Helper()
	text := "format_version = 1\nrepository_layout = \"workspace\"\nrelease_branches = [\"main\"]\nreleasables = []\n\n[[members]]\npath = \".\"\nname = \"root\"\ndev_only = true\nreleasable = false\n"
	for _, m := range members {
		text += "\n[[members]]\npath = \"packages/" + m + "\"\nname = \"" + m + "\"\nreleasable = false\n" + extra[m]
	}
	return newWorkspace(t, root, text)
}

func TestManifestsAndDependsOnMakeTheEdges(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeFixture(t, root, "packages/alpha/pyproject.toml", "[project]\nname = \"alpha\"\ndependencies = [\"Beta_Lib>=1.2\", \"requests\"]\n\n[project.optional-dependencies]\ntest = [\"gamma @ file:///x/gamma\"]\n")
	writeFixture(t, root, "packages/beta/pyproject.toml", "[project]\nname = \"beta.lib\"\n")
	writeFixture(t, root, "packages/web/package.json", `{"dependencies": {"beta": "workspace:*", "left-pad": "1"}, "devDependencies": {"gamma": "file:../gamma"}, "peerDependencies": {"alpha": "^1.0.0"}}`)
	w := graphWorkspace(t, root, []string{"alpha", "beta", "gamma", "web", "tool"}, map[string]string{"tool": "depends_on = [\"web\"]\n"})
	g := NewGraph(w)
	if len(g.ScanErrors) != 0 {
		t.Fatalf("scan errors: %v", g.ScanErrors)
	}
	want := map[string][]Dependency{
		"alpha": {
			{Name: "beta", Form: FormVersioned, Constraint: ">=1.2", Scope: ScopeRuntime},
			{Name: "gamma", Form: FormPath, Constraint: "file:///x/gamma", Scope: ScopeDev},
		},
		"web": {
			{Name: "beta", Form: FormWorkspace, Constraint: "workspace:*", Scope: ScopeRuntime},
			{Name: "gamma", Form: FormPath, Constraint: "file:../gamma", Scope: ScopeDev},
			{Name: "alpha", Form: FormVersioned, Constraint: "^1.0.0", Scope: ScopePeer},
		},
		"tool": {{Name: "web", Form: FormExplicit, Scope: ScopeExplicit}},
	}
	for member, deps := range want {
		if got := g.Dependencies(member); !reflect.DeepEqual(got, deps) {
			t.Errorf("Dependencies(%s) = %#v, want %#v", member, got, deps)
		}
	}
	if got := g.Dependents("beta"); !reflect.DeepEqual(got, []string{"alpha", "web"}) {
		t.Errorf("Dependents(beta) = %v", got)
	}
	order, err := g.TopologicalOrder()
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(order, " "); got != "beta gamma alpha root web tool" {
		t.Errorf("TopologicalOrder = %s", got)
	}
}

func TestACycleIsRefusedNamingItsMembers(t *testing.T) {
	hygiene.Isolate(t)
	w := graphWorkspace(t, t.TempDir(), []string{"a", "b", "c"}, map[string]string{
		"a": "depends_on = [\"b\"]\n",
		"b": "depends_on = [\"a\"]\n",
	})
	g := NewGraph(w)
	_, err := g.TopologicalOrder()
	var cycle *CycleError
	if !errors.As(err, &cycle) || strings.Join(cycle.Members, " ") != "a b" {
		t.Fatalf("TopologicalOrder = %v", err)
	}
	if !g.HasCycles() {
		t.Error("HasCycles = false")
	}
}

func TestTransitiveWalksFollowDepthAndScope(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeFixture(t, root, "packages/d/package.json", `{"devDependencies": {"c": "1.0.0"}}`)
	w := graphWorkspace(t, root, []string{"a", "b", "c", "d"}, map[string]string{
		"b": "depends_on = [\"a\"]\n",
		"c": "depends_on = [\"b\"]\n",
	})
	g := NewGraph(w)
	for _, c := range []struct {
		depth int
		want  string
	}{{-1, "b c d"}, {0, ""}, {1, "b"}, {2, "b c"}} {
		got, err := g.TransitiveDependents("a", c.depth, "")
		if err != nil {
			t.Fatal(err)
		}
		if strings.Join(got, " ") != c.want {
			t.Errorf("TransitiveDependents(a, %d) = %v, want %s", c.depth, got, c.want)
		}
	}
	if got, _ := g.TransitiveDependents("a", -1, ScopeExplicit); strings.Join(got, " ") != "b c" {
		t.Errorf("explicit-only dependents = %v", got)
	}
	if got, _ := g.TransitiveDependencies("d", -1); strings.Join(got, " ") != "c b a" {
		t.Errorf("TransitiveDependencies(d) = %v", got)
	}
	if _, err := g.TransitiveDependencies("zeta", -1); err == nil {
		t.Error("an unknown member was walked")
	}
}

func TestAManifestThatCannotBeReadIsRecordedNotSkipped(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeFixture(t, root, "packages/a/pyproject.toml", "[project\n")
	writeFixture(t, root, "packages/a/package.json", "{")
	writeFixture(t, root, "packages/b/package.json", `{"dependencies": {"c": "1"}}`)
	g := NewGraph(graphWorkspace(t, root, []string{"a", "b", "c"}, nil))
	if len(g.ScanErrors) != 2 || g.ScanErrors[0].Member != "a" || g.ScanErrors[1].Member != "a" {
		t.Fatalf("ScanErrors = %v", g.ScanErrors)
	}
	if got := g.Dependents("c"); !reflect.DeepEqual(got, []string{"b"}) {
		t.Errorf("the readable manifests lost their edges: %v", got)
	}
}

func TestNormalizePyPI(t *testing.T) {
	hygiene.Isolate(t)
	for _, c := range [][2]string{{"Foo_Bar", "foo-bar"}, {"foo..bar", "foo-bar"}, {"foo-_.bar", "foo-bar"}} {
		if got := NormalizePyPI(c[0]); got != c[1] {
			t.Errorf("NormalizePyPI(%s) = %s", c[0], got)
		}
	}
}

func TestEvaluateConstraint(t *testing.T) {
	hygiene.Isolate(t)
	for _, c := range []struct {
		constraint, current string
		want                ConstraintVerdict
	}{
		{">=1.2.0", "1.2.0", ConstraintSatisfied},
		{">=1.2.0", "1.1.9", ConstraintOutdated},
		{">1.2.0", "1.2.0", ConstraintOutdated},
		{"<2.0.0", "1.9.0", ConstraintSatisfied},
		{"<=1.0.0", "1.0.1", ConstraintOutdated},
		{"==1.0.0", "1.0.0", ConstraintSatisfied},
		{"=1.0.0", "1.0.1", ConstraintOutdated},
		{"1.0.0", "1.0.0", ConstraintSatisfied},
		{"^1.2.0", "1.9.0", ConstraintSatisfied},
		{"^1.2.0", "2.0.0", ConstraintOutdated},
		{"^0.3.0", "0.3.5", ConstraintSatisfied},
		{"^0.3.0", "0.4.0", ConstraintOutdated},
		{"~1.2.0", "1.2.9", ConstraintSatisfied},
		{"~1.2.0", "1.3.0", ConstraintOutdated},
		{"~=1.2", "1.2.5", ConstraintSatisfied},
		{">=1.0,<2.0", "1.5.0", ConstraintUnevaluated},
		{"^1 || ^2", "1.5.0", ConstraintUnevaluated},
		{"!=1.0.0", "1.5.0", ConstraintUnevaluated},
		{"", "1.5.0", ConstraintUnevaluated},
		{">=1.x", "1.5.0", ConstraintUnevaluated},
		{">=1.0.0", "dev", ConstraintUnevaluated},
	} {
		if got := EvaluateConstraint(c.constraint, c.current); got != c.want {
			t.Errorf("EvaluateConstraint(%q, %s) = %s, want %s", c.constraint, c.current, got, c.want)
		}
	}
}
