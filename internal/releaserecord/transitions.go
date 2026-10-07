package releaserecord

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/releaserecord/transitionspec"
)

// The transition record is an append-only log of repository surgery, one
// JSON object per line in .strictmetadata/transitions/transitions.jsonl: the
// conversions extract and absorb performed, the tag renames, the commit
// correspondence of history rewrites, the tag globs that left with an
// extracted releasable, and the alias tags made at a boundary. It records
// history and never drives it: a reader consults it to explain a divergence
// it already observed. Renames, identities, closed release histories, and
// tags outside the version model are facts of the lifecycle-and-license
// record, not of this one.

// TransitionFormatVersion is the format version of a transition record line.
const TransitionFormatVersion = 2

// EventName is which event a transition record line records.
type EventName string

// The events.
const (
	EventConversion         EventName = "conversion"
	EventTagMap             EventName = "tag-map"
	EventReleaseCommitRemap EventName = "release-commit-remap"
	EventDepartedGlobs      EventName = "departed-globs"
	EventBoundaryAlias      EventName = "boundary-alias"
)

// EventNames is every event, in the order the schema declares them.
var EventNames = []EventName{EventConversion, EventTagMap, EventReleaseCommitRemap, EventDepartedGlobs, EventBoundaryAlias}

// EventHeader is what every event carries. ID and RecordedAt are stamped by
// AppendEvents when empty, so a writer states only its fact.
type EventHeader struct {
	ID string `json:"id"`
	// RecordedAt is RFC 3339 with an offset, to the second.
	RecordedAt string `json:"recorded_at"`
	// RelatedTo is the id of an earlier event this one elaborates.
	RelatedTo string `json:"related_to,omitempty"`
}

// Event is one transition record event.
type Event interface {
	EventName() EventName
	header() *EventHeader
}

// Endpoint is one side of a conversion.
type Endpoint struct {
	// Repo is a remote URL for another repository, or "." for this one.
	Repo       string `json:"repo"`
	Path       string `json:"path,omitempty"`
	Project    string `json:"project,omitempty"`
	Releasable string `json:"releasable,omitempty"`
	TagFormat  string `json:"tag_format,omitempty"`
}

// TagMapping is one old-tag to new-tag correspondence.
type TagMapping struct {
	OldTag    string `json:"old_tag"`
	NewTag    string `json:"new_tag"`
	OldCommit string `json:"old_commit,omitempty"`
	NewCommit string `json:"new_commit"`
}

// CommitMapping is one old-commit to new-commit correspondence.
type CommitMapping struct {
	OldSHA string `json:"old_sha"`
	NewSHA string `json:"new_sha"`
}

// BoundaryAlias is one alias tag created at a boundary.
type BoundaryAlias struct {
	AliasTag   string `json:"alias_tag"`
	AliasedTag string `json:"aliased_tag"`
	Commit     string `json:"commit"`
}

// ConversionEvent is a releasable extracted out of a workspace, or a
// repository absorbed into one.
type ConversionEvent struct {
	EventHeader
	// Direction is "extract" or "absorb".
	Direction   string   `json:"direction"`
	Source      Endpoint `json:"source"`
	Destination Endpoint `json:"destination"`
	Commit      string   `json:"commit"`
}

// TagMapEvent is the tag renames a conversion performed.
type TagMapEvent struct {
	EventHeader
	Releasable string       `json:"releasable,omitempty"`
	Mappings   []TagMapping `json:"mappings"`
}

// ReleaseCommitRemapEvent is the commit correspondence a history rewrite
// produced for the commits rlsbl's records name.
type ReleaseCommitRemapEvent struct {
	EventHeader
	Releasable string          `json:"releasable,omitempty"`
	Rewrite    string          `json:"rewrite"`
	Mappings   []CommitMapping `json:"mappings"`
}

// DepartedGlobsEvent is tag globs that left with an extracted releasable.
type DepartedGlobsEvent struct {
	EventHeader
	Globs       []string `json:"globs"`
	Destination Endpoint `json:"destination"`
}

// BoundaryAliasEvent is alias tags created at a conversion or a rename.
type BoundaryAliasEvent struct {
	EventHeader
	Releasable string          `json:"releasable,omitempty"`
	Aliases    []BoundaryAlias `json:"aliases"`
}

func (e *ConversionEvent) EventName() EventName         { return EventConversion }
func (e *TagMapEvent) EventName() EventName             { return EventTagMap }
func (e *ReleaseCommitRemapEvent) EventName() EventName { return EventReleaseCommitRemap }
func (e *DepartedGlobsEvent) EventName() EventName      { return EventDepartedGlobs }
func (e *BoundaryAliasEvent) EventName() EventName      { return EventBoundaryAlias }

func (e *ConversionEvent) header() *EventHeader         { return &e.EventHeader }
func (e *TagMapEvent) header() *EventHeader             { return &e.EventHeader }
func (e *ReleaseCommitRemapEvent) header() *EventHeader { return &e.EventHeader }
func (e *DepartedGlobsEvent) header() *EventHeader      { return &e.EventHeader }
func (e *BoundaryAliasEvent) header() *EventHeader      { return &e.EventHeader }

// Header is the event's common fields.
func Header(e Event) EventHeader { return *e.header() }

// newEvent is the empty event of a name, and false for a name that is no
// event's.
func newEvent(name EventName) (Event, bool) {
	switch name {
	case EventConversion:
		return &ConversionEvent{}, true
	case EventTagMap:
		return &TagMapEvent{}, true
	case EventReleaseCommitRemap:
		return &ReleaseCommitRemapEvent{}, true
	case EventDepartedGlobs:
		return &DepartedGlobsEvent{}, true
	case EventBoundaryAlias:
		return &BoundaryAliasEvent{}, true
	}
	return nil, false
}

// lineHead is the two fields every line leads with.
type lineHead struct {
	FormatVersion int       `json:"format_version"`
	Event         EventName `json:"event"`
}

// SerializeEvent renders one event as its line, without the newline:
// format_version and event first, then the header, then the event's fields;
// optional fields that are empty are left out.
func SerializeEvent(e Event) (string, error) {
	head, err := marshal(lineHead{FormatVersion: TransitionFormatVersion, Event: e.EventName()})
	if err != nil {
		return "", err
	}
	body, err := marshal(e)
	if err != nil {
		return "", err
	}
	// Both are JSON objects: splice the event's members after the head's.
	if string(body) == "{}" {
		return string(head), nil
	}
	return string(head[:len(head)-1]) + "," + string(body[1:]), nil
}

// marshal renders v as compact JSON, leaving <, >, and & as they are.
func marshal(v any) ([]byte, error) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(b.Bytes(), []byte("\n")), nil
}

// ParseEvent parses one line: the generated validator (the format version,
// the event, every field's type, required fields, unknown keys), then the
// strict decode into the event's type.
func ParseEvent(line string) (Event, error) {
	if !utf8.ValidString(line) {
		return nil, fmt.Errorf("the line is not valid UTF-8")
	}
	if _, diags := transitionspec.ValidateBytes([]byte(line), "json"); len(diags) > 0 {
		return nil, fmt.Errorf("%s", strings.Join(diagnosticProblems(diags), "; "))
	}
	var head lineHead
	if err := json.Unmarshal([]byte(line), &head); err != nil {
		return nil, err
	}
	e, ok := newEvent(head.Event)
	if !ok {
		return nil, fmt.Errorf("unknown event %q", head.Event)
	}
	// The event's decode sees every member but the two the head already
	// read, so an unknown key is refused.
	var members map[string]json.RawMessage
	if err := json.Unmarshal([]byte(line), &members); err != nil {
		return nil, err
	}
	delete(members, "format_version")
	delete(members, "event")
	rest, err := json.Marshal(members)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(rest))
	dec.DisallowUnknownFields()
	if err := dec.Decode(e); err != nil {
		return nil, err
	}
	return e, nil
}

// newEventID is a fresh event id: 16 hexadecimal digits of the nanosecond
// time, then 32 of a random UUID4, so ids sort roughly by creation.
func newEventID(now time.Time) (string, error) {
	var u [16]byte
	if _, err := rand.Read(u[:]); err != nil {
		return "", fmt.Errorf("reading randomness for an event id: %w", err)
	}
	u[6] = (u[6] & 0x0f) | 0x40
	u[8] = (u[8] & 0x3f) | 0x80
	return fmt.Sprintf("%016x", uint64(now.UnixNano())) + hex.EncodeToString(u[:]), nil
}

// AppendEvents appends events to the repository's transition record in the
// order given. Each event without an ID or a RecordedAt is stamped, on the
// event itself, with a fresh id and now. Every event is validated before
// anything is written, so an invalid one aborts the whole append; an empty
// list writes nothing.
//
// The effects handle has no append, so the record is read and written back
// whole, atomically: lines already in it are written back byte for byte, and
// a last line missing its newline gets one, so the new events start their
// own lines. Two processes appending at once can lose one append; only
// commands holding the release lock write the record.
func AppendEvents(e *strictcli.Effects, root string, events []Event, now time.Time) error {
	if len(events) == 0 {
		return nil
	}
	var lines []string
	for _, ev := range events {
		h := ev.header()
		if h.ID == "" {
			id, err := newEventID(now)
			if err != nil {
				return err
			}
			h.ID = id
		}
		if h.RecordedAt == "" {
			h.RecordedAt = now.Format(time.RFC3339)
		}
		line, err := SerializeEvent(ev)
		if err != nil {
			return err
		}
		if _, err := ParseEvent(line); err != nil {
			return fmt.Errorf("refusing to append a %s event the record's reader would refuse: %w", ev.EventName(), err)
		}
		lines = append(lines, line)
	}
	existing, _, err := readFile(root, declarations.TransitionsFile)
	if err != nil {
		return err
	}
	var b bytes.Buffer
	b.Write(existing)
	if len(existing) > 0 && existing[len(existing)-1] != '\n' {
		b.WriteByte('\n')
	}
	for _, line := range lines {
		b.WriteString(line)
		b.WriteByte('\n')
	}
	return writeFile(e, root, declarations.TransitionsFile, b.Bytes(), releaseFileMode)
}

// TransitionError is a transition record rlsbl cannot read in full.
type TransitionError struct {
	File string
	// Line is the 1-based line, and 0 when the problem is the whole file's.
	Line    int
	Problem string
}

func (e *TransitionError) Error() string {
	if e.Line == 0 {
		return e.File + ": " + e.Problem
	}
	return fmt.Sprintf("%s:%d: %s", e.File, e.Line, e.Problem)
}

// ReadEvents reads the repository's transition record, events in order. A
// missing record holds no event: the repository never had surgery recorded.
// Any line rlsbl cannot read (not UTF-8, not JSON, an unknown event, a
// missing field, another format version) and an id used twice are refused,
// naming the line: a record that cannot be read in full is not read in
// part. Blank lines are skipped.
func ReadEvents(root string) ([]Event, error) {
	data, found, err := readFile(root, declarations.TransitionsFile)
	if err != nil || !found {
		return nil, err
	}
	var events []Event
	seen := map[string]int{}
	for i, raw := range strings.Split(string(data), "\n") {
		n := i + 1
		line := strings.TrimSpace(raw)
		if line == "" {
			continue
		}
		ev, err := ParseEvent(line)
		if err != nil {
			return nil, &TransitionError{File: declarations.TransitionsFile, Line: n, Problem: err.Error()}
		}
		id := ev.header().ID
		if first, dup := seen[id]; dup {
			return nil, &TransitionError{File: declarations.TransitionsFile, Line: n, Problem: fmt.Sprintf("the event id %q is already used on line %d; ids are unique within the transition record", id, first)}
		}
		seen[id] = n
		events = append(events, ev)
	}
	return events, nil
}
