package releaserecord

import (
	"fmt"
	"slices"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/semver"
)

// The backfill brings archives written before the record asked for every
// field into the fate model: it reads an archive the strict reader would
// refuse (a blank bump or description, a missing include or exclude, no
// fate), and completes it in one write. Both halves are here, so an archive
// is still only ever written by this package.

// RequiredFields are the fields every release document carries, in the
// order the backfill completes them.
var RequiredFields = []string{"bump", "include", "exclude", "description"}

// LooseArchive is what an archive states, read without the strict reader.
type LooseArchive struct {
	// Path is the archive's repository-relative path.
	Path string
	// Answered names the required fields the archive answers. A blank
	// string is not an answer: a release file archived before anyone filled
	// it in. An empty list is one (exclude = [] says nothing is excluded).
	Answered map[string]bool
	// Fate is what the archive's markers state, FateUnstated for none.
	Fate          Fate
	ReleaseCommit string
	ShippedAs     string
	Description   string
	Context       string
}

// Missing lists the required fields the archive does not answer, in order.
func (a LooseArchive) Missing() []string {
	var missing []string
	for _, f := range RequiredFields {
		if !a.Answered[f] {
			missing = append(missing, f)
		}
	}
	return missing
}

// ReadArchiveLoosely reads the archive of v in dir (repository-relative)
// without validating its required fields. A document that is not TOML, one
// whose format_version is not this format's, and one carrying the first
// format's fields are refused, naming why: the backfill completes archives
// of this format, and `rlsbl migrate records` is what converts the first.
func ReadArchiveLoosely(root, dir string, v semver.Version) (LooseArchive, error) {
	rel := ArchivePath(dir, v)
	data, found, err := readFile(root, rel)
	if err != nil {
		return LooseArchive{}, err
	}
	if !found {
		return LooseArchive{}, fmt.Errorf("there is no archive %s", rel)
	}
	parsed, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return LooseArchive{}, refuseFile(rel, "it is not a TOML document: "+err.Error())
	}
	fields := *parsed
	if problems := retiredProblems(fields, ""); len(problems) > 0 {
		return LooseArchive{}, refuseFile(rel, problems...)
	}
	if format, _ := fields["format_version"].(int64); format != FormatVersion {
		return LooseArchive{}, refuseFile(rel, fmt.Sprintf("format_version is %v, and the backfill completes archives of format %d only; rlsbl migrate records converts an archive of the first format, and an archive with no format_version is converted the same way", fields["format_version"], FormatVersion))
	}
	a := LooseArchive{Path: rel, Answered: map[string]bool{}, Fate: FateUnstated}
	for _, f := range RequiredFields {
		value, present := fields[f]
		if !present {
			continue
		}
		if s, isString := value.(string); isString && strings.TrimSpace(s) == "" {
			continue
		}
		a.Answered[f] = true
	}
	switch {
	case fields[fieldNeverReleased] != nil:
		a.Fate = FateNeverReleased
	case fields[fieldUnrecoverable] != nil:
		a.Fate = FateUnrecoverable
	case fields[fieldReleaseCommit] != nil:
		a.Fate = FateRecorded
	}
	a.ReleaseCommit, _ = fields[fieldReleaseCommit].(string)
	a.ShippedAs, _ = fields[fieldShippedAs].(string)
	a.Description, _ = fields["description"].(string)
	a.Context, _ = fields["context"].(string)
	return a, nil
}

// ArchiveFill is one field the backfill writes into an archive: a required
// field the archive does not answer, or a reviewed description or context
// replacing what it says. Value is a string or a []string.
type ArchiveFill struct {
	Field  string
	Value  any
	Source string
}

// ArchiveCompletion is everything one backfill write puts into an existing
// archive.
type ArchiveCompletion struct {
	Fills []ArchiveFill
	// Fate is FateRecorded or FateUnrecoverable to state that fate, and
	// empty to leave the archive's fate as it is.
	Fate Fate
	// ReleaseCommit is set with FateRecorded.
	ReleaseCommit ReleaseCommit
}

// completionComment says on a written field's line that the backfill wrote
// it and where the value came from, so a reader of the read-only archive
// knows it was reconstructed rather than authored.
func completionComment(field, source string) string {
	return field + " reconstructed by `rlsbl release backfill` from " + source
}

// CompleteArchive writes c into the existing archive of v in dir
// (repository-relative) in one write: each fill in place of the line it
// replaces or appended, with a comment naming its source, then the fate.
// Every other line is kept. The result must read as an archive, or nothing
// is written. Stating a fate beside a different one the archive already
// states is refused: an archive states one fate.
func CompleteArchive(e *strictcli.Effects, root, dir string, v semver.Version, c ArchiveCompletion) error {
	current, err := ReadArchiveLoosely(root, dir, v)
	if err != nil {
		return err
	}
	switch c.Fate {
	case "":
	case FateRecorded:
		if err := c.ReleaseCommit.check(); err != nil {
			return err
		}
		if current.Fate == FateUnrecoverable || current.Fate == FateNeverReleased {
			return fmt.Errorf("refusing to record a release commit in %s: it states the %s fate, and an archive states one fate", current.Path, current.Fate)
		}
	case FateUnrecoverable:
		if current.Fate == FateRecorded || current.Fate == FateNeverReleased {
			return fmt.Errorf("refusing to mark %s unrecoverable: it states the %s fate, and an archive states one fate", current.Path, current.Fate)
		}
	default:
		return fmt.Errorf("a completion states the recorded or the unrecoverable fate, not %q", c.Fate)
	}
	for _, fill := range c.Fills {
		if !slices.Contains(RequiredFields, fill.Field) && fill.Field != "context" {
			return fmt.Errorf("the backfill writes the required fields and context, not %q", fill.Field)
		}
		if strings.ContainsAny(fill.Source, "\n\r") {
			return fmt.Errorf("the source of %s holds a line break, which a comment cannot carry", fill.Field)
		}
	}
	data, _, err := readFile(root, current.Path)
	if err != nil {
		return err
	}
	out, err := editDocument(current.Path, v, data, func(d *tomledit.Document) error {
		for _, fill := range c.Fills {
			if err := d.Set(fill.Field, fill.Value); err != nil {
				return err
			}
			if err := d.SetComment(fill.Field, completionComment(fill.Field, fill.Source)); err != nil {
				return err
			}
		}
		switch c.Fate {
		case FateRecorded:
			return setReleaseCommit(d, c.ReleaseCommit)
		case FateUnrecoverable:
			return d.Set(fieldUnrecoverable, true)
		}
		return nil
	})
	if err != nil {
		return err
	}
	return writeFile(e, root, current.Path, out, archiveMode)
}
