package checks

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

const gadgetRequiring = "module github.com/acme/repo/gadget\n\ngo 1.26\n\nrequire github.com/acme/repo/widget %s\n"

func TestARequireBelowASiblingsLatestReleaseFailsUntilItIsRaised(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	writeArchive(t, r, "widget", "1.0.0", r.Head(), map[string]string{"widget": strings.Repeat("e", 40)})
	r.Write("gadget/go.mod", strings.Replace(gadgetRequiring, "%s", "v0.9.0", 1))
	got := runCheck(t, inputs(t, r.Dir), "go-workspace-require-current")
	mustStatus(t, got, "fail")
	mustMention(t, got, "gadget/go.mod requires github.com/acme/repo/widget v0.9.0", "`go get github.com/acme/repo/widget@v1.0.0` in gadget")
	// What `go get github.com/acme/repo/widget@v1.0.0` writes into go.mod.
	r.Write("gadget/go.mod", strings.Replace(gadgetRequiring, "%s", "v1.0.0", 1))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-workspace-require-current"), "pass")
}

func TestARequireOfAnUnreleasedSiblingIsNotJudged(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write("gadget/go.mod", strings.Replace(gadgetRequiring, "%s", "v0.0.1", 1))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-workspace-require-current"), "pass")
}

func TestAReplaceIntoTheWorkspaceFailsUntilItIsDropped(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write("gadget/go.mod", strings.Replace(gadgetRequiring, "%s", "v1.0.0", 1)+"\nreplace github.com/acme/repo/widget => ../widget\n")
	got := runCheck(t, inputs(t, r.Dir), "go-workspace-replace")
	mustStatus(t, got, "fail")
	mustMention(t, got, "replaces github.com/acme/repo/widget with ../widget", "`go work init` and `go work use gadget widget`", "go mod edit -dropreplace=github.com/acme/repo/widget")
	// With a go.work the fix is to use the modules, not to create one.
	r.Write("go.work", "go 1.26\n")
	mustMention(t, runCheck(t, inputs(t, r.Dir), "go-workspace-replace"), "run `go work use gadget widget`")
	// What `go mod edit -dropreplace` leaves.
	r.Write("gadget/go.mod", strings.Replace(gadgetRequiring, "%s", "v1.0.0", 1))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-workspace-replace"), "pass")
}

func TestAReplaceOutsideTheWorkspaceIsNotJudged(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write("gadget/go.mod", strings.Replace(gadgetRequiring, "%s", "v1.0.0", 1)+"\nreplace github.com/acme/repo/widget => ../../elsewhere/widget\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-workspace-replace"), "pass")
}
