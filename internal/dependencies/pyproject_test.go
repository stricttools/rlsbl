package dependencies_test

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/dependencies"
)

func parse(t *testing.T, text string) *dependencies.Pyproject {
	t.Helper()
	p, err := dependencies.ParsePyproject("pyproject.toml", []byte(text))
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestParseRequirement(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		text string
		want dependencies.Requirement
	}{
		{"requests>=2.0", dependencies.Requirement{Name: "requests", Specifier: ">=2.0"}},
		{"my-lib[extra] >=1.0", dependencies.Requirement{Name: "my-lib", Extras: "[extra]", Specifier: ">=1.0"}},
		{"foo @ file:///path/to/foo", dependencies.Requirement{Name: "foo", DirectReference: true, Specifier: "file:///path/to/foo"}},
		{"foo[cli] @ {root:uri}/foo ; os_name=='nt'", dependencies.Requirement{Name: "foo", Extras: "[cli]", DirectReference: true, Specifier: "{root:uri}/foo", Marker: "; os_name=='nt'"}},
		{"Zope.Interface", dependencies.Requirement{Name: "Zope.Interface"}},
	}
	for _, c := range cases {
		got, ok := dependencies.ParseRequirement(c.text)
		if !ok || got != c.want {
			t.Errorf("%q: got %+v, %v", c.text, got, ok)
		}
	}
	if _, ok := dependencies.ParseRequirement("  "); ok {
		t.Error("blank text parsed")
	}
	if got := (dependencies.Requirement{Name: "Zope.Interface"}).Normalized(); got != "zope-interface" {
		t.Errorf("normalized %q", got)
	}
}

const pathDeps = `# the manifest
[project]
name = "consumer"
dependencies = [
    "core @ file:///abs/core",  # the core sibling
    "requests>=2",
    "util @ {root:uri}/../util",
]

[project.optional-dependencies]
cli = ["clikit @ file:///abs/clikit"]

[dependency-groups]
dev = ["devkit @ file:///abs/devkit", {include-group = "lint"}]
lint = ["ruff"]
`

func TestDirectReferencesFollowTheFamiliesNamed(t *testing.T) {
	hygiene.Isolate(t)
	p := parse(t, pathDeps)
	names := func(families []dependencies.Family) string {
		refs, err := p.DirectReferences(families)
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, r := range refs {
			out = append(out, r.Section+":"+r.Requirement.Name)
		}
		return strings.Join(out, ",")
	}
	if got := names(dependencies.PublishedFamilies); got != "dependencies:core,dependencies:util,optional-dependencies.cli:clikit" {
		t.Errorf("published: %s", got)
	}
	if got := names(dependencies.AllFamilies); !strings.HasSuffix(got, ",dependency-groups.dev:devkit") {
		t.Errorf("all: %s", got)
	}
}

func TestFlooringKeepsExtrasMarkersCommentsAndUnrelatedLines(t *testing.T) {
	hygiene.Isolate(t)
	p := parse(t, `[project]
name = "consumer"
dependencies = [
    "sibling[cli]>=0.1; python_version < '3.12'",
    "other==1",  # keep me
]
`)
	n, err := p.FloorEntries(map[string]string{"sibling": "1.4.0"}, dependencies.AllFamilies)
	if err != nil || n != 1 {
		t.Fatalf("%d, %v", n, err)
	}
	got := string(p.Bytes())
	for _, want := range []string{`"sibling[cli]>=1.4.0; python_version < '3.12'"`, "# keep me", `"other==1"`} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %s in\n%s", want, got)
		}
	}
}

// The PyPI build's rewrite dropped a direct reference's extras and marker
// (it wrote name + constraint), which changes what a consumer installs and
// where; it keeps them now, and matches names by PEP 503 normalization.
func TestThePublishedRewriteKeepsExtrasAndMarkers(t *testing.T) {
	hygiene.Isolate(t)
	text := `[project]
name = "consumer"
dependencies = ["My_Core[fast] @ file:///abs/core ; sys_platform == 'linux'", "requests>=2"]

[dependency-groups]
dev = ["my-core @ file:///abs/core"]
`
	out, n, err := dependencies.RewritePublishedDirectReferences("pyproject.toml", []byte(text), map[string]string{"my-core": ">=1.2.0"})
	if err != nil || n != 1 {
		t.Fatalf("%d, %v", n, err)
	}
	if !strings.Contains(string(out), `"My_Core[fast]>=1.2.0; sys_platform == 'linux'"`) {
		t.Errorf("rewritten:\n%s", out)
	}
	if !strings.Contains(string(out), `dev = ["my-core @ file:///abs/core"]`) {
		t.Errorf("a dependency group, which is not published, was rewritten:\n%s", out)
	}
	same, n, err := dependencies.RewritePublishedDirectReferences("pyproject.toml", []byte("[tool.uv.workspace]\nmembers = []\n"), map[string]string{"x": ">=1"})
	if err != nil || n != 0 || string(same) != "[tool.uv.workspace]\nmembers = []\n" {
		t.Errorf("a manifest without [project]: %q, %d, %v", same, n, err)
	}
}

func TestUvLocalSourcesLeaveRegistryNeutralOnesOut(t *testing.T) {
	hygiene.Isolate(t)
	p := parse(t, `[tool.uv.sources]
core = { path = "../core", editable = true }
util = { workspace = true }
remote = { git = "https://example.com/remote" }
mirror = { index = "internal" }
gated = [{ index = "internal", marker = "sys_platform == 'win32'" }, { path = "../gated", marker = "sys_platform != 'win32'" }]
`)
	sources, err := p.UvLocalSources()
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, s := range sources {
		got = append(got, s.Name+"="+string(s.Type))
	}
	if strings.Join(got, ",") != "core=path,util=workspace,gated=path" {
		t.Errorf("got %v", got)
	}
}

func TestRemovingTheLastSourceRemovesTheEmptiedTables(t *testing.T) {
	hygiene.Isolate(t)
	p := parse(t, "[project]\nname = \"consumer\"\n\n[tool.uv.sources]\ncore = { path = \"../core\" }\n")
	n, err := p.RemoveUvSources([]string{"core"})
	if err != nil || n != 1 {
		t.Fatalf("%d, %v", n, err)
	}
	if got := string(p.Bytes()); strings.Contains(got, "tool") {
		t.Errorf("an empty header was left:\n%s", got)
	}
}

func TestASiblingSourceAndANonLocalListElementRemain(t *testing.T) {
	hygiene.Isolate(t)
	p := parse(t, `[tool.uv.sources]
core = { path = "../core" }
util = { git = "https://example.com/util" }
gated = [{ index = "internal", marker = "sys_platform == 'win32'" }, { path = "../gated", marker = "sys_platform != 'win32'" }]

[tool.ruff]
line-length = 100
`)
	n, err := p.RemoveUvSources([]string{"core", "gated", "util"})
	if err != nil || n != 2 {
		t.Fatalf("%d, %v", n, err)
	}
	got := string(p.Bytes())
	for _, want := range []string{`util = { git = "https://example.com/util" }`, `index = "internal"`, "[tool.ruff]"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %s in\n%s", want, got)
		}
	}
	for _, gone := range []string{"../core", "../gated"} {
		if strings.Contains(got, gone) {
			t.Errorf("%s remains in\n%s", gone, got)
		}
	}
}

func TestAListOfOnlyLocalElementsAndAnArrayOfTablesAreHandled(t *testing.T) {
	hygiene.Isolate(t)
	p := parse(t, `[project]
name = "consumer"

[tool.uv.sources]
only = [{ path = "../a", marker = "os_name == 'nt'" }, { path = "../b", marker = "os_name != 'nt'" }]

[[tool.uv.sources.tables]]
index = "internal"
marker = "sys_platform == 'win32'"

[[tool.uv.sources.tables]]
path = "../tables"
marker = "sys_platform != 'win32'"
`)
	n, err := p.RemoveUvSources([]string{"only", "tables"})
	if err != nil || n != 2 {
		t.Fatalf("%d, %v", n, err)
	}
	got := string(p.Bytes())
	if strings.Contains(got, "only") || strings.Contains(got, "../tables") || !strings.Contains(got, `index = "internal"`) {
		t.Errorf("got:\n%s", got)
	}
}
