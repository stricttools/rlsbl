package targets

import (
	"slices"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

var v123 = semver.Version{Major: 1, Minor: 2, Patch: 3}

func goMember(path string, publishes bool) RefMember {
	return RefMember{Path: path, Targets: []declarations.Target{{Name: Go}}, Publishes: publishes}
}

func TestEveryPublishingGoMemberOwesItsProxyTag(t *testing.T) {
	hygiene.Isolate(t)
	refs, err := ExpectedRefsOf(v123, RefInputs{
		SchemeTag: "gfx/v1.2.3",
		Members:   []RefMember{goMember("gfx", true), goMember("gfx/shader", true), goMember("tools", false), {Path: "web", Targets: []declarations.Target{{Name: NPM}}, Publishes: true}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if refs.Primary != "gfx/v1.2.3" || !slices.Equal(refs.Companions, []string{"gfx/shader/v1.2.3"}) || refs.SchemeSpelling != "" {
		t.Fatalf("refs = %+v", refs)
	}
	if !slices.Equal(refs.Tags(), []string{"gfx/v1.2.3", "gfx/shader/v1.2.3"}) {
		t.Fatalf("Tags = %q", refs.Tags())
	}
}

func TestARootGoModuleOwesTheProxyTagWhenTheTagFormatDiffers(t *testing.T) {
	hygiene.Isolate(t)
	refs, err := ExpectedRefsOf(v123, RefInputs{SchemeTag: "portal@v1.2.3", Members: []RefMember{goMember(".", true)}})
	if err != nil || !slices.Equal(refs.Companions, []string{"v1.2.3"}) {
		t.Fatalf("refs = %+v, %v", refs, err)
	}
	refs, err = ExpectedRefsOf(v123, RefInputs{SchemeTag: "v1.2.3", Members: []RefMember{goMember(".", true)}})
	if err != nil || len(refs.Companions) != 0 {
		t.Fatalf("a companion equal to the primary was repeated: %+v, %v", refs, err)
	}
}

func TestAReleasedVersionOwesOnlyTheMembersItRecorded(t *testing.T) {
	hygiene.Isolate(t)
	refs, err := ExpectedRefsOf(v123, RefInputs{
		SchemeTag:     "v1.2.3",
		Members:       []RefMember{goMember("gfx", true), goMember("later", true)},
		RecordedPaths: []string{".", "gfx"},
	})
	if err != nil || !slices.Equal(refs.Companions, []string{"gfx/v1.2.3"}) {
		t.Fatalf("refs = %+v, %v", refs, err)
	}
}

func TestAShippedSpellingIsThePrimaryAndTheSchemeSpellingIsKeptApart(t *testing.T) {
	hygiene.Isolate(t)
	refs, err := ExpectedRefsOf(v123, RefInputs{SchemeTag: "portal@v1.2.3", ShippedAs: "widget@v1.2.3", Aliases: []string{"widget@v1.2.3"}})
	if err != nil {
		t.Fatal(err)
	}
	if refs.Primary != "widget@v1.2.3" || refs.ShippedAs != "widget@v1.2.3" || refs.SchemeSpelling != "portal@v1.2.3" {
		t.Fatalf("refs = %+v", refs)
	}
	if !slices.Equal(refs.Tags(), []string{"widget@v1.2.3"}) || !slices.Equal(refs.Spellings(), []string{"widget@v1.2.3", "portal@v1.2.3"}) {
		t.Fatalf("Tags %q, Spellings %q", refs.Tags(), refs.Spellings())
	}
	refs, err = ExpectedRefsOf(v123, RefInputs{SchemeTag: "portal@v1.2.3", ShippedAs: "widget@v1.2.3", Aliases: []string{"portal@v1.2.3"}})
	if err != nil || refs.SchemeSpelling != "" || !slices.Equal(refs.Tags(), []string{"widget@v1.2.3", "portal@v1.2.3"}) {
		t.Fatalf("a scheme spelling an alias names: %+v, %v", refs, err)
	}
}
