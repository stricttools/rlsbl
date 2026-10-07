package releaserecord_test

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
)

func TestDropReleaseCommitTurnsARecordedArchiveUnrecoverable(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	rel := writeArchiveWith(t, root, "0.1.0", "recorded", strings.Repeat("a", 40), "shipped_as = \"portal-v0.1.0\"\n")
	writeArchive(t, root, "0.2.0", "unrecoverable", "")
	writeArchive(t, root, "0.3.0", "never-released", "")
	var changed, again bool
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		var err error
		if changed, err = releaserecord.DropReleaseCommit(e, root, dir, version(t, "0.1.0")); err != nil {
			return err
		}
		again, err = releaserecord.DropReleaseCommit(e, root, dir, version(t, "0.2.0"))
		return err
	})
	mustNotFail(t, err)
	if !changed || again {
		t.Errorf("changed %v, again %v", changed, again)
	}
	text := readText(t, filepath.Join(root, filepath.FromSlash(rel)))
	if strings.Contains(text, "release_commit") || strings.Contains(text, "released_trees") || !strings.Contains(text, "unrecoverable = true") || !strings.Contains(text, `shipped_as = "portal-v0.1.0"`) {
		t.Errorf("the archive reads:\n%s", text)
	}
	_, err = writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		_, err := releaserecord.DropReleaseCommit(e, root, dir, version(t, "0.3.0"))
		return err
	})
	if err == nil || !strings.Contains(err.Error(), "not a release commit") {
		t.Fatalf("a never-released archive was changed: %v", err)
	}
}
