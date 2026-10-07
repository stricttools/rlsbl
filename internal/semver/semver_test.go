package semver

import (
	"math"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestBumpMovesTheVersion(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		version string
		bump    Bump
		want    string
	}{
		{"1.2.3", Patch, "1.2.4"},
		{"1.2.3", Minor, "1.3.0"},
		{"1.2.3", Major, "2.0.0"},
		{"1.2.3", Infra, "1.2.4"},
		{"0.1.0", Patch, "0.1.1"},
		{"0.1.0", Minor, "0.2.0"},
		{"0.1.0", Major, "1.0.0"},
		{"3.2.1", Patch, "3.2.2"},
		{"3.2.1", Minor, "3.3.0"},
		{"3.2.1", Major, "4.0.0"},
		{"0.0.0", Patch, "0.0.1"},
		{"0.131.0", Minor, "0.132.0"},
	}
	for _, c := range cases {
		v, err := Parse(c.version)
		if err != nil {
			t.Fatalf("Parse(%q): %v", c.version, err)
		}
		got, err := v.Bump(c.bump)
		if err != nil {
			t.Fatalf("%s bump of %s: %v", c.bump, c.version, err)
		}
		if got.String() != c.want {
			t.Errorf("%s bump of %s = %s, want %s", c.bump, c.version, got, c.want)
		}
	}
}

func TestParseRefusesEverythingButTheReleaseForm(t *testing.T) {
	hygiene.Isolate(t)
	for _, s := range []string{
		"", "not-a-version", "1.2", "1.2.3.4", "1.2.x", "v1.2.3", " 1.2.3", "1.2.3 ",
		"01.2.3", "1.02.3", "1.2.03", "1..3", ".1.2", "1.2.", "1.2.-3",
		"18446744073709551616.0.0",
	} {
		if _, err := Parse(s); err == nil {
			t.Errorf("Parse(%q) accepted a string that is not a release version", s)
		}
	}
}

// The pre-release channel is dropped: a pre-release version is refused, never
// truncated to its base and bumped as the base.
func TestParseRefusesAPreReleaseSuffixAndBuildMetadata(t *testing.T) {
	hygiene.Isolate(t)
	for _, s := range []string{"1.0.0-beta.1", "2.3.0-rc.2", "0.5.0-alpha.3", "1.0.0-rc"} {
		_, err := Parse(s)
		if err == nil || !strings.Contains(err.Error(), "pre-release") {
			t.Errorf("Parse(%q) = %v, want a refusal naming the pre-release channel", s, err)
		}
	}
	_, err := Parse("1.0.0+build.5")
	if err == nil || !strings.Contains(err.Error(), "build metadata") {
		t.Errorf("Parse with build metadata = %v, want a refusal naming build metadata", err)
	}
}

func TestParseRoundTrips(t *testing.T) {
	hygiene.Isolate(t)
	for _, s := range []string{"0.0.0", "1.2.3", "10.20.30", "0.131.0", "18446744073709551615.0.1"} {
		v, err := Parse(s)
		if err != nil {
			t.Fatalf("Parse(%q): %v", s, err)
		}
		if v.String() != s {
			t.Errorf("Parse(%q).String() = %q", s, v.String())
		}
		if !Valid(s) {
			t.Errorf("Valid(%q) = false", s)
		}
	}
}

func TestParseBump(t *testing.T) {
	hygiene.Isolate(t)
	for _, b := range Bumps {
		got, err := ParseBump(string(b))
		if err != nil || got != b {
			t.Errorf("ParseBump(%q) = %q, %v", b, got, err)
		}
	}
	_, err := ParseBump("prerelease")
	if err == nil || !strings.Contains(err.Error(), "no pre-release channel") {
		t.Errorf("ParseBump(prerelease) = %v, want the pre-release refusal", err)
	}
	_, err = ParseBump("mega")
	if err == nil || !strings.Contains(err.Error(), "patch, minor, major, infra") {
		t.Errorf("ParseBump(mega) = %v, want a refusal naming every bump", err)
	}
	if _, err := (Version{Major: 1}).Bump(Bump("mega")); err == nil {
		t.Error("an unknown bump moved a version")
	}
}

func TestBumpRefusesAComponentAtItsLargestValue(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := (Version{Patch: math.MaxUint64}).Bump(Patch); err == nil {
		t.Error("a patch bump wrapped around")
	}
	if _, err := (Version{Minor: math.MaxUint64}).Bump(Minor); err == nil {
		t.Error("a minor bump wrapped around")
	}
	if _, err := (Version{Major: math.MaxUint64}).Bump(Major); err == nil {
		t.Error("a major bump wrapped around")
	}
}

func TestCompareIsNumericPerComponent(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		a, b string
		want int
	}{
		{"1.2.3", "1.2.3", 0},
		{"1.2.3", "1.2.4", -1},
		{"1.10.0", "1.9.9", 1},
		{"2.0.0", "10.0.0", -1},
		{"0.0.10", "0.0.9", 1},
	}
	for _, c := range cases {
		a, _ := Parse(c.a)
		b, _ := Parse(c.b)
		if got := Compare(a, b); got != c.want {
			t.Errorf("Compare(%s, %s) = %d, want %d", c.a, c.b, got, c.want)
		}
	}
}

func TestSortAndHighest(t *testing.T) {
	hygiene.Isolate(t)
	var vs []Version
	for _, s := range []string{"0.10.0", "0.2.0", "1.0.0", "0.9.12"} {
		v, _ := Parse(s)
		vs = append(vs, v)
	}
	Sort(vs)
	var got []string
	for _, v := range vs {
		got = append(got, v.String())
	}
	if strings.Join(got, " ") != "0.2.0 0.9.12 0.10.0 1.0.0" {
		t.Errorf("Sort = %v", got)
	}
	h, ok := Highest(vs)
	if !ok || h.String() != "1.0.0" {
		t.Errorf("Highest = %s, %v", h, ok)
	}
	if _, ok := Highest(nil); ok {
		t.Error("Highest of nothing reported a version")
	}
}
