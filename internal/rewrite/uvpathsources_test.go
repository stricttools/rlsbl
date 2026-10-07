package rewrite_test

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/rewrite"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// depFloorsChecks is a checks registry holding the dep-floors check, which
// the options registry renders as rlsbl:dep-floors.
const depFloorsChecks = `app = "rlsbl"

[checks.dep-floors]
description = "Every internal dependency declares a floor."
subject = "dependencies"
tags = ["dependencies"]
severity = "error"
depends_on = []
`

func optionsRegistry(t *testing.T) *options.Registry {
	t.Helper()
	doc, err := options.RenderRegistry([]byte(depFloorsChecks))
	if err != nil {
		t.Fatal(err)
	}
	reg, err := options.ParseRegistry(doc)
	if err != nil {
		t.Fatal(err)
	}
	return reg
}

const pathSourcedManifest = `[project]
name = "portal"
version = "0.1.0"
dependencies = [
    "core",
    "util @ file:///abs/util",
    "requests>=2",  # from the registry
]

[project.optional-dependencies]
cli = ["core[cli]; python_version >= '3.11'"]

[dependency-groups]
dev = ["devkit"]

[tool.uv.sources]
core = { workspace = true }
devkit = { path = "../devkit", editable = true }

[tool.ruff]
line-length = 100
`

const pathSourcedLock = `version = 1

[[package]]
name = "portal"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "core"
version = "1.4.0"
source = { editable = "../core" }

[[package]]
name = "util"
version = "0.3.0"
source = { directory = "/abs/util" }

[[package]]
name = "devkit"
version = "2.0.1"
source = { editable = "../devkit" }
`

// pythonProject is a standalone project portal resolving three
// dependencies from local checkouts.
func pythonProject(t *testing.T) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", standaloneDeclarations)
	repo.Write("pyproject.toml", pathSourcedManifest)
	repo.Write("uv.lock", pathSourcedLock)
	return repo
}

// pypiDocument is PyPI's project document listing versions.
func pypiDocument(name string, versions ...string) testsupport.HTTPAnswer {
	var releases []string
	for _, v := range versions {
		releases = append(releases, fmt.Sprintf("%q: []", v))
	}
	body := fmt.Sprintf(`{"info": {"version": %q}, "releases": {%s}}`, versions[len(versions)-1], strings.Join(releases, ", "))
	return testsupport.HTTPAnswer{Method: "GET", URL: "https://pypi.org/pypi/" + name + "/json", Status: 200, Body: body}
}

func publishedAnswers() []testsupport.HTTPAnswer {
	return []testsupport.HTTPAnswer{
		pypiDocument("core", "1.3.0", "1.4.0"),
		pypiDocument("devkit", "2.0.1"),
		pypiDocument("util", "0.3.0"),
	}
}

func convert(t *testing.T, root, dir string, dryRun bool, client *http.Client) (strictcli.Result, error) {
	t.Helper()
	reg := optionsRegistry(t)
	var runErr error
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes(), HTTPClient: client}, func(ctx *strictcli.Context) error {
		runErr = rewrite.RunUvPathSources(ctx, root, dir, reg)
		return runErr
	})
	return res, runErr
}

func TestTheDryRunPlansEachDependencyAndWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo := pythonProject(t)
	fake := testsupport.NewFakeHTTP(t, publishedAnswers()...)
	res, err := convert(t, repo.Dir, repo.Dir, true, fake.Client())
	if err != nil {
		t.Fatalf("%v\n%s", err, res.Stderr)
	}
	requireContains(t, res.Stdout,
		"core: convert: 3 entries -> core>=1.4.0",
		"[tool.uv.sources].core: workspace source",
		"[optional-dependencies.cli]: declares this dependency",
		"floor read from uv.lock (beside the manifest)",
		"published on PyPI: yes (1.4.0)",
		"apply would rewrite 2 dependency entries to 'core>=1.4.0'.",
		"apply would delete [tool.uv.sources].core.",
		"devkit: convert: 2 entries -> devkit>=2.0.1",
		"util: convert: 1 entry -> util>=0.3.0",
		declarations.ReleasablesFile+": declare-floors: the member \"root\"'s internal_dep_floors would gain core, devkit, util",
		".strictmetadata/options/dependencies.toml: switch-on-dep-floors: rlsbl:dep-floors would be switched on",
	)
	if read(t, repo.Path("pyproject.toml")) != pathSourcedManifest {
		t.Error("the dry run wrote the manifest")
	}
	if _, err := os.Stat(repo.Path(".strictmetadata/options")); err == nil {
		t.Error("the dry run wrote an options entry")
	}
	for _, u := range fake.URLs() {
		if !strings.HasSuffix(u, "/json") || strings.Count(strings.TrimPrefix(u, "https://pypi.org/pypi/"), "/") != 1 {
			t.Errorf("a request named something besides a project document: %s", u)
		}
	}
}

func TestTheConversionFloorsDeclaresAndSwitchesTheOptionOn(t *testing.T) {
	hygiene.Isolate(t)
	repo := pythonProject(t)
	res, err := convert(t, repo.Dir, repo.Dir, false, testsupport.NewFakeHTTP(t, publishedAnswers()...).Client())
	if err != nil {
		t.Fatalf("%v\n%s", err, res.Stderr)
	}
	manifest := read(t, repo.Path("pyproject.toml"))
	requireContains(t, manifest,
		`"core>=1.4.0"`,
		`"util>=0.3.0"`,
		`"requests>=2",  # from the registry`,
		`cli = ["core[cli]>=1.4.0; python_version >= '3.11'"]`,
		`dev = ["devkit>=2.0.1"]`,
		"[tool.ruff]\nline-length = 100",
	)
	if strings.Contains(manifest, "tool.uv") || strings.Contains(manifest, "file://") {
		t.Errorf("a source survived:\n%s", manifest)
	}
	d, err := declarations.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(d.RootMember().InternalDepFloors, ","); got != "core,devkit,util" {
		t.Errorf("internal_dep_floors: %s", got)
	}
	o, err := options.Load(optionsRegistry(t), repo.Dir, d)
	if err != nil {
		t.Fatal(err)
	}
	if v, err := o.Value(options.DepFloors, "."); err != nil || v.Value != "error" {
		t.Errorf("%+v, %v", v, err)
	}
	requireContains(t, res.Stdout, "Converted 3 path-sourced dependencies.")

	res, err = convert(t, repo.Dir, repo.Dir, false, testsupport.NewFakeHTTP(t).Client())
	if err != nil {
		t.Fatalf("%v\n%s", err, res.Stderr)
	}
	requireContains(t, res.Stdout, "Nothing to convert")
}

func TestAnOptionAlreadyOnAndFloorsAlreadyDeclaredAreLeftAlone(t *testing.T) {
	hygiene.Isolate(t)
	repo := pythonProject(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", standaloneDeclarations+"internal_dep_floors = [\"core\", \"devkit\", \"util\"]\n")
	repo.Write(".strictmetadata/options/manifest.toml", "owner = \"strictspec\"\n")
	repo.Write(".strictmetadata/options/dependencies.toml", "format_version = 1\n\n[[entry]]\nid = \"rlsbl:dep-floors\"\ncurrent = \"warn\"\nideal = \"error\"\nreason = \"adopted\"\n")
	res, err := convert(t, repo.Dir, repo.Dir, true, testsupport.NewFakeHTTP(t, publishedAnswers()...).Client())
	if err != nil {
		t.Fatalf("%v\n%s", err, res.Stderr)
	}
	requireContains(t, res.Stdout, "floors-already-declared", "dep-floors-already-on: rlsbl:dep-floors is already warn")
}

func TestAnUnpublishedFloorRefusesNamingTheReleaseFirstFix(t *testing.T) {
	hygiene.Isolate(t)
	repo := pythonProject(t)
	fake := testsupport.NewFakeHTTP(t, pypiDocument("core", "1.3.0"), pypiDocument("devkit", "2.0.1"), pypiDocument("util", "0.3.0"))
	_, err := convert(t, repo.Dir, repo.Dir, true, fake.Client())
	if err == nil || !strings.Contains(err.Error(), "core 1.4.0 is not published on PyPI") || !strings.Contains(err.Error(), "Release core first") {
		t.Fatalf("%v", err)
	}
	// The probe asked the project document, never the one version.
	if urls := fake.URLs(); len(urls) != 1 || urls[0] != "https://pypi.org/pypi/core/json" {
		t.Errorf("requests: %v", urls)
	}
	gone := testsupport.NewFakeHTTP(t, testsupport.HTTPAnswer{Method: "GET", URL: "https://pypi.org/pypi/core/json", Status: 404})
	if _, err := convert(t, repo.Dir, repo.Dir, true, gone.Client()); err == nil || !strings.Contains(err.Error(), "is not published on PyPI") {
		t.Fatalf("a project PyPI does not have: %v", err)
	}
}

func TestAnUnansweredProbeRefusesRatherThanAssuming(t *testing.T) {
	hygiene.Isolate(t)
	repo := pythonProject(t)
	fake := testsupport.NewFakeHTTP(t, testsupport.HTTPAnswer{Method: "GET", URL: "https://pypi.org/pypi/core/json", Status: 503})
	_, err := convert(t, repo.Dir, repo.Dir, true, fake.Client())
	if err == nil || !strings.Contains(err.Error(), "could not determine whether it is published") {
		t.Fatalf("%v", err)
	}
}

func TestTheLockMustResolveEveryConvertedDependency(t *testing.T) {
	hygiene.Isolate(t)
	repo := pythonProject(t)
	repo.Write("uv.lock", strings.Replace(pathSourcedLock, "name = \"devkit\"", "name = \"other\"", 1))
	_, err := convert(t, repo.Dir, repo.Dir, true, testsupport.NewFakeHTTP(t).Client())
	if err == nil || !strings.Contains(err.Error(), "devkit: uv.lock does not resolve this package") {
		t.Fatalf("%v", err)
	}
	if err := os.Remove(repo.Path("uv.lock")); err != nil {
		t.Fatal(err)
	}
	_, err = convert(t, repo.Dir, repo.Dir, true, testsupport.NewFakeHTTP(t).Client())
	if err == nil || !strings.Contains(err.Error(), "no uv.lock for") || !strings.Contains(err.Error(), "Run `uv lock`") {
		t.Fatalf("%v", err)
	}
	if err := os.Remove(repo.Path("pyproject.toml")); err != nil {
		t.Fatal(err)
	}
	_, err = convert(t, repo.Dir, repo.Dir, true, testsupport.NewFakeHTTP(t).Client())
	if err == nil || !strings.Contains(err.Error(), "no pyproject.toml at") {
		t.Fatalf("%v", err)
	}
}

// movingTransport answers PyPI from canned documents and, on its first
// request, adds one more core entry to the manifest: the tree moves after
// the preview counted it.
type movingTransport struct {
	t        *testing.T
	manifest string
	moved    bool
}

func (m *movingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if !m.moved {
		m.moved = true
		testsupport.WriteFile(m.t, m.manifest, strings.Replace(read(m.t, m.manifest), `dev = ["devkit"]`, `dev = ["devkit", "core"]`, 1))
	}
	for _, a := range publishedAnswers() {
		if a.URL == req.URL.String() {
			return &http.Response{StatusCode: a.Status, Status: "200 OK", Header: http.Header{}, Body: io.NopCloser(strings.NewReader(a.Body)), Request: req}, nil
		}
	}
	return nil, fmt.Errorf("no answer for %s", req.URL)
}

func TestACountThatMovedAbortsWithoutWritingThatDependency(t *testing.T) {
	hygiene.Isolate(t)
	repo := pythonProject(t)
	transport := &movingTransport{t: t, manifest: repo.Path("pyproject.toml")}
	res, err := convert(t, repo.Dir, repo.Dir, false, &http.Client{Transport: transport})
	if err == nil || !strings.Contains(err.Error(), "the preview counted 3 occurrence(s) in core but it now has 4") || !strings.Contains(err.Error(), "Nothing had been written by this run before the failure.") {
		t.Fatalf("%v\n%s", err, res.Stdout)
	}
	if strings.Contains(read(t, repo.Path("pyproject.toml")), ">=1.4.0") {
		t.Error("the moved dependency was written")
	}
}

func TestAWorkspaceMemberReadsTheRootLockAndScopesItsEntry(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".strictmetadata/releasables/releasables.toml", `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "widget"
tag_format = "widget@v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "packages/widget"
name = "widget"
releasable = "widget"
`)
	repo.Write("pyproject.toml", "[tool.uv.workspace]\nmembers = [\"packages/*\"]\n")
	repo.Write("uv.lock", "version = 1\n\n[[package]]\nname = \"widget\"\nversion = \"0.1.0\"\nsource = { editable = \"packages/widget\" }\n\n[[package]]\nname = \"core\"\nversion = \"1.4.0\"\nsource = { editable = \"packages/core\" }\n")
	repo.Write("packages/widget/pyproject.toml", "[project]\nname = \"widget\"\nversion = \"0.1.0\"\ndependencies = [\"core\"]\n\n[tool.uv.sources]\ncore = { workspace = true }\n")
	member := repo.Path("packages/widget")
	res, err := convert(t, repo.Dir, member, false, testsupport.NewFakeHTTP(t, pypiDocument("core", "1.4.0")).Client())
	if err != nil {
		t.Fatalf("%v\n%s", err, res.Stderr)
	}
	requireContains(t, read(t, repo.Path("packages/widget/pyproject.toml")), `dependencies = ["core>=1.4.0"]`)
	requireContains(t, read(t, repo.Path(".strictmetadata/options/dependencies.toml")), `scope = "packages/widget"`)
	d, err := declarations.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	m, _ := d.MemberAt("packages/widget")
	if strings.Join(m.InternalDepFloors, ",") != "core" {
		t.Errorf("%+v", m)
	}
}
