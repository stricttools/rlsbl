// Package releaserecord is the record of what a releasable has released:
// its release file, its release archives with their fates, the questions
// asked of them (the nearest release a checkout contains, the latest
// release, the fate of one version, the next version a release ships), the
// batch release file, the transition record of repository surgery, the
// explanation of the tags a repository holds, and the move of recorded
// release commits through a history rewrite.
//
// Every write goes through the strictcli effects handle, so --dry-run records
// it instead of performing it. Reads go to the files directly: reading a
// record changes nothing.
package releaserecord

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

// ReleaseFileName is the editable release file in a releasable's release
// directory.
const ReleaseFileName = "unreleased.toml"

// The modes of the two release documents. An archive is read-only from the
// instant it exists; the mode is local hygiene (git records no read-only
// bit), and rlsbl rewrites an archive only through this package.
const (
	releaseFileMode = 0o644
	archiveMode     = 0o444
)

// ArchiveDir is the repository-relative directory holding a releasable's
// release file and archives.
func ArchiveDir(releasable string) string { return declarations.ReleasesDir(releasable) }

// RetiredArchiveDir is the repository-relative directory holding the
// archives of a subject whose lifecycle is retired.
func RetiredArchiveDir(subject string) string {
	return declarations.RetiredHistoryDir(subject) + "/releases"
}

// ReleaseFilePath is the repository-relative path of a releasable's release
// file.
func ReleaseFilePath(releasable string) string {
	return ArchiveDir(releasable) + "/" + ReleaseFileName
}

// ArchiveName is the file name of the archive of v.
func ArchiveName(v semver.Version) string { return "v" + v.String() + ".toml" }

// ArchivePath is the repository-relative path of the archive of v in dir.
func ArchivePath(dir string, v semver.Version) string { return path.Join(dir, ArchiveName(v)) }

// archiveVersion reads an archive file name. ok is false for a name that is
// no archive's (unreleased.toml, version, undo-audits.jsonl, manifest.toml);
// a name that looks like an archive's but whose version rlsbl cannot read is
// an error, because passing over it would hide a release from every question
// asked of the record.
func archiveVersion(name string) (v semver.Version, ok bool, err error) {
	if !strings.HasPrefix(name, "v") || !strings.HasSuffix(name, ".toml") {
		return semver.Version{}, false, nil
	}
	middle := strings.TrimSuffix(strings.TrimPrefix(name, "v"), ".toml")
	v, err = semver.Parse(middle)
	if err != nil {
		return semver.Version{}, false, fmt.Errorf("%s is named like a release archive, but its version cannot be read (%v); an archive is named v<MAJOR.MINOR.PATCH>.toml, so rename or remove the file", name, err)
	}
	return v, true, nil
}

// ArchivedVersions lists the versions archived in dir (repository-relative),
// highest first. Only file names are read, so enumerating a releasable's
// whole history opens no archive. A missing directory holds no archive: a
// releasable before its first release.
func ArchivedVersions(root, dir string) ([]semver.Version, error) {
	entries, err := os.ReadDir(filepath.Join(root, filepath.FromSlash(dir)))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("listing %s: %w", dir, err)
	}
	var versions []semver.Version
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		v, ok, err := archiveVersion(entry.Name())
		if err != nil {
			return nil, fmt.Errorf("%s: %w", dir, err)
		}
		if ok {
			versions = append(versions, v)
		}
	}
	sort.Slice(versions, func(i, j int) bool { return semver.Compare(versions[i], versions[j]) > 0 })
	return versions, nil
}

// absolute is the path of the repository-relative rel under root.
func absolute(root, rel string) string { return filepath.Join(root, filepath.FromSlash(rel)) }

// readFile reads the file at the repository-relative rel. found is false
// when it does not exist.
func readFile(root, rel string) (data []byte, found bool, err error) {
	data, err = os.ReadFile(absolute(root, rel))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", rel, err)
	}
	return data, true, nil
}

// exists reports whether the repository-relative rel exists.
func exists(root, rel string) (bool, error) {
	_, err := os.Lstat(absolute(root, rel))
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("reading %s: %w", rel, err)
	}
	return true, nil
}

// topDirectory is the directory directly under .strictmetadata/ that rel
// lies in, the one carrying the ownership manifest.
func topDirectory(rel string) string {
	rest := strings.TrimPrefix(rel, declarations.MetadataDir+"/")
	first, _, _ := strings.Cut(rest, "/")
	return declarations.MetadataDir + "/" + first
}

// writeFile replaces the repository-relative rel with data, atomically: a
// sibling temporary file with the final mode, renamed over the target. The
// directory under .strictmetadata/ that rel lies in is created with its
// ownership manifest when missing. Renaming over a read-only archive needs
// no unlocking, so the archive is never observable as writable.
func writeFile(e *strictcli.Effects, root, rel string, data []byte, mode os.FileMode) error {
	if !filepath.IsAbs(root) {
		return fmt.Errorf("the repository root %q is not absolute", root)
	}
	if err := declarations.EnsureOwnedDirectory(e, root, topDirectory(rel)); err != nil {
		return err
	}
	target := absolute(root, rel)
	if _, err := e.Mkdir(filepath.Dir(target)); err != nil {
		return fmt.Errorf("creating the directory of %s: %w", rel, err)
	}
	temporary := target + ".tmp"
	if leftover, err := exists(root, rel+".tmp"); err != nil {
		return err
	} else if leftover {
		// A crashed write's leftover may be read-only, which a new write
		// cannot open; it is nobody's record.
		if _, err := e.Remove(temporary); err != nil {
			return fmt.Errorf("removing the leftover %s.tmp: %w", rel, err)
		}
	}
	if _, err := e.Write(temporary, data, strictcli.Mode(mode)); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	if _, err := e.Rename(temporary, target); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	return nil
}
