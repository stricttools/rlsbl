package checks

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// fakeProgram puts a shell script named name first on PATH for the rest of
// the test; it appends its arguments to calls.txt beside it.
func fakeProgram(t *testing.T, name, body string) (calls string) {
	t.Helper()
	dir := t.TempDir()
	script := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"${0%/*}/calls.txt\"\n" + body
	if err := os.WriteFile(filepath.Join(dir, name), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return filepath.Join(dir, "calls.txt")
}

func readCalls(t *testing.T, calls string) string {
	t.Helper()
	data, err := os.ReadFile(calls)
	if err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	return string(data)
}

func TestAVersionedMemberWithoutATargetFailsUntilItHasOne(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-targets"), "pass")
	if err := os.Remove(r.Path("gadget/go.mod")); err != nil {
		t.Fatal(err)
	}
	got := runCheck(t, inputs(t, r.Dir), "workspace-targets")
	mustStatus(t, got, "fail")
	mustMention(t, got, "gadget: no release target", `the releasable "gadget" has no target`)
	// The fix: the member's manifest.
	r.Write("gadget/go.mod", "module github.com/acme/repo/gadget\n\ngo 1.26\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-targets"), "pass")
}

func TestAProjectNoMemberDeclaresFailsUntilItIsDeclared(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write("tools/cli/go.mod", "module github.com/acme/repo/tools/cli\n\ngo 1.26\n")
	r.Write("widget/testdata/fixture/go.mod", "module fixture\n")
	r.Write("widget/experiments/probe/go.mod", "module probe\n")
	r.Write(".hidden/go.mod", "module hidden\n")
	r.Write("docs/pyproject.toml", "[tool.ruff]\nline-length = 100\n")
	got := runCheck(t, inputs(t, r.Dir), "workspace-unregistered")
	mustStatus(t, got, "fail")
	mustMention(t, got, "tools/cli holds a go project", "rlsbl monorepo add tools/cli --releasable")
	for _, quiet := range []string{"testdata", "experiments", ".hidden", "docs"} {
		if strings.Contains(got.texts(), quiet) {
			t.Errorf("%s was reported as a project: %s", quiet, got)
		}
	}
	// What `rlsbl monorepo add tools/cli --releasable false` declares.
	r.Write(".strictmetadata/releasables/releasables.toml", workspaceWidgetGadget+"\n[[members]]\npath = \"tools/cli\"\nname = \"cli\"\nreleasable = false\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-unregistered"), "pass")
}

func TestAMemberWhoseDirectoryIsGoneFailsUntilItIsRestored(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	if err := os.RemoveAll(r.Path("gadget")); err != nil {
		t.Fatal(err)
	}
	got := runCheck(t, inputs(t, r.Dir), "workspace-stale-entries")
	mustStatus(t, got, "fail")
	mustMention(t, got, "gadget: its directory gadget does not exist", "rlsbl monorepo remove gadget")
	r.Write("gadget/README.md", "gadget\n")
	got = runCheck(t, inputs(t, r.Dir), "workspace-stale-entries")
	mustStatus(t, got, "fail")
	mustMention(t, got, "gadget holds no manifest")
	// The fix: restore its manifest.
	r.Write("gadget/go.mod", "module github.com/acme/repo/gadget\n\ngo 1.26\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-stale-entries"), "pass")
}

// boundaryDeclarations are the workspace's declarations with a tools
// member, declared as given, and widget's depends_on on it.
func boundaryDeclarations(tools string) string {
	declared := strings.Replace(workspaceWidgetGadget, "name = \"widget\"\nreleasable = \"widget\"\n", "name = \"widget\"\nreleasable = \"widget\"\ndepends_on = [\"tools\"]\n", 1)
	return declared + "\n[[members]]\npath = \"tools\"\nname = \"tools\"\n" + tools
}

func boundaryWorkspace(t *testing.T, tools string) *testsupport.Repo {
	t.Helper()
	r := workspaceRepo(t)
	r.Write(".strictmetadata/releasables/releasables.toml", boundaryDeclarations(tools))
	r.Write("tools/go.mod", "module github.com/acme/repo/tools\n\ngo 1.26\n")
	return r
}

func TestARuntimeDependencyOnADevOnlyMemberFailsUntilTheBoundaryHolds(t *testing.T) {
	hygiene.Isolate(t)
	r := boundaryWorkspace(t, "dev_only = true\nreleasable = false\n")
	got := runCheck(t, inputs(t, r.Dir), "dev-only-boundary")
	mustStatus(t, got, "fail")
	mustMention(t, got, `the member "widget", which is not dev-only, has a runtime dependency on the dev-only member "tools"`)
	// One fix the finding names: declare tools without dev_only.
	r.Write(".strictmetadata/releasables/releasables.toml", boundaryDeclarations("releasable = false\n"))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "dev-only-boundary"), "pass")
}

func TestARuntimeDependencyOnAnUnversionedMemberFailsUntilItIsVersioned(t *testing.T) {
	hygiene.Isolate(t)
	r := boundaryWorkspace(t, "releasable = false\n")
	got := runCheck(t, inputs(t, r.Dir), "unversioned-boundary")
	mustStatus(t, got, "fail")
	mustMention(t, got, `the versioned member "widget" has a runtime dependency on the member "tools"`)
	// The fix the finding names: version tools under a releasable.
	r.Write(".strictmetadata/releasables/releasables.toml", boundaryDeclarations("releasable = \"gadget\"\n"))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "unversioned-boundary"), "pass")
}

// pypiWorkspace is the workspace with widget and gadget as pypi projects.
func pypiWorkspace(t *testing.T) *testsupport.Repo {
	t.Helper()
	return newRepo(t, workspaceWidgetGadget, map[string]string{
		"widget/pyproject.toml":                   "[project]\nname = \"widget\"\nversion = \"1.0.0\"\n",
		"gadget/pyproject.toml":                   "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\n",
		".strictmetadata/releases/widget/version": "1.0.0\n",
		".strictmetadata/releases/gadget/version": "1.0.0\n",
	})
}

func TestAMemberUVCannotSyncFailsUntilItResolves(t *testing.T) {
	hygiene.Isolate(t)
	r := pypiWorkspace(t)
	calls := fakeProgram(t, "uv", "echo 'error: no solution for widget' >&2\nexit 1\n")
	got := runCheck(t, inputs(t, r.Dir), "workspace-unbuildable")
	mustStatus(t, got, "fail")
	mustMention(t, got, "widget: error: no solution for widget")
	if !strings.Contains(readCalls(t, calls), "sync --dry-run") || strings.Contains(readCalls(t, calls), "--all-packages") {
		t.Errorf("without a uv workspace at the root each member syncs alone: %s", readCalls(t, calls))
	}
	fakeProgram(t, "uv", "exit 0\n")
	got = runCheck(t, inputs(t, r.Dir), "workspace-unbuildable")
	mustStatus(t, got, "pass")
	mustMention(t, got, "all 2 pypi member(s) build")
}

func TestAUVWorkspaceAtTheRootSyncsOnce(t *testing.T) {
	hygiene.Isolate(t)
	r := pypiWorkspace(t)
	r.Write("pyproject.toml", "[tool.uv.workspace]\nmembers = [\"widget\", \"gadget\"]\n")
	calls := fakeProgram(t, "uv", "exit 0\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-unbuildable"), "pass")
	if got := strings.TrimSpace(readCalls(t, calls)); got != "sync --all-packages --dry-run" {
		t.Errorf("uv ran %q", got)
	}
}

func TestWorkspaceUnbuildableSkipsWithoutAPypiMember(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "workspace-unbuildable"), "skip")
}

func TestOldLayoutResidueFailsUntilCleanupRemovesIt(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "releasable-residue"), "pass")
	r.Write("widget/.rlsbl/config.json", "{}\n")
	r.Write(".strictmetadata/releases/gizmo/v1.0.0.toml", "format_version = 2\n")
	got := runCheck(t, inputs(t, r.Dir), "releasable-residue")
	mustStatus(t, got, "fail")
	mustMention(t, got, "widget/.rlsbl/: ", "`rlsbl monorepo cleanup` removes it", ".strictmetadata/releases/gizmo/", "does not remove it")
	// What `rlsbl monorepo cleanup` deletes, and the record moved by hand.
	for _, p := range []string{"widget/.rlsbl", ".strictmetadata/releases/gizmo"} {
		if err := os.RemoveAll(r.Path(p)); err != nil {
			t.Fatal(err)
		}
	}
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "releasable-residue"), "pass")
}

func TestOldLayoutResidueOfAStandaloneRepositoryIsReported(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{".rlsbl/config.json": "{}\n"})
	got := runCheck(t, inputs(t, r.Dir), "releasable-residue")
	mustStatus(t, got, "fail")
	mustMention(t, got, ".rlsbl/: ")
}

func TestAMemberWithTestsUnderAnEnclosingConftestFailsUntilItPinsItsRootdir(t *testing.T) {
	hygiene.Isolate(t)
	r := nestedRepo(t, "none", map[string]string{
		"widget/conftest.py":                 "\n",
		"widget/gadget/pyproject.toml":       "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\n",
		"widget/gadget/tests/test_gadget.py": "def test(): pass\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "member-pytest-config")
	mustStatus(t, got, "fail")
	mustMention(t, got, "gadget: add a [tool.pytest.ini_options] table to widget/gadget/pyproject.toml", "widget/conftest.py is loaded")
	r.Write("widget/gadget/pyproject.toml", "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\n\n[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "member-pytest-config"), "pass")
}

func TestMemberPytestConfigSkipsWithoutAConftest(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "member-pytest-config"), "skip")
}

func TestMixedTagSchemesHasNothingToFindWhenEveryFormatIsDeclared(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	got := runCheck(t, inputs(t, r.Dir), "mixed-tag-schemes")
	mustStatus(t, got, "pass")
	mustMention(t, got, fmt.Sprintf("declares its tag_format in %s", ".strictmetadata/releasables/releasables.toml"))
}
