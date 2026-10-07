package releaserecord

import (
	"fmt"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/semver"
)

// DropReleaseCommit turns the recorded archive of v in dir unrecoverable:
// a squash folded the commit it shipped from into a commit carrying another
// tree, so the released tree no longer exists in the history. The release
// commit and the released trees are removed and unrecoverable = true is
// set, every other line kept. An archive already unrecoverable is left as
// it is (changed is false); a never-released archive, and one stating no
// fate, are refused.
func DropReleaseCommit(e *strictcli.Effects, root, dir string, v semver.Version) (changed bool, err error) {
	return rewriteArchive(e, root, dir, v, func(a Archive, d *tomledit.Document) (bool, error) {
		switch a.Fate {
		case FateUnrecoverable:
			return false, nil
		case FateRecorded:
		default:
			return false, fmt.Errorf("refusing to drop the release commit of %s: it records %s, not a release commit", a.Path, a.Fate)
		}
		if err := d.Delete(fieldReleasedTrees); err != nil {
			return false, err
		}
		if err := d.Delete(fieldReleaseCommit); err != nil {
			return false, err
		}
		return true, d.Set(fieldUnrecoverable, true)
	})
}
