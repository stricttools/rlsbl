package workspace

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

func scheme(t *testing.T, pattern string) TagScheme {
	t.Helper()
	s, err := NewTagScheme(pattern)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestATagSchemeRendersListsAndOwns(t *testing.T) {
	hygiene.Isolate(t)
	s := scheme(t, "kernel/v{version}")
	v, err := semver.Parse("0.2.0")
	if err != nil {
		t.Fatal(err)
	}
	if got := s.Render(v); got != "kernel/v0.2.0" {
		t.Errorf("Render = %s", got)
	}
	if got := s.ListGlob(); got != "kernel/v*" {
		t.Errorf("ListGlob = %s", got)
	}
	for _, c := range []struct {
		tag  string
		owns bool
	}{
		{"kernel/v0.2.0", true},
		{"kernel/vulkan/v0.1.0", false},
		{"kernel/v0.2", false},
		{"kernel/v0.2.0-rc.1", false},
		{"kernel/vlatest", false},
		{"v0.2.0", false},
	} {
		if got := s.Owns(c.tag); got != c.owns {
			t.Errorf("Owns(%s) = %t, want %t", c.tag, got, c.owns)
		}
	}
	if root := scheme(t, "v{version}"); root.Owns("draw/v0.1.0") {
		t.Error("the bare scheme owns a go path tag")
	}
}

func TestAGlobAndItsFormatAreOneScheme(t *testing.T) {
	hygiene.Isolate(t)
	from, err := SchemeFromGlob("portal@v*")
	if err != nil {
		t.Fatal(err)
	}
	if from != scheme(t, "portal@v{version}") || from.Pattern() != "portal@v{version}" {
		t.Errorf("SchemeFromGlob = %#v", from)
	}
	for _, glob := range []string{"portal@v", "*@v*", "v?*", "v[0-9]*"} {
		if _, err := SchemeFromGlob(glob); err == nil {
			t.Errorf("SchemeFromGlob(%s) was accepted", glob)
		}
	}
	for _, pattern := range []string{"v", "{version}{version}", "{name}@v{version}"} {
		if _, err := NewTagScheme(pattern); err == nil {
			t.Errorf("NewTagScheme(%s) was accepted", pattern)
		}
	}
}

func TestParseVersionTag(t *testing.T) {
	hygiene.Isolate(t)
	for _, c := range []struct {
		tag     string
		version string
		style   TagStyle
	}{
		{"v1.2.3", "1.2.3", TagStyleStandalone},
		{"portal@v1.2.3", "1.2.3", TagStyleMonorepo},
		{"@scope/pkg@v0.1.0", "0.1.0", TagStyleMonorepo},
		{"draw/cmd/v0.1.0", "0.1.0", TagStylePath},
	} {
		v, style, ok := ParseVersionTag(c.tag)
		if !ok || v.String() != c.version || style != c.style {
			t.Errorf("ParseVersionTag(%s) = %s, %s, %t", c.tag, v, style, ok)
		}
	}
	for _, tag := range []string{"latest", "v1.2", "v1.2.3-rc.1", "x1.2.3", "@v1.2.3", "/v1.2.3", "milestone-3"} {
		if _, _, ok := ParseVersionTag(tag); ok {
			t.Errorf("ParseVersionTag(%s) was accepted", tag)
		}
	}
}

func TestATagsOwnerIsTheReleasableItsSchemeRendersAs(t *testing.T) {
	hygiene.Isolate(t)
	w := newWorkspace(t, t.TempDir(), nested)
	for _, c := range [][2]string{
		{"draw/v0.1.0", "draw"},
		{"draw/cmd/v0.1.0", "cmd"},
		{"kernel@v1.0.0", "kernel"},
	} {
		r, ok, err := w.TagOwner(c[0])
		if err != nil || !ok || r.Name != c[1] {
			t.Errorf("TagOwner(%s) = %q, %t, %v", c[0], r.Name, ok, err)
		}
	}
	if r, ok, err := w.TagOwner("stray/v1.0.0"); ok || err != nil {
		t.Errorf("TagOwner(stray/v1.0.0) = %q, %t, %v", r.Name, ok, err)
	}
}

func TestTwoSchemesOwningOneTagAreRefusedNamingBoth(t *testing.T) {
	hygiene.Isolate(t)
	// Parse refuses such declarations; the model refuses them too.
	d := &declarations.Releasables{
		Layout:          declarations.LayoutWorkspace,
		ReleaseBranches: []string{"main"},
		Releasables: []declarations.Releasable{
			{Name: "a", TagFormat: "v{version}", PublishMode: declarations.PublishNone},
			{Name: "b", TagFormat: "v{version}", PublishMode: declarations.PublishNone},
		},
	}
	w, err := New(t.TempDir(), d)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := w.TagOwner("v1.0.0"); err == nil || !strings.Contains(err.Error(), "a, b") {
		t.Fatalf("TagOwner = %v", err)
	}
}
