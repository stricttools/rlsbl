package monorepo

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// sharedWidget declares a workspace whose widget releasable has two
// members, widget and gadget, and whose gadget depends on widget.
const sharedWidget = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "widget"
tag_format = "{name}@v{version}"
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
library = true

[[members]]
path = "apps/gadget"
name = "gadget"
releasable = "widget"
depends_on = ["widget"]
`

var sharedWidgetFiles = map[string]string{
	"packages/widget/go.mod": "module github.com/acme/widget\n\ngo 1.26\n",
	"apps/gadget/go.mod":     "module github.com/acme/gadget\n\ngo 1.26\n",
}

func removing(repo *testsupport.Repo, path string) func(e *strictcli.Effects, say func(string)) error {
	return func(e *strictcli.Effects, say func(string)) error {
		ws, err := workspace.Load(repo.Dir)
		if err != nil {
			return err
		}
		_, err = Remove(e, ws, path)
		return err
	}
}

func TestRemoveDeletesTheDeclarationAndLeavesTheFiles(t *testing.T) {
	hygiene.Isolate(t)
	repo := newWorkspace(t, sharedWidget, sharedWidgetFiles)
	mustRun(t, strictcli.EffectMutating, false, removing(repo, "apps/gadget"))
	ws := load(t, repo)
	if _, ok := ws.Declarations.Member("gadget"); ok {
		t.Fatal("the member is still declared")
	}
	if !exists(repo, "apps/gadget/go.mod") {
		t.Error("the member's files were removed")
	}
	if !strings.Contains(readText(t, repo, declarations.ReleasablesFile), "library = true") {
		t.Error("the edit did not keep the other members' declarations")
	}
}

func TestRemoveNamesTheMembersOfAPathItDoesNotDeclare(t *testing.T) {
	hygiene.Isolate(t)
	repo := newWorkspace(t, sharedWidget, sharedWidgetFiles)
	mustFail(t, strictcli.EffectMutating, false, removing(repo, "apps/gadget/"), `no member is declared at "apps/gadget/"`, `"apps/gadget" (gadget)`, `"packages/widget" (widget)`)
	mustRun(t, strictcli.EffectMutating, false, removing(repo, "apps/gadget"))
}

func TestRemoveRefusesTheRootMember(t *testing.T) {
	hygiene.Isolate(t)
	repo := newWorkspace(t, sharedWidget, sharedWidgetFiles)
	mustFail(t, strictcli.EffectMutating, false, removing(repo, "."), "cannot be removed")
}

// A member another member depends on is refused until the dependency is
// deleted from the dependent's depends_on.
func TestRemoveRefusesAMemberAnotherDependsOn(t *testing.T) {
	hygiene.Isolate(t)
	repo := newWorkspace(t, sharedWidget, sharedWidgetFiles)
	mustFail(t, strictcli.EffectMutating, false, removing(repo, "packages/widget"), `the members gadget name "widget" in their depends_on`)
	repo.Write(declarations.ReleasablesFile, strings.Replace(sharedWidget, "depends_on = [\"widget\"]\n", "", 1))
	mustRun(t, strictcli.EffectMutating, false, removing(repo, "packages/widget"))
}

// The only member of a releasable is refused, and versioning another member
// under the releasable clears the refusal.
func TestRemoveRefusesTheOnlyMemberOfAReleasable(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	testsupport.FakeGH(t, topicsAnswer)
	repo := newWorkspace(t, rootAndWidget, addFiles)
	mustFail(t, strictcli.EffectMutating, false, removing(repo, "packages/widget"), `the only member versioned under the releasable "widget"`, "rlsbl monorepo add <path> --releasable widget")
	req := gadgetRequest(repo)
	req.Releasable, req.TagFormat, req.PublishMode, req.License = "widget", "", "", ""
	mustRun(t, strictcli.EffectMutating, false, adding(req))
	mustRun(t, strictcli.EffectMutating, false, removing(repo, "packages/widget"))
}

// A member the lifecycle-and-license record holds an open period of is
// refused until the period is closed.
func TestRemoveRefusesAMemberTheRecordHoldsOpenEntriesOf(t *testing.T) {
	hygiene.Isolate(t)
	record := "format_version = 1\n\n[[lifecycle]]\nsubject = \"gadget\"\nstatus = \"active\"\nfrom = 2026-01-01\nreason = \"a member with a lifecycle of its own\"\n"
	repo := newWorkspace(t, sharedWidget, sharedWidgetFiles)
	repo.Write(lifecycle.RecordFile, record)
	mustFail(t, strictcli.EffectMutating, false, removing(repo, "apps/gadget"), lifecycle.RecordFile, "an open lifecycle period", "close or delete them first")
	repo.Write(lifecycle.RecordFile, strings.Replace(record, "from = 2026-01-01\n", "from = 2026-01-01\nuntil = 2026-10-07\n", 1))
	mustRun(t, strictcli.EffectMutating, false, removing(repo, "apps/gadget"))
}

func TestListReportsWhatEachMemberDeclares(t *testing.T) {
	hygiene.Isolate(t)
	repo := newWorkspace(t, sharedWidget, sharedWidgetFiles)
	got := List(load(t, repo))
	if len(got) != 3 {
		t.Fatalf("members %+v", got)
	}
	root, widget, gadget := got[0], got[1], got[2]
	if root.Name != "root" || root.Releasable != nil || !root.DevOnly {
		t.Errorf("root %+v", root)
	}
	if widget.Path != "packages/widget" || widget.Releasable == nil || *widget.Releasable != "widget" || !widget.Library {
		t.Errorf("widget %+v", widget)
	}
	if gadget.Library || gadget.DevOnly || gadget.TestOnly {
		t.Errorf("gadget %+v", gadget)
	}
}
