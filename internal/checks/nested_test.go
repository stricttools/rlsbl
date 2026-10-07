package checks

import (
	"fmt"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// nestedWorkspace declares gadget nested inside widget, under a dev-node
// root; widget's publish mode is filled in.
const nestedWorkspace = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "widget"
tag_format = "widget/v{version}"
publish_mode = "%s"

[[releasables]]
name = "gadget"
tag_format = "gadget/v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "widget"
name = "widget"
releasable = "widget"

[[members]]
path = "widget/gadget"
name = "gadget"
releasable = "gadget"
`

func nestedRepo(t *testing.T, widgetMode string, files map[string]string) *testsupport.Repo {
	t.Helper()
	all := map[string]string{
		".strictmetadata/releases/widget/version": "1.0.0\n",
		".strictmetadata/releases/gadget/version": "1.0.0\n",
	}
	for k, v := range files {
		all[k] = v
	}
	return newRepo(t, fmt.Sprintf(nestedWorkspace, widgetMode), all)
}

const widgetPyproject = "[project]\nname = \"widget\"\nversion = \"1.0.0\"\n"

func TestARunnerCollectingANestedMemberFailsUntilScaffoldExcludesIt(t *testing.T) {
	hygiene.Isolate(t)
	r := nestedRepo(t, "none", map[string]string{
		"widget/pyproject.toml":        widgetPyproject,
		"widget/gadget/pyproject.toml": "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "nested-member-runner-exclusion")
	mustStatus(t, got, "fail")
	mustMention(t, got, "widget/pyproject.toml must exclude --ignore=gadget", "Run `rlsbl scaffold` in widget")
	// What `rlsbl scaffold` merges into the member's pyproject.toml.
	r.Write("widget/pyproject.toml", widgetPyproject+"\n[tool.pytest.ini_options]\naddopts = \"--ignore=gadget\"\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "nested-member-runner-exclusion"), "pass")
}

func TestTheRootMembersRunnerExclusionsAreWrittenByHand(t *testing.T) {
	hygiene.Isolate(t)
	r := nestedRepo(t, "none", map[string]string{
		"pyproject.toml":        "[project]\nname = \"tools\"\nversion = \"0.1.0\"\n",
		"widget/pyproject.toml": widgetPyproject + "\n[tool.pytest.ini_options]\naddopts = [\"--ignore=gadget\"]\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "nested-member-runner-exclusion")
	mustStatus(t, got, "fail")
	mustMention(t, got, "root:", "Write them by hand", "--ignore=widget --ignore=widget/gadget", "[tool.pytest.ini_options]")
	r.Write("pyproject.toml", "[project]\nname = \"tools\"\nversion = \"0.1.0\"\n\n[tool.pytest.ini_options]\naddopts = \"--ignore=widget --ignore=widget/gadget\"\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "nested-member-runner-exclusion"), "pass")
}

func TestANestedMemberDeclaringAUVWorkspaceSourceFailsUntilTheRootDeclaresIt(t *testing.T) {
	hygiene.Isolate(t)
	r := nestedRepo(t, "none", map[string]string{
		"widget/pyproject.toml":        widgetPyproject + "\n[tool.uv.sources]\nhelper = { workspace = true }\n",
		"widget/gadget/pyproject.toml": "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\n\n[tool.uv.sources]\nwidget = { workspace = true }\nother = { path = \"../other\" }\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "nested-member-uv-sources")
	mustStatus(t, got, "fail")
	mustMention(t, got, "widget/gadget/pyproject.toml declares widget as", "Move the entries to [tool.uv.sources] in pyproject.toml")
	if strings.Contains(got.texts(), "helper") {
		t.Errorf("a member nested in the root alone was judged: %s", got)
	}
	// The fix: the entry moves to the repository root's pyproject.toml.
	r.Write("widget/gadget/pyproject.toml", "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\n")
	r.Write("pyproject.toml", "[tool.uv.sources]\nwidget = { workspace = true }\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "nested-member-uv-sources"), "pass")
}

func TestAModuleZipCarryingANestedMembersFilesFailsUntilItIsLeftOut(t *testing.T) {
	hygiene.Isolate(t)
	r := nestedRepo(t, "ci", map[string]string{
		"widget/go.mod":                "module github.com/acme/repo/widget\n\ngo 1.26\n",
		"widget/gadget/pyproject.toml": "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "nested-member-upload-contents")
	mustStatus(t, got, "fail")
	mustMention(t, got, "the Go module zip would ship widget/gadget/pyproject.toml", `the member "gadget" owns`, "give it a go.mod of its own")
	// The fix the finding names: a go.mod of its own leaves it out of the zip.
	r.CommitFile("widget/gadget/go.mod", "module github.com/acme/repo/widget/gadget\n\ngo 1.26\n", "Give gadget a module of its own")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "nested-member-upload-contents"), "pass")
}

func TestAMemberPublishingNothingHasNoUploadToJudge(t *testing.T) {
	hygiene.Isolate(t)
	r := nestedRepo(t, "none", map[string]string{
		"widget/go.mod":                "module github.com/acme/repo/widget\n\ngo 1.26\n",
		"widget/gadget/pyproject.toml": "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\n",
	})
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "nested-member-upload-contents"), "pass")
}
