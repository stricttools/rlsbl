package declarations

import (
	"fmt"
	"path/filepath"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
)

// Owner is the tool named in the manifest.toml of every directory rlsbl
// owns under .strictmetadata/.
const Owner = "rlsbl"

// Write writes data as the release declarations of the repository rooted at
// root, through the effects handle, after parsing it: a document the
// declaration rules refuse is never written. The directory's manifest.toml
// is written when it is missing, and one naming another owner refuses the
// write.
func Write(e *strictcli.Effects, root string, data []byte) (*Releasables, error) {
	d, err := Parse(data)
	if err != nil {
		return nil, err
	}
	if err := writeRecord(e, root, ReleasablesDir, ReleasablesFile, data); err != nil {
		return nil, err
	}
	return d, nil
}

// manifest is a directory's manifest.toml under .strictmetadata/.
type manifest struct {
	Owner string `toml:"owner,required"`
}

// writeRecord writes data to the record rel inside dir (both repository-
// relative) atomically: a sibling temporary file renamed over the record.
func writeRecord(e *strictcli.Effects, root, dir, rel string, data []byte) error {
	if !filepath.IsAbs(root) {
		return fmt.Errorf("the repository root %q is not absolute", root)
	}
	if err := ensureManifest(e, root, dir); err != nil {
		return err
	}
	target := filepath.Join(root, filepath.FromSlash(rel))
	temporary := target + ".tmp"
	if _, err := e.Write(temporary, data); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	if _, err := e.Rename(temporary, target); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	return nil
}

// ensureManifest creates dir and its manifest.toml naming rlsbl when the
// manifest is missing, and refuses one naming another owner.
func ensureManifest(e *strictcli.Effects, root, dir string) error {
	rel := dir + "/" + git.OwnershipManifest
	data, found, err := readRecord(root, rel)
	if err != nil {
		return err
	}
	if found {
		m, err := tomledit.Unmarshal[manifest](data)
		if err != nil {
			return fmt.Errorf("%s: %w", rel, err)
		}
		if m.Owner != Owner {
			return fmt.Errorf("%s names %q as the directory's owner; rlsbl writes only directories whose manifest names %q", rel, m.Owner, Owner)
		}
		return nil
	}
	if _, err := e.Mkdir(filepath.Join(root, filepath.FromSlash(dir))); err != nil {
		return fmt.Errorf("creating %s: %w", dir, err)
	}
	if _, err := e.Write(filepath.Join(root, filepath.FromSlash(rel)), "owner = "+quote(Owner)+"\n"); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	return nil
}
