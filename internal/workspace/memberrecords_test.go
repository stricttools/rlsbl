package workspace

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// A repository absorbed into a workspace brings its own records inside its
// member, where nothing reads them: they are residue, and cleanup removes
// all but the records of what a subject released. What else a member keeps
// under .strictmetadata/ (a schema dump, a stub go.mod) is not rlsbl's
// record and is not residue.
func TestARepositorysOwnRecordsInsideAMemberAreResidue(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeFixture(t, root, "kernel/.strictmetadata/releasables/releasables.toml", "format_version = 1\n")
	writeFixture(t, root, "kernel/.strictmetadata/transitions/transitions.jsonl", "")
	writeFixture(t, root, "kernel/.strictmetadata/lifecycle-and-license/lifecycle-and-license.toml", "format_version = 1\n")
	writeFixture(t, root, "kernel/.strictmetadata/changelog/gizmo/0.1.0.jsonl", "")
	writeFixture(t, root, "kernel/.strictmetadata/.cli-schema/schema.json", "{}\n")
	writeFixture(t, root, "kernel/.strictmetadata/go.mod", "module private.invalid/rlsbl-private\n")
	w := newWorkspace(t, root, nested)
	paths, removable := residuePaths(t, w, nil)
	want := []string{
		"kernel/.strictmetadata/changelog",
		"kernel/.strictmetadata/lifecycle-and-license",
		"kernel/.strictmetadata/releasables",
		"kernel/.strictmetadata/transitions",
	}
	if strings.Join(paths, " ") != strings.Join(want, " ") {
		t.Fatalf("residue = %v, want %v", paths, want)
	}
	for _, p := range want[1:] {
		if !removable[p] {
			t.Errorf("cleanup does not remove %s", p)
		}
	}
	if removable["kernel/.strictmetadata/changelog"] {
		t.Error("cleanup would delete the record of what a subject released")
	}
}
