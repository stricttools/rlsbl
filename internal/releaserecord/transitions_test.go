package releaserecord_test

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

const transitionsFile = ".strictmetadata/transitions/transitions.jsonl"

var recordedAt = time.Date(2026, 10, 7, 12, 30, 0, 0, time.FixedZone("CEST", 2*60*60))

// oneOfEach is one event of every name.
func oneOfEach() []releaserecord.Event {
	return []releaserecord.Event{
		&releaserecord.ConversionEvent{
			Direction:   "absorb",
			Source:      releaserecord.Endpoint{Repo: "https://github.com/acme/widget"},
			Destination: releaserecord.Endpoint{Repo: ".", Path: "widget", Project: "widget", Releasable: "widget", TagFormat: "{name}@v{version}"},
			Commit:      commitA,
		},
		&releaserecord.TagMapEvent{Releasable: "widget", Mappings: []releaserecord.TagMapping{{OldTag: "v0.1.0", NewTag: "widget@v0.1.0", NewCommit: commitB}}},
		&releaserecord.ReleaseCommitRemapEvent{Releasable: "widget", Rewrite: "safegit scrub", Mappings: []releaserecord.CommitMapping{{OldSHA: commitA, NewSHA: commitB}}},
		&releaserecord.DepartedGlobsEvent{Globs: []string{"gadget@v*"}, Destination: releaserecord.Endpoint{Repo: "https://github.com/acme/gadget"}},
		&releaserecord.BoundaryAliasEvent{Releasable: "widget", Aliases: []releaserecord.BoundaryAlias{{AliasTag: "widget@v0.1.0", AliasedTag: "v0.1.0", Commit: commitB}}},
	}
}

func appendEvents(t *testing.T, root string, events []releaserecord.Event) error {
	t.Helper()
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		return releaserecord.AppendEvents(e, root, events, recordedAt)
	})
	return err
}

func TestEveryEventIsNamedOnce(t *testing.T) {
	hygiene.Isolate(t)
	var names []releaserecord.EventName
	for _, e := range oneOfEach() {
		names = append(names, e.EventName())
	}
	if !reflect.DeepEqual(names, releaserecord.EventNames) {
		t.Fatalf("the fixtures name %v, the record %v", names, releaserecord.EventNames)
	}
}

func TestAppendedEventsReadBackStamped(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	events := oneOfEach()
	events[1].(*releaserecord.TagMapEvent).ID = "caller-chosen"
	events[1].(*releaserecord.TagMapEvent).RelatedTo = "conversion-1"
	mustNotFail(t, appendEvents(t, root, events))
	read, err := releaserecord.ReadEvents(root)
	mustNotFail(t, err)
	if !reflect.DeepEqual(read, events) {
		t.Fatalf("read back\n%#v\nwrote\n%#v", read, events)
	}
	for i, e := range read {
		h := releaserecord.Header(e)
		if h.RecordedAt != "2026-10-07T12:30:00+02:00" {
			t.Errorf("event %d recorded at %q", i, h.RecordedAt)
		}
		if i != 1 && len(h.ID) != 48 {
			t.Errorf("event %d id %q", i, h.ID)
		}
	}
	if releaserecord.Header(read[1]).ID != "caller-chosen" {
		t.Error("a caller's id was replaced")
	}
	text := readText(t, filepath.Join(root, transitionsFile))
	lines := strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	if len(lines) != len(events) {
		t.Fatalf("%d lines for %d events", len(lines), len(events))
	}
	for _, line := range lines {
		if !strings.HasPrefix(line, `{"format_version":2,"event":"`) {
			t.Errorf("a line does not lead with its format version and event: %s", line)
		}
		if strings.Contains(line, "null") || strings.Contains(line, `""`) {
			t.Errorf("an empty optional field was written: %s", line)
		}
	}
	requireContains(t, readText(t, filepath.Join(root, ".strictmetadata/transitions/manifest.toml")), `owner = "rlsbl"`)
}

func TestSeparateAppendsAccumulateWithoutRewritingEarlierLines(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	events := oneOfEach()
	mustNotFail(t, appendEvents(t, root, events[:1]))
	first := readText(t, filepath.Join(root, transitionsFile))
	mustNotFail(t, appendEvents(t, root, events[1:]))
	if !strings.HasPrefix(readText(t, filepath.Join(root, transitionsFile)), first) {
		t.Fatal("an append rewrote an earlier line")
	}
	read, err := releaserecord.ReadEvents(root)
	mustNotFail(t, err)
	if len(read) != len(events) {
		t.Fatalf("read %d events", len(read))
	}
}

func TestAnEmptyAppendWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	mustNotFail(t, appendEvents(t, root, nil))
	if _, err := os.Stat(filepath.Join(root, ".strictmetadata")); !os.IsNotExist(err) {
		t.Fatal("an empty append wrote")
	}
	events, err := releaserecord.ReadEvents(root)
	mustNotFail(t, err)
	if len(events) != 0 {
		t.Fatalf("an absent record read %v", events)
	}
}

func TestAnInvalidEventAbortsTheWholeAppend(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	events := oneOfEach()
	events = append(events, &releaserecord.TagMapEvent{})
	err := appendEvents(t, root, events)
	if err == nil {
		t.Fatal("a tag map with no mapping was appended")
	}
	if _, serr := os.Stat(filepath.Join(root, transitionsFile)); !os.IsNotExist(serr) {
		t.Fatal("part of the batch was written")
	}
}

func TestAnAppendAfterATornLastLineStartsItsOwnLine(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, transitionsFile), `{"format_version":2,"event":"tag-m`)
	mustNotFail(t, appendEvents(t, root, oneOfEach()[:1]))
	text := readText(t, filepath.Join(root, transitionsFile))
	if !strings.HasPrefix(text, "{\"format_version\":2,\"event\":\"tag-m\n{\"format_version\":2,\"event\":\"conversion\"") {
		t.Fatalf("the new event joined the torn line:\n%s", text)
	}
	// The torn line stays torn, and the read names it.
	_, err := releaserecord.ReadEvents(root)
	var te *releaserecord.TransitionError
	if !errors.As(err, &te) || te.Line != 1 {
		t.Fatalf("got %v", err)
	}
}

func TestBlankLinesAreSkipped(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	line, err := releaserecord.SerializeEvent(stamped(oneOfEach()[3], "id-1"))
	mustNotFail(t, err)
	testsupport.WriteFile(t, filepath.Join(root, transitionsFile), "\n"+line+"\n\n")
	events, err := releaserecord.ReadEvents(root)
	mustNotFail(t, err)
	if len(events) != 1 {
		t.Fatalf("read %d events", len(events))
	}
}

// stamped gives an event an id and a time, as an append would.
func stamped(e releaserecord.Event, id string) releaserecord.Event {
	switch ev := e.(type) {
	case *releaserecord.ConversionEvent:
		ev.ID, ev.RecordedAt = id, "2026-10-07T12:30:00+02:00"
	case *releaserecord.TagMapEvent:
		ev.ID, ev.RecordedAt = id, "2026-10-07T12:30:00+02:00"
	case *releaserecord.ReleaseCommitRemapEvent:
		ev.ID, ev.RecordedAt = id, "2026-10-07T12:30:00+02:00"
	case *releaserecord.DepartedGlobsEvent:
		ev.ID, ev.RecordedAt = id, "2026-10-07T12:30:00+02:00"
	case *releaserecord.BoundaryAliasEvent:
		ev.ID, ev.RecordedAt = id, "2026-10-07T12:30:00+02:00"
	}
	return e
}

func TestUnreadableLinesAreRefusedNamingTheLine(t *testing.T) {
	hygiene.Isolate(t)
	good := `{"format_version":2,"event":"departed-globs","id":"id-0","recorded_at":"2026-10-07T12:30:00+02:00","globs":["gadget@v*"],"destination":{"repo":"x"}}`
	cases := []struct {
		name, line string
	}{
		{"bad json", `{"format_version":2,`},
		{"not an object", `[1, 2]`},
		{"unknown event", strings.Replace(good, "departed-globs", "promotion-split-map", 1)},
		{"the moved identity event", strings.Replace(good, "departed-globs", "identity-transition", 1)},
		{"another discriminator", strings.Replace(good, `"event"`, `"type"`, 1)},
		{"missing event", strings.Replace(good, `"event":"departed-globs",`, "", 1)},
		{"missing field", strings.Replace(good, `"globs":["gadget@v*"],`, "", 1)},
		{"missing nested field", strings.Replace(good, `{"repo":"x"}`, `{}`, 1)},
		{"missing format version", strings.Replace(good, `"format_version":2,`, "", 1)},
		{"first format", strings.Replace(good, `"format_version":2`, `"format_version":1`, 1)},
		{"unknown key", strings.Replace(good, `"id":"id-0"`, `"id":"id-0","extra":1`, 1)},
		{"bad commit", `{"format_version":2,"event":"boundary-alias","id":"id-0","recorded_at":"2026-10-07T12:30:00+02:00","aliases":[{"alias_tag":"a","aliased_tag":"b","commit":"HEAD"}]}`},
		{"empty mappings", `{"format_version":2,"event":"tag-map","id":"id-0","recorded_at":"2026-10-07T12:30:00+02:00","mappings":[]}`},
		{"no offset", strings.Replace(good, "+02:00", "", 1)},
		{"bad direction", `{"format_version":2,"event":"conversion","id":"id-0","recorded_at":"2026-10-07T12:30:00+02:00","direction":"sideways","source":{"repo":"x"},"destination":{"repo":"."},"commit":"` + commitA + `"}`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hygiene.Isolate(t)
			root := t.TempDir()
			testsupport.WriteFile(t, filepath.Join(root, transitionsFile), strings.Replace(good, "id-0", "id-1", 1)+"\n"+c.line+"\n")
			_, err := releaserecord.ReadEvents(root)
			var te *releaserecord.TransitionError
			if !errors.As(err, &te) || te.Line != 2 {
				t.Fatalf("got %v", err)
			}
			requireContains(t, err.Error(), transitionsFile+":2:")
		})
	}
}

func TestInvalidUTF8IsRefusedNamingTheLine(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, transitionsFile), "\xff\xfe\n")
	_, err := releaserecord.ReadEvents(root)
	var te *releaserecord.TransitionError
	if !errors.As(err, &te) || te.Line != 1 || !strings.Contains(err.Error(), "UTF-8") {
		t.Fatalf("got %v", err)
	}
}

func TestADuplicateIDIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	events := oneOfEach()
	a, err := releaserecord.SerializeEvent(stamped(events[0], "same"))
	mustNotFail(t, err)
	b, err := releaserecord.SerializeEvent(stamped(events[4], "same"))
	mustNotFail(t, err)
	testsupport.WriteFile(t, filepath.Join(root, transitionsFile), a+"\n"+b+"\n")
	_, err = releaserecord.ReadEvents(root)
	var te *releaserecord.TransitionError
	if !errors.As(err, &te) || te.Line != 2 || !strings.Contains(err.Error(), "already used on line 1") {
		t.Fatalf("got %v", err)
	}
}
