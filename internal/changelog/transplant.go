package changelog

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

// The writers a repository conversion (`monorepo extract` and `monorepo
// absorb`) needs beyond the release's: it moves a releasable's changelog
// from one repository to another, maps every commit id through the
// rewrite's commit map, and narrows or drops what the rewrite did not carry.

// MapCommit maps one possibly abbreviated commit id through a rewrite map:
// by the key it equals, or by the one key it is a prefix of. ambiguous is
// true when it is the prefix of more than one key; next is empty when
// nothing maps it.
func MapCommit(h string, rewrites map[string]string) (next string, ambiguous bool) {
	return mapCommit(h, rewrites)
}

// ReplaceEntries rewrites f with entries in place of its lines, keeping the
// file's mode (a released file stays read-only). Every entry is validated
// before anything is written, and a file changed since it was read is
// refused rather than overwritten.
func ReplaceEntries(e *strictcli.Effects, root string, f *File, entries []Entry) error {
	lines := make([]string, len(entries))
	for i, entry := range entries {
		if err := Validate(entry); err != nil {
			return fmt.Errorf("%s: %w", f.Path, err)
		}
		lines[i] = Serialize(entry)
	}
	return rewrite(e, root, f, lines)
}

// WriteVersionFile writes released version v's file in dir
// (repository-relative) holding entries, read-only. A file already there is
// refused: a released file is a record, never overwritten.
func WriteVersionFile(e *strictcli.Effects, root, dir string, v semver.Version, entries []Entry) error {
	rel := path.Join(dir, VersionName(v))
	if _, err := os.Lstat(absolute(root, rel)); err == nil {
		return fmt.Errorf("refusing to write %s: the changelog of %s already exists, and a released file is never overwritten", rel, v)
	} else if !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("reading %s: %w", rel, err)
	}
	lines := make([]string, len(entries))
	for i, entry := range entries {
		if err := Validate(entry); err != nil {
			return fmt.Errorf("%s: %w", rel, err)
		}
		lines[i] = Serialize(entry)
	}
	return writeFile(e, root, rel, render(lines), releasedMode)
}

// RegenerateRollUp rewrites a workspace's roll-up, the root CHANGELOG.md,
// when it exists and its content changed: after a releasable left the
// workspace, the roll-up still holds its sections. It returns the
// repository-relative paths it wrote. A repository without a roll-up, or
// declarations of a standalone project, write nothing.
func RegenerateRollUp(e *strictcli.Effects, root string, d *declarations.Releasables) ([]string, error) {
	if !d.IsWorkspace() {
		return nil, nil
	}
	current, err := os.ReadFile(absolute(root, RollUpPath))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", RollUpPath, err)
	}
	rollUp, err := RollUp(root, d, "", nil)
	if err != nil {
		return nil, err
	}
	if string(current) == rollUp {
		return nil, nil
	}
	mode, err := modeOf(root, RollUpPath, 0o644)
	if err != nil {
		return nil, err
	}
	if err := writeFile(e, root, RollUpPath, []byte(rollUp), mode); err != nil {
		return nil, err
	}
	return []string{RollUpPath}, nil
}
