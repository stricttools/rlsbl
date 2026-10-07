package releaserecord

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/semver"
)

// quote renders a TOML basic string.
func quote(s string) string { return tomledit.QuoteString(s) }

func stringArray(values []string) string {
	quoted := make([]string, len(values))
	for i, v := range values {
		quoted[i] = quote(v)
	}
	return "[" + strings.Join(quoted, ", ") + "]"
}

// renderFields writes one release's fields as TOML lines.
func renderFields(f ReleaseFile) string {
	var b strings.Builder
	fmt.Fprintf(&b, "bump = %s\n", quote(string(f.Bump)))
	fmt.Fprintf(&b, "include = %s\n", stringArray(f.Include))
	fmt.Fprintf(&b, "exclude = %s\n", stringArray(f.Exclude))
	fmt.Fprintf(&b, "description = %s\n", quote(f.Description))
	if f.Context != "" {
		fmt.Fprintf(&b, "context = %s\n", quote(f.Context))
	}
	return b.String()
}

// RenderReleaseFile renders an editable release file.
func RenderReleaseFile(f ReleaseFile) []byte {
	return []byte(fmt.Sprintf("format_version = %d\n%s", FormatVersion, renderFields(f)))
}

// WriteReleaseFile writes a releasable's editable release file, refusing
// one its own reader would refuse.
func WriteReleaseFile(e *strictcli.Effects, root, releasable string, f ReleaseFile) error {
	rel := ReleaseFilePath(releasable)
	data := RenderReleaseFile(f)
	if _, err := ParseReleaseFile(rel, data); err != nil {
		return err
	}
	return writeFile(e, root, rel, data, releaseFileMode)
}

// ArchiveSpec is an archive written whole, for a release that had no
// release file of its own (a batch member, whose fields come from the batch
// release file) and for the backfill.
type ArchiveSpec struct {
	ReleaseFile
	// Fate is FateRecorded, FateUnrecoverable, or FateNeverReleased.
	Fate Fate
	// ReleaseCommit is set for FateRecorded and empty otherwise.
	ReleaseCommit ReleaseCommit
	ShippedAs     string
	// Header replaces the comment block at the top of the file, one line per
	// element, for a writer other than the release flow (the backfill says
	// the archive was written after the fact).
	Header []string
}

var defaultArchiveHeader = []string{
	"Archived by rlsbl at release time. This release had no release file of its",
	"own (a batch member takes its fields from the batch release file); the",
	"archive keeps its description and context for every later changelog.",
}

// WriteArchive writes the archive of v in dir (repository-relative) from a
// spec and returns its path. An archive has one fate, and that fate decides
// which fields it carries: a release commit only when recorded, shipped_as
// never when never released. An archive already there is refused: an
// archive is rewritten only through the writers that change one field.
func WriteArchive(e *strictcli.Effects, root, dir string, v semver.Version, spec ArchiveSpec) (string, error) {
	rel := ArchivePath(dir, v)
	switch spec.Fate {
	case FateRecorded:
		if err := spec.ReleaseCommit.check(); err != nil {
			return "", err
		}
	case FateUnrecoverable, FateNeverReleased:
		if spec.ReleaseCommit.Commit != "" || len(spec.ReleaseCommit.Trees) > 0 {
			return "", fmt.Errorf("an archive whose fate is %s carries no release commit", spec.Fate)
		}
	default:
		return "", fmt.Errorf("an archive is written with one of the fates recorded, unrecoverable, and never-released, not %q", spec.Fate)
	}
	if spec.ShippedAs != "" && spec.Fate == FateNeverReleased {
		return "", errors.New("a never-released archive shipped under no tag, so it carries no shipped_as")
	}
	if spec.ShippedAs != strings.TrimSpace(spec.ShippedAs) {
		return "", fmt.Errorf("shipped_as %q carries surrounding whitespace; a tag has none", spec.ShippedAs)
	}
	found, err := exists(root, rel)
	if err != nil {
		return "", err
	}
	if found {
		return "", fmt.Errorf("refusing to write %s: an archive of %s already exists", rel, v)
	}
	header := spec.Header
	if header == nil {
		header = defaultArchiveHeader
	}
	var b strings.Builder
	for _, line := range header {
		fmt.Fprintf(&b, "# %s\n", line)
	}
	fmt.Fprintf(&b, "format_version = %d\n", FormatVersion)
	b.WriteString(renderFields(spec.ReleaseFile))
	if spec.ShippedAs != "" {
		fmt.Fprintf(&b, "shipped_as = %s\n", quote(spec.ShippedAs))
	}
	switch spec.Fate {
	case FateUnrecoverable:
		b.WriteString("unrecoverable = true\n")
	case FateNeverReleased:
		b.WriteString("never_released = true\n")
	case FateRecorded:
		fmt.Fprintf(&b, "release_commit = %s\n", quote(spec.ReleaseCommit.Commit))
		b.WriteString(renderTrees(spec.ReleaseCommit.Trees))
	}
	data := []byte(b.String())
	if _, err := parseArchive(rel, v, data); err != nil {
		return "", err
	}
	return rel, writeFile(e, root, rel, data, archiveMode)
}

// renderTrees writes the released trees as a [released_trees] table, paths
// in order.
func renderTrees(trees map[string]string) string {
	paths := make([]string, 0, len(trees))
	for p := range trees {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	var b strings.Builder
	b.WriteString("\n[released_trees]\n")
	for _, p := range paths {
		fmt.Fprintf(&b, "%s = %s\n", tomledit.QuoteKey(p), quote(trees[p]))
	}
	return b.String()
}

// editDocument applies edit to the parsed document at rel and returns the
// result, refusing a result the archive reader would refuse.
func editDocument(rel string, v semver.Version, data []byte, edit func(d *tomledit.Document) error) ([]byte, error) {
	doc, err := tomledit.Parse(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", rel, err)
	}
	if err := edit(doc); err != nil {
		return nil, fmt.Errorf("%s: %w", rel, err)
	}
	out := doc.Bytes()
	if _, err := parseArchive(rel, v, out); err != nil {
		return nil, fmt.Errorf("the edit would leave a document the archive reader refuses, so nothing was written: %w", err)
	}
	return out, nil
}

// setReleaseCommit replaces the release commit fields of a document, keeping
// every other line: the operator's comments, ordering, and shipped_as.
func setReleaseCommit(d *tomledit.Document, c ReleaseCommit) error {
	if err := d.Delete(fieldReleasedTrees); err != nil {
		return err
	}
	if err := d.Set(fieldReleaseCommit, c.Commit); err != nil {
		return err
	}
	if err := d.NewTable(fieldReleasedTrees); err != nil {
		return err
	}
	paths := make([]string, 0, len(c.Trees))
	for p := range c.Trees {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	for _, p := range paths {
		// check admits no quote or backslash in a released path, so quoting
		// it is wrapping it.
		if err := d.Set(fieldReleasedTrees+`."`+p+`"`, c.Trees[p]); err != nil {
			return err
		}
	}
	return nil
}

// ArchiveReleaseFile turns a releasable's release file into the archive of v
// with its release commit: the file the operator wrote, comments and all,
// plus the release commit, written read-only at the archive's path; the
// release file is then removed. Archiving the same release again (a resumed
// release past this step) finds the archive recording this release commit
// and the release file gone, and does nothing. An archive of v recording
// anything else is refused and left as it is: a never-released archive above
// all, since rlsbl never releases under a number recorded never released.
func ArchiveReleaseFile(e *strictcli.Effects, root, releasable string, v semver.Version, c ReleaseCommit) error {
	if err := c.check(); err != nil {
		return err
	}
	dir := ArchiveDir(releasable)
	archive := ArchivePath(dir, v)
	release := ReleaseFilePath(releasable)
	existing, archived, err := readFile(root, archive)
	if err != nil {
		return err
	}
	data, editable, err := readFile(root, release)
	if err != nil {
		return err
	}
	if archived {
		a, err := parseArchive(archive, v, existing)
		if err != nil {
			return err
		}
		if a.Fate == FateNeverReleased {
			return fmt.Errorf("refusing to archive the release of %s: %s records it as never released, and rlsbl never releases under a never-released number. Nothing was written; bump past it and release again", v, archive)
		}
		if a.Fate != FateRecorded || a.ReleaseCommit.Commit != c.Commit {
			return fmt.Errorf("refusing to archive the release of %s at %s: %s already records it (%s). Nothing was written", v, c.Commit, archive, describeArchiveFate(a))
		}
		if editable {
			return fmt.Errorf("%s already records the release of %s at %s, yet %s is still there; it holds the next release's fields or a leftover, so it is left alone. Remove it or fill it in for the next release", archive, v, c.Commit, release)
		}
		return nil
	}
	if !editable {
		return fmt.Errorf("refusing to archive the release of %s: the releasable %q has no release file %s to archive", v, releasable, release)
	}
	if _, err := ParseReleaseFile(release, data); err != nil {
		return err
	}
	out, err := editDocument(archive, v, data, func(d *tomledit.Document) error { return setReleaseCommit(d, c) })
	if err != nil {
		return err
	}
	if err := writeFile(e, root, archive, out, archiveMode); err != nil {
		return err
	}
	if _, err := e.Remove(absolute(root, release)); err != nil {
		return fmt.Errorf("removing %s: %w", release, err)
	}
	return nil
}

func describeArchiveFate(a Archive) string {
	if a.Fate == FateRecorded {
		return "released from " + a.ReleaseCommit.Commit
	}
	return string(a.Fate)
}

// rewriteArchive reads the archive of v in dir, applies edit, and writes the
// result back read-only.
func rewriteArchive(e *strictcli.Effects, root, dir string, v semver.Version, edit func(a Archive, d *tomledit.Document) (bool, error)) (bool, error) {
	rel := ArchivePath(dir, v)
	data, found, err := readFile(root, rel)
	if err != nil {
		return false, err
	}
	if !found {
		return false, fmt.Errorf("there is no archive %s", rel)
	}
	a, err := parseArchive(rel, v, data)
	if err != nil {
		return false, err
	}
	changed := true
	out, err := editDocument(rel, v, data, func(d *tomledit.Document) error {
		var err error
		changed, err = edit(a, d)
		return err
	})
	if err != nil || !changed {
		return false, err
	}
	return true, writeFile(e, root, rel, out, archiveMode)
}

// WriteReleaseCommit records a release commit into the existing archive of v
// in dir, replacing a release commit it already records: the backfill
// recording a version shipped before release commits were, and a history
// rewrite moving one. An archive stating another fate is refused: a release
// commit beside unrecoverable or never_released is two fates.
func WriteReleaseCommit(e *strictcli.Effects, root, dir string, v semver.Version, c ReleaseCommit) error {
	if err := c.check(); err != nil {
		return err
	}
	_, err := rewriteArchive(e, root, dir, v, func(a Archive, d *tomledit.Document) (bool, error) {
		if a.Fate == FateUnrecoverable || a.Fate == FateNeverReleased {
			return false, fmt.Errorf("refusing to write a release commit: the archive records the %s fate, and an archive states one fate. If the version's fate changed, delete the %s line by hand first", a.Fate, fateField(a.Fate))
		}
		return true, setReleaseCommit(d, c)
	})
	return err
}

func fateField(f Fate) string {
	if f == FateNeverReleased {
		return fieldNeverReleased
	}
	return fieldUnrecoverable
}

// MarkUnrecoverable records on the existing archive of v in dir that the
// commit it shipped from cannot be recovered. An archive whose fate is
// settled otherwise is refused: one recording a release commit shipped from a
// known commit, and one recording never_released never shipped.
func MarkUnrecoverable(e *strictcli.Effects, root, dir string, v semver.Version) error {
	_, err := rewriteArchive(e, root, dir, v, func(a Archive, d *tomledit.Document) (bool, error) {
		switch a.Fate {
		case FateRecorded:
			return false, fmt.Errorf("refusing to mark %s unrecoverable: it records the release commit %s, so the version shipped from a known commit", a.Path, a.ReleaseCommit.Commit)
		case FateNeverReleased:
			return false, fmt.Errorf("refusing to mark %s unrecoverable: it records never_released = true, which says no release was published under %s at all. If %s did ship, delete the never_released line by hand first", a.Path, v, v)
		case FateUnrecoverable:
			return false, nil
		}
		return true, d.Set(fieldUnrecoverable, true)
	})
	return err
}

// RecordShippedAs records on the existing archive of v in dir the tag the
// version shipped under. It reports false, writing nothing, when the archive
// already records tag. A never-released archive is refused, and so is one
// recording a different tag: a version ships under one tag.
func RecordShippedAs(e *strictcli.Effects, root, dir string, v semver.Version, tag string) (bool, error) {
	if strings.TrimSpace(tag) == "" || tag != strings.TrimSpace(tag) {
		return false, fmt.Errorf("refusing to record shipped_as %q: a tag is non-empty and carries no surrounding whitespace", tag)
	}
	return rewriteArchive(e, root, dir, v, func(a Archive, d *tomledit.Document) (bool, error) {
		if a.Fate == FateNeverReleased {
			return false, fmt.Errorf("refusing to record shipped_as %q in %s: it records never_released = true, and a version no release used shipped under no tag", tag, a.Path)
		}
		if a.ShippedAs == tag {
			return false, nil
		}
		if a.ShippedAs != "" {
			return false, fmt.Errorf("refusing to record shipped_as %q in %s: it already records shipped_as %q, and a version ships under one tag; correct the archive by hand if the recorded one is wrong", tag, a.Path, a.ShippedAs)
		}
		return true, d.Set(fieldShippedAs, tag)
	})
}

// RecordReleaseNotice puts notice first in the release_notices of the
// existing archive of v in dir: deprecate and yank prepend their notice to
// the Release body, so a later notice sits above an earlier one.
func RecordReleaseNotice(e *strictcli.Effects, root, dir string, v semver.Version, notice string) error {
	if strings.TrimSpace(notice) == "" {
		return errors.New("refusing to record an empty release notice")
	}
	_, err := rewriteArchive(e, root, dir, v, func(a Archive, d *tomledit.Document) (bool, error) {
		notices := append([]string{notice}, a.ReleaseNotices...)
		return true, d.Set(fieldReleaseNotices, notices)
	})
	return err
}

// Unfinalize restores the archive of v as the releasable's editable release
// file, for `release undo`: the archive's fields without the flow-owned ones,
// which describe a version whose fate is settled and are refused in a
// release file. A release file already there is refused unless it is
// pristine, since it may hold the next release's fields. It returns the
// repository-relative paths it changed, nothing when there is no archive of
// v.
func Unfinalize(e *strictcli.Effects, root, releasable string, v semver.Version) ([]string, error) {
	dir := ArchiveDir(releasable)
	archive := ArchivePath(dir, v)
	release := ReleaseFilePath(releasable)
	data, found, err := readFile(root, archive)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, nil
	}
	existing, editable, err := readFile(root, release)
	if err != nil {
		return nil, err
	}
	if editable && !IsPristineReleaseFile(existing) {
		return nil, fmt.Errorf("refusing to restore %s as %s: %s holds someone's release fields. Move them out of the way (commit them elsewhere, or empty the file) and undo again", archive, release, release)
	}
	if _, err := parseArchive(archive, v, data); err != nil {
		return nil, err
	}
	doc, err := tomledit.Parse(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", archive, err)
	}
	for _, field := range flowOwnedFields {
		if err := doc.Delete(field); err != nil {
			return nil, fmt.Errorf("%s: %w", archive, err)
		}
	}
	out := doc.Bytes()
	if _, err := ParseReleaseFile(release, out); err != nil {
		return nil, err
	}
	if err := writeFile(e, root, release, out, releaseFileMode); err != nil {
		return nil, err
	}
	if _, err := e.Remove(absolute(root, archive)); err != nil {
		return nil, fmt.Errorf("removing %s: %w", archive, err)
	}
	return []string{release, archive}, nil
}
