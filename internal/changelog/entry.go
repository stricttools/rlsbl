// Package changelog is rlsbl's structured changelog: one JSONL file of
// entries per release of each releasable, under
// .strictmetadata/changelog/<releasable>/ (unreleased.jsonl, and a
// read-only <version>.jsonl per release), with CHANGELOG.md generated from
// them.
//
// Every line is format version 2 and is read strictly: the generated
// validator (entryspec) is the authority on a line's shape, and a line it
// refuses makes its file unreadable, naming the line. There is no tolerant
// read and no legacy mode.
//
// Beside the files, the package answers the questions the changelog checks,
// the release, and the changelog commands ask: which commits are in a
// releasable's unreleased range (from the nearest release its release
// record holds), which of them need an entry (in the releasable's scope, and
// not exempt), which entries name commits that do not resolve, lie outside
// the range, or belong to another releasable, the batch limits, and the
// rewrite of commit ids after a history rewrite.
package changelog

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/rlsbl/internal/changelog/entryspec"
)

// FormatVersion is the format version of every changelog line.
const FormatVersion = 2

// The entry types.
const (
	TypeFeature  = "feature"
	TypeFix      = "fix"
	TypeBreaking = "breaking"
)

// Types are the entry types, in the order a type is listed in help and
// messages.
var Types = []string{TypeFeature, TypeFix, TypeBreaking}

// Entry is one changelog line.
type Entry struct {
	// ID is 48 lowercase hexadecimal digits (NewID).
	ID      string
	Commits []string
	// UserFacing entries appear in CHANGELOG.md and the release notes.
	UserFacing bool
	// Description is empty when the line carries none.
	Description string
	// Type is empty when the line carries none.
	Type string
	// Packages are the member packages the entry affects; nil when the line
	// carries none.
	Packages []string
	// BatchReason exempts the entry from the per-entry commit limit; empty
	// when the line carries none.
	BatchReason string
}

// NewID mints an entry id: 16 hexadecimal digits of the current time in
// nanoseconds, then the 32 of a random UUID4, so ids sort roughly by when
// they were written. crypto/rand's reader does not fail (since Go 1.24 it
// ends the program rather than return an error).
func NewID() string {
	var u [16]byte
	_, _ = rand.Read(u[:])
	u[6] = (u[6] & 0x0f) | 0x40
	u[8] = (u[8] & 0x3f) | 0x80
	return fmt.Sprintf("%016x", time.Now().UnixNano()) + hex.EncodeToString(u[:])
}

// jsonString is s as a JSON string, with <, >, and & written as themselves.
func jsonString(s string) string {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(s) // encoding a string cannot fail
	return strings.TrimSuffix(b.String(), "\n")
}

func jsonStrings(values []string) string {
	parts := make([]string, len(values))
	for i, v := range values {
		parts[i] = jsonString(v)
	}
	return "[" + strings.Join(parts, ",") + "]"
}

// Serialize is the entry as one line, without a newline: the format
// version first, then the fields in a fixed order, leaving out the absent
// optional ones.
func Serialize(e Entry) string {
	parts := []string{
		`"format_version":` + strconv.Itoa(FormatVersion),
		`"id":` + jsonString(e.ID),
		`"commits":` + jsonStrings(e.Commits),
		`"user_facing":` + strconv.FormatBool(e.UserFacing),
	}
	if e.Description != "" {
		parts = append(parts, `"description":`+jsonString(e.Description))
	}
	if e.Type != "" {
		parts = append(parts, `"type":`+jsonString(e.Type))
	}
	if e.Packages != nil {
		parts = append(parts, `"packages":`+jsonStrings(e.Packages))
	}
	if e.BatchReason != "" {
		parts = append(parts, `"batch_reason":`+jsonString(e.BatchReason))
	}
	return "{" + strings.Join(parts, ",") + "}"
}

// LineError is a changelog line rlsbl refuses.
type LineError struct {
	// File is the repository-relative file, or empty for a line read alone.
	File string
	// Line is the 1-based line number, or 0 for a line read alone.
	Line     int
	Problems []string
	// FormatVersion reports that the line is not of format version 2,
	// whatever else is wrong with it.
	FormatVersion bool
}

func (e *LineError) Error() string {
	where := "the changelog line"
	if e.File != "" {
		where = fmt.Sprintf("line %d of %s", e.Line, e.File)
	}
	if len(e.Problems) == 1 {
		return where + ": " + e.Problems[0]
	}
	return where + " is refused:\n  " + strings.Join(e.Problems, "\n  ")
}

// rawEntry is a line as encoding/json decodes it, after the validator
// accepted it.
type rawEntry struct {
	FormatVersion int      `json:"format_version"`
	ID            string   `json:"id"`
	Commits       []string `json:"commits"`
	UserFacing    bool     `json:"user_facing"`
	Description   string   `json:"description"`
	Type          string   `json:"type"`
	Packages      []string `json:"packages"`
	BatchReason   string   `json:"batch_reason"`
}

// formatVersionProblem says why a line is not of format version 2, and is
// empty when it is one. It is asked before the schema, so a line of the
// first format is named as one rather than as a list of shape problems.
func formatVersionProblem(text string) (problem string, isObject bool) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(text), &fields); err != nil || fields == nil {
		return "", false
	}
	raw, ok := fields["format_version"]
	if !ok {
		return fmt.Sprintf("the line carries no format_version: rlsbl reads only format_version %d changelog lines, and `rlsbl migrate records` converts a repository's changelog to it", FormatVersion), true
	}
	if strings.TrimSpace(string(raw)) != strconv.Itoa(FormatVersion) {
		return fmt.Sprintf("the line carries format_version %s: rlsbl reads only format_version %d changelog lines, and `rlsbl migrate records` converts a repository's changelog to it", strings.TrimSpace(string(raw)), FormatVersion), true
	}
	return "", true
}

// ParseLine reads one changelog line. The validator runs first and its
// diagnostics are the problems a refused line reports.
func ParseLine(text string) (Entry, error) {
	if problem, isObject := formatVersionProblem(text); problem != "" {
		return Entry{}, &LineError{Problems: []string{problem}, FormatVersion: true}
	} else if !isObject {
		return Entry{}, &LineError{Problems: []string{"the line is not a JSON object"}}
	}
	if _, diags := entryspec.ValidateBytes([]byte(text), "jsonl"); len(diags) > 0 {
		problems := make([]string, 0, len(diags))
		for _, d := range diags {
			problems = append(problems, fmt.Sprintf("%s [%s]", d.Message, d.Code))
		}
		return Entry{}, &LineError{Problems: problems}
	}
	dec := json.NewDecoder(strings.NewReader(text))
	dec.DisallowUnknownFields()
	var raw rawEntry
	if err := dec.Decode(&raw); err != nil {
		return Entry{}, &LineError{Problems: []string{err.Error()}}
	}
	return Entry{
		ID:          raw.ID,
		Commits:     raw.Commits,
		UserFacing:  raw.UserFacing,
		Description: raw.Description,
		Type:        raw.Type,
		Packages:    raw.Packages,
		BatchReason: raw.BatchReason,
	}, nil
}

// Validate checks an entry against the schema before it is written: the
// line it serializes to must read back as the same entry.
func Validate(e Entry) error {
	line := Serialize(e)
	back, err := ParseLine(line)
	if err != nil {
		return err
	}
	if Serialize(back) != line {
		return &LineError{Problems: []string{"the entry does not read back as written: " + line}}
	}
	return nil
}

// RemovalCommand is the `changelog remove` invocation that deletes e, for a
// finding to print. The id addresses the entry whatever else the file holds.
func RemovalCommand(e Entry) string {
	return "rlsbl changelog remove --id " + e.ID
}

// short is a commit id cut to 12 characters for a message.
func short(sha string) string {
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}
