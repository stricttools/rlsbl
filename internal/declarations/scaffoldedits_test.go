package declarations

import (
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// standaloneCommented is a standalone repository's declarations with
// comments the scaffold edits must keep.
const standaloneCommented = `# Release declarations for widget.
format_version = 1
repository_layout = "standalone"
release_branches = ["main"]

[[releasables]]
name = "widget" # the one releasable
tag_format = "v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
releasable = "widget"
`

func TestSettingThePublishModeKeepsEverythingElse(t *testing.T) {
	hygiene.Isolate(t)
	text, d := edit(t, standaloneCommented, func(ed *Editor) error { return ed.SetPublishMode("widget", PublishCI) })
	if d.Releasables[0].PublishMode != PublishCI {
		t.Fatalf("publish mode = %q", d.Releasables[0].PublishMode)
	}
	if want := strings.Replace(standaloneCommented, `publish_mode = "none"`, `publish_mode = "ci"`, 1); text != want {
		t.Fatalf("got:\n%s\nwant:\n%s", text, want)
	}
	ed, err := NewEditor([]byte(standaloneCommented))
	if err != nil {
		t.Fatal(err)
	}
	if err := ed.SetPublishMode("gadget", PublishCI); err == nil || !strings.Contains(err.Error(), "widget") {
		t.Fatalf("an undeclared releasable: %v", err)
	}
	if err := ed.SetPublishMode("widget", "sometimes"); err == nil {
		t.Fatal("an unknown publish mode was accepted")
	}
}

func TestDeclaringAMembersTargets(t *testing.T) {
	hygiene.Isolate(t)
	targets := []Target{{Name: TargetGo}, {Name: TargetNPM, Path: "npm"}}
	text, d := edit(t, standaloneCommented, func(ed *Editor) error { return ed.SetMemberTargets(".", targets) })
	if !reflect.DeepEqual(d.RootMember().Targets, targets) {
		t.Fatalf("targets = %+v\n%s", d.RootMember().Targets, text)
	}
	if !strings.HasPrefix(text, "# Release declarations for widget.\n") || !strings.Contains(text, `name = "widget" # the one releasable`) {
		t.Fatalf("the comments were lost:\n%s", text)
	}
	ed, err := NewEditor([]byte(standaloneCommented))
	if err != nil {
		t.Fatal(err)
	}
	if err := ed.SetMemberTargets(".", nil); err == nil {
		t.Fatal("an empty target list was accepted")
	}
}
