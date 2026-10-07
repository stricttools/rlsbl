package changelog_test

import (
	"errors"
	"regexp"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
)

const sha1 = "1111111111111111111111111111111111111111"

func TestAnEntryReadsBackAsItWasWritten(t *testing.T) {
	hygiene.Isolate(t)
	e := changelog.Entry{
		ID:          id("a1"),
		Commits:     []string{sha1},
		UserFacing:  true,
		Description: "Add <the> `widget` & gadget — fast",
		Type:        changelog.TypeFeature,
		Packages:    []string{"widget"},
		BatchReason: "one change",
	}
	text := changelog.Serialize(e)
	want := `{"format_version":2,"id":"` + id("a1") + `","commits":["` + sha1 + `"],"user_facing":true,"description":"Add <the> ` + "`widget`" + ` & gadget — fast","type":"feature","packages":["widget"],"batch_reason":"one change"}`
	if text != want {
		t.Fatalf("serialized:\n%s\nwant:\n%s", text, want)
	}
	back, err := changelog.ParseLine(text)
	mustNotFail(t, err)
	if changelog.Serialize(back) != text {
		t.Fatalf("read back as %+v", back)
	}
	mustNotFail(t, changelog.Validate(e))
}

func TestAbsentOptionalFieldsStayAbsentAndAnEmptyPackagesListStays(t *testing.T) {
	hygiene.Isolate(t)
	bare := internalEntry("b", sha1)
	text := changelog.Serialize(bare)
	if strings.Contains(text, "description") || strings.Contains(text, "packages") || strings.Contains(text, "batch_reason") || strings.Contains(text, `"type"`) {
		t.Fatalf("absent fields were written: %s", text)
	}
	bare.Packages = []string{}
	text = changelog.Serialize(bare)
	back, err := changelog.ParseLine(text)
	mustNotFail(t, err)
	if back.Packages == nil || changelog.Serialize(back) != text {
		t.Fatalf("an empty packages list read back as %+v", back)
	}
}

func lineError(t *testing.T, err error) *changelog.LineError {
	t.Helper()
	var le *changelog.LineError
	if !errors.As(err, &le) {
		t.Fatalf("want a line error, got %T: %v", err, err)
	}
	return le
}

func TestALineOfTheFirstFormatIsRefusedNamingTheMigration(t *testing.T) {
	hygiene.Isolate(t)
	for _, text := range []string{
		`{"format_version":1,"id":"` + id("1") + `","commits":["` + sha1 + `"],"user_facing":false}`,
		`{"commits":["` + sha1 + `"],"user_facing":false}`,
	} {
		_, err := changelog.ParseLine(text)
		le := lineError(t, err)
		if !le.FormatVersion {
			t.Errorf("%s: not reported as a format version problem: %v", text, err)
		}
		requireContains(t, err.Error(), "format_version 2", "rlsbl migrate records")
	}
}

func TestEveryShapeProblemOfALineIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	good := `"format_version":2,"id":"` + id("1") + `","commits":["` + sha1 + `"]`
	for name, text := range map[string]string{
		"no id":                        `{"format_version":2,"commits":["` + sha1 + `"],"user_facing":false}`,
		"an id that is not 48 hex":     `{"format_version":2,"id":"8d633d4906fb4ef99313b76ddf5e78cf","commits":["` + sha1 + `"],"user_facing":false}`,
		"no commits":                   `{"format_version":2,"id":"` + id("1") + `","commits":[],"user_facing":false}`,
		"no user_facing":               `{` + good + `}`,
		"user-facing, no description":  `{` + good + `,"user_facing":true,"type":"fix"}`,
		"user-facing, no type":         `{` + good + `,"user_facing":true,"description":"d"}`,
		"a type outside the set":       `{` + good + `,"user_facing":true,"description":"d","type":"chore"}`,
		"an unknown key":               `{` + good + `,"user_facing":false,"release_type":"ota"}`,
		"an empty batch reason":        `{` + good + `,"user_facing":false,"batch_reason":""}`,
		"a packages value of a string": `{` + good + `,"user_facing":false,"packages":"widget"}`,
	} {
		_, err := changelog.ParseLine(text)
		if err == nil {
			t.Errorf("%s: accepted %s", name, text)
			continue
		}
		if le := lineError(t, err); le.FormatVersion || len(le.Problems) == 0 {
			t.Errorf("%s: %+v", name, le)
		}
	}
	if _, err := changelog.ParseLine("[1,2]"); err == nil || !strings.Contains(err.Error(), "not a JSON object") {
		t.Errorf("an array line: %v", err)
	}
}

func TestNewIDsAreFortyEightHexDigitsLedByTheTime(t *testing.T) {
	hygiene.Isolate(t)
	a, b := changelog.NewID(), changelog.NewID()
	pattern := regexp.MustCompile(`^[0-9a-f]{16}[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$`)
	for _, x := range []string{a, b} {
		if !pattern.MatchString(x) {
			t.Errorf("%q is not a timestamp and a UUID4", x)
		}
	}
	if a == b {
		t.Error("two ids are equal")
	}
	if a[:16] > b[:16] {
		t.Errorf("the later id %s sorts before %s", b, a)
	}
}

func TestRemovalCommandAddressesTheEntryByID(t *testing.T) {
	hygiene.Isolate(t)
	if got := changelog.RemovalCommand(feature("c", "d", sha1)); got != "rlsbl changelog remove --id "+id("c") {
		t.Fatalf("got %q", got)
	}
}
