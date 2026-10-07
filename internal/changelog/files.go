package changelog

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

// UnreleasedName is the file of the entries since the last release.
const UnreleasedName = "unreleased.jsonl"

// The modes of the changelog files: the unreleased file is written to, a
// released version's file is a read-only record.
const (
	unreleasedMode os.FileMode = 0o644
	releasedMode   os.FileMode = 0o444
)

// Dir is a releasable's changelog directory, repository-relative.
func Dir(releasable string) string { return declarations.ChangelogDir(releasable) }

// RetiredDir is the changelog directory of a retired subject's release
// history, repository-relative.
func RetiredDir(subject string) string {
	return declarations.RetiredHistoryDir(subject) + "/changelog"
}

// VersionName is the file of a released version's entries.
func VersionName(v semver.Version) string { return v.String() + ".jsonl" }

// File is one changelog file and its lines.
type File struct {
	// Path is repository-relative.
	Path string
	// Released is false for the unreleased file.
	Released bool
	// Version is the released version; zero for the unreleased file.
	Version semver.Version
	Lines   []Line
}

// Line is one non-blank line of a changelog file.
type Line struct {
	// Number is 1-based.
	Number int
	// Text is the line as the file holds it, without its newline.
	Text  string
	Entry Entry
}

// Entries are the file's entries, in file order.
func (f *File) Entries() []Entry {
	out := make([]Entry, len(f.Lines))
	for i, l := range f.Lines {
		out[i] = l.Entry
	}
	return out
}

// Label names the file in a message: "unreleased" or the version.
func (f *File) Label() string {
	if !f.Released {
		return "unreleased"
	}
	return f.Version.String()
}

// FileError is a changelog file with refused lines: every one is named.
type FileError struct {
	File  string
	Lines []*LineError
}

func (e *FileError) Error() string {
	parts := make([]string, len(e.Lines))
	for i, l := range e.Lines {
		parts[i] = l.Error()
	}
	return strings.Join(parts, "\n")
}

// FormatVersionOnly reports whether every refused line is refused for its
// format version.
func (e *FileError) FormatVersionOnly() bool {
	for _, l := range e.Lines {
		if !l.FormatVersion {
			return false
		}
	}
	return true
}

func absolute(root, rel string) string { return filepath.Join(root, filepath.FromSlash(rel)) }

// parseFile reads the lines of data, refusing every line rlsbl refuses.
func parseFile(rel string, data []byte) ([]Line, error) {
	var lines []Line
	var refused []*LineError
	for i, text := range strings.Split(string(data), "\n") {
		text = strings.TrimSpace(text)
		if text == "" {
			continue
		}
		entry, err := ParseLine(text)
		if err != nil {
			var le *LineError
			if !errors.As(err, &le) {
				return nil, err
			}
			le.File, le.Line = rel, i+1
			refused = append(refused, le)
			continue
		}
		lines = append(lines, Line{Number: i + 1, Text: text, Entry: entry})
	}
	if len(refused) > 0 {
		return nil, &FileError{File: rel, Lines: refused}
	}
	return lines, nil
}

// readFile reads the changelog file rel. found is false when it does not
// exist.
func readFile(root, rel string) (f *File, found bool, err error) {
	data, err := os.ReadFile(absolute(root, rel))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", rel, err)
	}
	lines, err := parseFile(rel, data)
	if err != nil {
		return nil, true, err
	}
	return &File{Path: rel, Lines: lines}, true, nil
}

// Versions are the released versions dir (repository-relative) holds a file
// of, highest first. A .jsonl file that is neither the unreleased file nor
// MAJOR.MINOR.PATCH.jsonl is refused, naming it: skipping it would hide
// entries from every question asked of the changelog. A missing directory
// holds none.
func Versions(root, dir string) ([]semver.Version, error) {
	items, err := os.ReadDir(absolute(root, dir))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", dir, err)
	}
	var versions []semver.Version
	for _, item := range items {
		name := item.Name()
		if item.IsDir() || !strings.HasSuffix(name, ".jsonl") || name == UnreleasedName {
			continue
		}
		v, err := semver.Parse(strings.TrimSuffix(name, ".jsonl"))
		if err != nil {
			return nil, fmt.Errorf("%s is in %s but is neither %s nor a released version's file (MAJOR.MINOR.PATCH.jsonl): rlsbl will not read the changelog past a file it cannot place; rename it or remove it", path.Join(dir, name), dir, UnreleasedName)
		}
		versions = append(versions, v)
	}
	sort.Slice(versions, func(i, j int) bool { return semver.Compare(versions[i], versions[j]) > 0 })
	return versions, nil
}

// ReadUnreleased reads dir's unreleased file; a missing file is an empty
// one.
func ReadUnreleased(root, dir string) (*File, error) {
	rel := path.Join(dir, UnreleasedName)
	f, found, err := readFile(root, rel)
	if err != nil {
		return nil, err
	}
	if !found {
		return &File{Path: rel}, nil
	}
	return f, nil
}

// ReadVersion reads the file of released version v; a missing file is an
// error naming it.
func ReadVersion(root, dir string, v semver.Version) (*File, error) {
	rel := path.Join(dir, VersionName(v))
	f, found, err := readFile(root, rel)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, fmt.Errorf("there is no changelog file %s", rel)
	}
	f.Released, f.Version = true, v
	return f, nil
}

// ReadAll reads every changelog file of dir: the unreleased file first
// (empty when missing), then each released version's, highest first.
func ReadAll(root, dir string) ([]*File, error) {
	versions, err := Versions(root, dir)
	if err != nil {
		return nil, err
	}
	unreleased, err := ReadUnreleased(root, dir)
	if err != nil {
		return nil, err
	}
	files := []*File{unreleased}
	for _, v := range versions {
		f, err := ReadVersion(root, dir, v)
		if err != nil {
			return nil, err
		}
		files = append(files, f)
	}
	return files, nil
}

// topDirectory is the directory directly under .strictmetadata/ that rel
// lies in, the one carrying the ownership manifest.
func topDirectory(rel string) string {
	rest := strings.TrimPrefix(rel, declarations.MetadataDir+"/")
	first, _, _ := strings.Cut(rest, "/")
	return declarations.MetadataDir + "/" + first
}

// writeFile replaces the repository-relative rel with data atomically: a
// sibling temporary file carrying the final mode, renamed over the target.
// Renaming over a read-only file needs no unlocking, so a released file is
// never observable as writable. A file under .strictmetadata/ gets its
// directory's ownership manifest when missing.
func writeFile(e *strictcli.Effects, root, rel string, data []byte, mode os.FileMode) error {
	if !filepath.IsAbs(root) {
		return fmt.Errorf("the repository root %q is not absolute", root)
	}
	if strings.HasPrefix(rel, declarations.MetadataDir+"/") {
		if err := declarations.EnsureOwnedDirectory(e, root, topDirectory(rel)); err != nil {
			return err
		}
	}
	target := absolute(root, rel)
	if _, err := e.Mkdir(filepath.Dir(target)); err != nil {
		return fmt.Errorf("creating the directory of %s: %w", rel, err)
	}
	temporary := target + ".tmp"
	if _, err := os.Lstat(temporary); err == nil {
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

// modeOf is the permission bits of the repository-relative rel, or fallback
// when it does not exist.
func modeOf(root, rel string, fallback os.FileMode) (os.FileMode, error) {
	info, err := os.Stat(absolute(root, rel))
	if errors.Is(err, fs.ErrNotExist) {
		return fallback, nil
	}
	if err != nil {
		return 0, fmt.Errorf("reading %s: %w", rel, err)
	}
	return info.Mode().Perm(), nil
}

// render is the text of lines, one per line, each ending in a newline.
func render(lines []string) []byte {
	if len(lines) == 0 {
		return nil
	}
	return []byte(strings.Join(lines, "\n") + "\n")
}

func texts(f *File) []string {
	out := make([]string, len(f.Lines))
	for i, l := range f.Lines {
		out[i] = l.Text
	}
	return out
}

// appendLine writes f with entry's line added after its last line, keeping
// every other line as the file holds it.
func appendLine(e *strictcli.Effects, root string, f *File, entry Entry, fallback os.FileMode) error {
	if err := Validate(entry); err != nil {
		return err
	}
	mode, err := modeOf(root, f.Path, fallback)
	if err != nil {
		return err
	}
	return writeFile(e, root, f.Path, render(append(texts(f), Serialize(entry))), mode)
}

// AppendEntry adds entry to the end of dir's unreleased file, creating the
// file and its directory when missing. The effects handle has no append, so
// the file is read and replaced whole: two writers at once would lose one
// entry, and a command that writes a changelog holds the repository's
// release lock (internal/runstate) around its read and its write.
func AppendEntry(e *strictcli.Effects, root, dir string, entry Entry) error {
	f, err := ReadUnreleased(root, dir)
	if err != nil {
		return err
	}
	return appendLine(e, root, f, entry, unreleasedMode)
}

// AppendToVersion adds entry to the end of released version v's file, which
// must exist, keeping its mode (read-only).
func AppendToVersion(e *strictcli.Effects, root, dir string, v semver.Version, entry Entry) error {
	f, err := ReadVersion(root, dir, v)
	if err != nil {
		return err
	}
	return appendLine(e, root, f, entry, releasedMode)
}

// Finalize turns dir's unreleased entries into released version v's file,
// read-only, and leaves an empty unreleased file. The unreleased file must
// exist, and v must have no file yet: a released file is a record, never
// overwritten (a leftover from a release that stopped part-way is inspected
// and removed by hand before the release is run again).
func Finalize(e *strictcli.Effects, root, dir string, v semver.Version) error {
	unreleased := path.Join(dir, UnreleasedName)
	released := path.Join(dir, VersionName(v))
	if _, err := os.Lstat(absolute(root, released)); err == nil {
		return fmt.Errorf("refusing to finalize the changelog of %s: %s already exists. A previous release attempt stopped part-way; released changelog files are read-only records, so inspect the existing file and remove it with saferm before releasing again", v, released)
	} else if !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("reading %s: %w", released, err)
	}
	f, found, err := readFile(root, unreleased)
	if err != nil {
		return err
	}
	if !found {
		return fmt.Errorf("refusing to finalize the changelog of %s: %s does not exist", v, unreleased)
	}
	if err := writeFile(e, root, released, render(texts(f)), releasedMode); err != nil {
		return err
	}
	return writeFile(e, root, unreleased, nil, unreleasedMode)
}

// Unfinalize reverses Finalize for v: v's lines go back into the unreleased
// file ahead of any entry written since the release (the order the two sets
// of commits were made in), every line kept as written, and v's file is
// removed. It returns the repository-relative paths it changed, nothing
// when v has no file.
func Unfinalize(e *strictcli.Effects, root, dir string, v semver.Version) ([]string, error) {
	released := path.Join(dir, VersionName(v))
	f, found, err := readFile(root, released)
	if err != nil || !found {
		return nil, err
	}
	since, err := ReadUnreleased(root, dir)
	if err != nil {
		return nil, err
	}
	merged := append(texts(f), texts(since)...)
	if err := writeFile(e, root, since.Path, render(merged), unreleasedMode); err != nil {
		return nil, err
	}
	if _, err := e.Remove(absolute(root, released)); err != nil {
		return nil, fmt.Errorf("removing %s: %w", released, err)
	}
	return []string{since.Path, released}, nil
}

// rewrite replaces f's lines with lines, keeping the file's mode. f must
// still hold what it held when it was read: a file changed since is
// refused rather than overwritten.
func rewrite(e *strictcli.Effects, root string, f *File, lines []string) error {
	current, found, err := readFile(root, f.Path)
	if err != nil {
		return err
	}
	if !found || strings.Join(texts(current), "\n") != strings.Join(texts(f), "\n") {
		return fmt.Errorf("%s changed while it was being edited; nothing was written, so run the command again", f.Path)
	}
	mode, err := modeOf(root, f.Path, unreleasedMode)
	if err != nil {
		return err
	}
	return writeFile(e, root, f.Path, render(lines), mode)
}
