package registry

import (
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestNpmNameProblems(t *testing.T) {
	hygiene.Isolate(t)
	for _, name := range []string{"portal", "@acme/portal", "widget-2", "a.b_c"} {
		if p := NpmNameProblems(name); len(p) != 0 {
			t.Errorf("%q: %v", name, p)
		}
	}
	for name, want := range map[string]string{
		"":             "must not be empty",
		".portal":      "start with a period",
		"_portal":      "start with an underscore",
		" portal":      "leading or trailing spaces",
		"node_modules": "blocked npm package name",
		"fs":           "Node core module",
		"Portal":       "uppercase",
		"por tal":      "URL-friendly",
		"portal!":      "special characters",
		strings.Repeat("a", 215): "longer than 214",
	} {
		if p := NpmNameProblems(name); !strings.Contains(strings.Join(p, "\n"), want) {
			t.Errorf("%q: %v, want one naming %q", name, p, want)
		}
	}
}

func TestPypiNameProblems(t *testing.T) {
	hygiene.Isolate(t)
	for _, name := range []string{"gadget", "Gadget_2", "a", "a.b-c"} {
		if p := PypiNameProblems(name); len(p) != 0 {
			t.Errorf("%q: %v", name, p)
		}
	}
	for _, name := range []string{"", "-gadget", "gadget-", "gad get", "gädget"} {
		if p := PypiNameProblems(name); len(p) != 1 || !strings.Contains(p[0], "PEP 508") {
			t.Errorf("%q: %v", name, p)
		}
	}
}

func TestNormalization(t *testing.T) {
	hygiene.Isolate(t)
	if got := NormalizeNpm("My-Pkg.Name_x"); got != "mypkgnamex" {
		t.Errorf("npm: %q", got)
	}
	if got := NormalizePypi("My__Pkg.-Name"); got != "my-pkg-name" {
		t.Errorf("pypi: %q", got)
	}
	if got := ultranormalize("Lo-Il_O"); got != "10110" {
		t.Errorf("ultranormalize: %q", got)
	}
}

func TestNpmVariants(t *testing.T) {
	hygiene.Isolate(t)
	got := npmVariants("foo-bar")
	want := []string{"foo.bar", "foo_bar", "foobar"}
	if !slices.Equal(got, want) {
		t.Errorf("foo-bar: %v", got)
	}
	got = npmVariants("abc")
	want = []string{"a-bc", "a.bc", "a_bc", "ab-c", "ab.c", "ab_c"}
	sort.Strings(want)
	if !slices.Equal(got, want) {
		t.Errorf("abc: %v", got)
	}
}

func TestPypiVariantsCapTheInsertions(t *testing.T) {
	hygiene.Isolate(t)
	if _, capped := pypiVariants("abcdef"); capped {
		t.Error("a short name was capped")
	}
	variants, capped := pypiVariants("abcdefghijklmnop")
	if !capped || len(variants) > pypiInsertionCap+4 {
		t.Errorf("a long name: %d variants, capped %v", len(variants), capped)
	}
	if v, _ := pypiVariants("foo_bar"); !slices.Contains(v, "foo-bar") || slices.Contains(v, "foo_bar") {
		t.Errorf("foo_bar: %v", v)
	}
}

func TestUltranormVariants(t *testing.T) {
	hygiene.Isolate(t)
	got, capped := ultranormVariants("lo")
	if capped || !slices.Equal(got, []string{"l0", "1o", "10"}) {
		t.Errorf("lo: %v %v", got, capped)
	}
	if _, capped := ultranormVariants("lolololo"); !capped {
		t.Error("256 combinations were not capped")
	}
}
