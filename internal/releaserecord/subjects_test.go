package releaserecord_test

import (
	"slices"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaserecord"
)

func TestTheVersionBumpSubjectsAreBothSpellings(t *testing.T) {
	hygiene.Isolate(t)
	got := releaserecord.VersionBumpSubjects("widget", "widget@v0.4.0", version(t, "0.4.0"))
	if !slices.Equal(got, []string{"widget@v0.4.0", "widget: release v0.4.0"}) {
		t.Fatalf("%q", got)
	}
}

func TestFinalizationSubjectsNameTheirVersion(t *testing.T) {
	hygiene.Isolate(t)
	v := version(t, "0.4.0")
	for _, subject := range []string{
		"chore: finalize changelog for 0.4.0",
		"chore: finalize release file for 0.4.0",
		"chore: regenerate 0.4.0.md from archived release metadata",
		"chore: clean 3 stale batch exclusion(s) from config.json",
		"snapshot",
	} {
		if !releaserecord.IsFinalizationSubject(subject, v) {
			t.Fatalf("%q is not recognized", subject)
		}
	}
	for _, subject := range []string{
		"chore: finalize changelog for 0.3.0",
		"chore: finalize release file for 0.4.1",
		"fix: the parser",
		"chore: clean stale batch exclusions",
	} {
		if releaserecord.IsFinalizationSubject(subject, v) {
			t.Fatalf("%q is recognized", subject)
		}
	}
}
