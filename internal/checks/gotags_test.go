package checks

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestAPathTagFormatNamingNoGoMemberFailsUntilItNamesOne(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "path-tag-format-go-member"), "pass")
	wrong := strings.Replace(workspaceWidgetGadget, `tag_format = "gadget/v{version}"`, `tag_format = "gizmo/v{version}"`, 1)
	r.Write(".strictmetadata/releasables/releasables.toml", wrong)
	got := runCheck(t, inputs(t, r.Dir), "path-tag-format-go-member")
	mustStatus(t, got, "fail")
	mustMention(t, got, `"gizmo/v{version}"`, `"gadget/v{version}"`, `"{name}@v{version}"`)
	// The fix: a tag format naming the releasable's Go member.
	r.Write(".strictmetadata/releasables/releasables.toml", workspaceWidgetGadget)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "path-tag-format-go-member"), "pass")
}

func TestTheGoTagChecksOfAWorkspaceSkipAStandaloneRepository(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	for _, name := range []string{"path-tag-format-go-member", "go-companion-tags"} {
		got := runCheck(t, inputs(t, r.Dir), name)
		mustStatus(t, got, "skip")
		mustMention(t, got, "not a workspace")
	}
}

func TestAMajorVersionSuffixFailsUntilTheModulePathDropsIt(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"go.mod": "module github.com/acme/portal/v2\n\ngo 1.26\n"})
	got := runCheck(t, inputs(t, r.Dir), "go-module-major-suffix")
	mustStatus(t, got, "fail")
	mustMention(t, got, "major version 2", "rlsbl rewrite go-module-path --from-module github.com/acme/portal/v2 --to-module github.com/acme/portal")
	// What the rewrite does to go.mod.
	r.Write("go.mod", "module github.com/acme/portal\n\ngo 1.26\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-module-major-suffix"), "pass")
}

func TestAPathElementThatOnlyLooksLikeAMajorIsNoSuffix(t *testing.T) {
	hygiene.Isolate(t)
	for _, module := range []string{"github.com/acme/portal/v1", "github.com/acme/portal/v0", "github.com/acme/portal/v2x"} {
		r := portalRepo(t, "none", map[string]string{"go.mod": "module " + module + "\n\ngo 1.26\n"})
		mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-module-major-suffix"), "pass")
	}
}

// widgetAtSign is the workspace with widget tagged {name}@v{version} and
// publishing from CI, so its Go member owes a companion path tag.
var widgetAtSign = strings.Replace(widgetPublishing, `tag_format = "widget/v{version}"`, `tag_format = "{name}@v{version}"`, 1)

func TestAMissingCompanionTagOfTheLatestReleaseWarnsUntilItIsPushed(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write(".strictmetadata/releasables/releasables.toml", widgetAtSign)
	r.Git("add", "-A")
	r.Git("commit", "-q", "-m", "Tag widget with its name")
	released := r.Head()
	writeArchive(t, r, "widget", "1.0.0", released, map[string]string{"widget": strings.Repeat("e", 40)})
	got := runCheck(t, inputs(t, r.Dir), "go-companion-tags")
	mustStatus(t, got, "warn")
	mustMention(t, got, "widget/v1.0.0", "rlsbl release reconcile")
	// What the reconcile's apply creates.
	r.Git("tag", "widget/v1.0.0", released)
	got = runCheck(t, inputs(t, r.Dir), "go-companion-tags")
	mustStatus(t, got, "pass")
}

func TestAReleasableWithoutAReleaseOwesNoCompanionTag(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	r.Write(".strictmetadata/releasables/releasables.toml", widgetAtSign)
	got := runCheck(t, inputs(t, r.Dir), "go-companion-tags")
	mustStatus(t, got, "skip")
	mustMention(t, got, "no latest release owes")
}

func TestACompanionEqualToThePrimaryTagIsNotOwedTwice(t *testing.T) {
	hygiene.Isolate(t)
	r := newRepo(t, widgetPublishing, map[string]string{
		"widget/go.mod":  "module github.com/acme/repo/widget\n\ngo 1.26\n",
		"widget/VERSION": "1.0.0\n",
		"gadget/go.mod":  "module github.com/acme/repo/gadget\n\ngo 1.26\n",
		"gadget/VERSION": "1.0.0\n",
		".strictmetadata/releases/widget/version": "1.0.0\n",
		".strictmetadata/releases/gadget/version": "1.0.0\n",
	})
	writeArchive(t, r, "widget", "1.0.0", r.Head(), map[string]string{"widget": strings.Repeat("e", 40)})
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-companion-tags"), "skip")
}

// widgetMovedRecord licenses widget and records that its companion tags
// (widget/v*) belonged to an identity that closed on 2026-05-01.
const widgetMovedRecord = `format_version = 1

[[lifecycle]]
subject = "widget"
status = "active"
from = 2026-01-01
reason = "first release"

[[licenses]]
subject = "widget"
license = "MIT"
from = 2026-01-01
reason = "chosen by the owner"

[[identities]]
subject = "widget"
facet = "go-module-path"
value = "github.com/acme/oldrepo/widget"
registry = "go"
tag_patterns = ["widget/v*"]
from = 2026-01-01
until = 2026-05-01
reason = "the first home"
`

func TestACompanionTagAClosedIdentityOwnsIsNotOwed(t *testing.T) {
	hygiene.Isolate(t)
	t.Setenv("GIT_COMMITTER_DATE", "2026-03-01T12:00:00Z")
	t.Setenv("GIT_AUTHOR_DATE", "2026-03-01T12:00:00Z")
	r := workspaceRepo(t)
	r.Write(".strictmetadata/releasables/releasables.toml", widgetAtSign)
	r.Write(recordFile, widgetMovedRecord)
	r.Git("add", "-A")
	r.Git("commit", "-q", "-m", "Tag widget with its name")
	writeArchive(t, r, "widget", "1.0.0", r.Head(), map[string]string{"widget": strings.Repeat("e", 40)})
	got := runCheck(t, inputs(t, r.Dir), "go-companion-tags")
	mustStatus(t, got, "pass")
	mustMention(t, got, "widget/v1.0.0", "github.com/acme/oldrepo/widget")
}
