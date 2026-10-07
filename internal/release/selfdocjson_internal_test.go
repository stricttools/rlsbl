package release

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestTheSelfdocVersionWriteKeepsEveryOtherByte(t *testing.T) {
	hygiene.Isolate(t)
	in := "{\n    \"name\": \"portal\",\n    \"version\": \"0.4.0\",\n    \"nested\": {\"version\": \"9.9.9\"},\n    \"versions\": [\n        {\"version\": \"0.3.0\", \"label\": \"old\"},\n        {\"label\": \"current\", \"version\": \"0.4.0\"}\n    ],\n    \"note\": \"ünïcode\"\n}\n"
	out, changed, err := bumpSelfdocJSON([]byte(in), "0.5.0")
	if err != nil || !changed {
		t.Fatalf("%v %v", changed, err)
	}
	want := strings.Replace(in, "\"version\": \"0.4.0\",", "\"version\": \"0.5.0\",", 1)
	want = strings.Replace(want, "\"label\": \"current\", \"version\": \"0.4.0\"", "\"label\": \"current\", \"version\": \"0.5.0\"", 1)
	if string(out) != want {
		t.Errorf("got:\n%s\nwant:\n%s", out, want)
	}
	again, changed, err := bumpSelfdocJSON(out, "0.5.0")
	if err != nil || changed || string(again) != string(out) {
		t.Errorf("a second write changed the file: %v %v", changed, err)
	}
}

func TestASelfdocFileDeclaringNoVersionIsLeftAlone(t *testing.T) {
	hygiene.Isolate(t)
	in := "{\"name\": \"portal\", \"versions\": []}\n"
	out, changed, err := bumpSelfdocJSON([]byte(in), "0.5.0")
	if err != nil || changed || string(out) != in {
		t.Errorf("%q %v %v", out, changed, err)
	}
}

func TestASelfdocVersionThatIsNotAStringIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	if _, _, err := bumpSelfdocJSON([]byte(`{"version": 4}`), "0.5.0"); err == nil || !strings.Contains(err.Error(), "not a string") {
		t.Errorf("a numeric version was accepted: %v", err)
	}
	if _, _, err := bumpSelfdocJSON([]byte(`[1]`), "0.5.0"); err == nil {
		t.Error("a document that is no object was accepted")
	}
}
