package releaserecord_test

import (
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

const lifecycleRecord = `format_version = 1

[[identities]]
subject = "gadget"
facet = "releasable-name"
value = "gizmo"
registry = ""
tag_patterns = ["gizmo@v*"]
from = 2025-01-01
until = 2026-01-01
reason = "the first name"

[[identities]]
subject = "gadget"
facet = "releasable-name"
value = "gadget"
registry = ""
tag_patterns = ["gadget@v*"]
from = 2026-01-01
reason = "renamed"

[[unversioned_tags]]
tag = "nightly"
reason = "a moving tag CI publishes"
recorded = 2026-02-01
`

func day(t *testing.T, s string) time.Time {
	t.Helper()
	d, err := time.Parse(time.DateOnly, s)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func explanations(t *testing.T) *releaserecord.Explanations {
	t.Helper()
	root := t.TempDir()
	writeArchiveWith(t, root, "0.3.0", "recorded", commitA, "shipped_as = \"gizmo@v0.3.0\"\n")
	writeArchive(t, root, "0.4.0", "recorded", commitB)
	retired := filepath.Join(root, releaserecord.RetiredArchiveDir("widget"), "v1.0.0.toml")
	testsupport.WriteFile(t, retired, releaseFile+"shipped_as = \"widget-1.0.0\"\nunrecoverable = true\n")
	dirs, err := releaserecord.ArchiveDirs(root)
	mustNotFail(t, err)
	if strings.Join(dirs, ",") != ".strictmetadata/releases/gadget,.strictmetadata/retired-release-histories/widget/releases" {
		t.Fatalf("archive directories %v", dirs)
	}
	record, err := lifecycle.Parse([]byte(lifecycleRecord))
	mustNotFail(t, err)
	x, err := releaserecord.BuildExplanations(root, map[string]semver.Version{"gadget@v0.4.0": version(t, "0.4.0")}, dirs, record)
	mustNotFail(t, err)
	return x
}

func TestEachSourceExplainsItsTags(t *testing.T) {
	hygiene.Isolate(t)
	x := explanations(t)
	cases := []struct {
		tag     string
		created string
		source  releaserecord.ExplanationSource
		want    string
	}{
		{"gadget@v0.4.0", "2026-05-01", releaserecord.SourceArchivedVersion, "the archived version 0.4.0"},
		{"gizmo@v0.3.0", "2025-05-01", releaserecord.SourceShippedAs, `shipped_as = "gizmo@v0.3.0"`},
		{"widget-1.0.0", "2024-05-01", releaserecord.SourceShippedAs, "1.0.0"},
		{"nightly", "2026-05-01", releaserecord.SourceUnversionedTag, "a moving tag CI publishes"},
		{"gizmo@v0.1.0", "2025-03-01", releaserecord.SourceRetiredIdentity, `"gizmo" of "gadget" owned it (until 2026-01-01)`},
	}
	for _, c := range cases {
		found, ok, err := x.Explain(c.tag, day(t, c.created))
		mustNotFail(t, err)
		if !ok || found.Source != c.source {
			t.Errorf("%s: %+v %v", c.tag, found, ok)
			continue
		}
		requireContains(t, found.Describe(), c.want)
	}
	if got := strings.Join(x.UnversionedTags(), ","); got != "nightly" {
		t.Errorf("unversioned tags %s", got)
	}
}

func TestAnOpenIdentityExplainsNothing(t *testing.T) {
	hygiene.Isolate(t)
	x := explanations(t)
	for _, c := range []struct{ tag, created string }{
		// The current scheme's tag nothing archived: a live release's tag the
		// backfill adopts, not one an identity accounts for.
		{"gadget@v0.9.0", "2026-05-01"},
		// A tag of the old name created after the old name ended.
		{"gizmo@v0.9.0", "2026-05-01"},
		{"random", "2026-05-01"},
	} {
		if found, ok, err := x.Explain(c.tag, day(t, c.created)); err != nil || ok {
			t.Errorf("%s was explained: %+v %v", c.tag, found, err)
		}
	}
}

func TestTheCurrentSchemeTakesPrecedence(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeArchiveWith(t, root, "0.3.0", "recorded", commitA, "shipped_as = \"gadget@v0.4.0\"\n")
	record, err := lifecycle.Parse([]byte(lifecycleRecord + "\n[[unversioned_tags]]\ntag = \"gadget@v0.4.0\"\nreason = \"x\"\nrecorded = 2026-02-01\n"))
	mustNotFail(t, err)
	dirs, err := releaserecord.ArchiveDirs(root)
	mustNotFail(t, err)
	x, err := releaserecord.BuildExplanations(root, map[string]semver.Version{"gadget@v0.4.0": version(t, "0.4.0")}, dirs, record)
	mustNotFail(t, err)
	found, ok, err := x.Explain("gadget@v0.4.0", day(t, "2026-05-01"))
	mustNotFail(t, err)
	if !ok || found.Source != releaserecord.SourceArchivedVersion {
		t.Fatalf("explained by %+v", found)
	}
}

func TestAnArchiveRlsblCannotReadIsAnError(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, releaserecord.ArchivePath(releaserecord.ArchiveDir(releasable), version(t, "0.1.0"))), "not = [toml")
	record, err := lifecycle.Parse([]byte("format_version = 1\n"))
	mustNotFail(t, err)
	if _, err := releaserecord.BuildExplanations(root, nil, []string{releaserecord.ArchiveDir(releasable)}, record); err == nil {
		t.Fatal("an unreadable archive was read as explaining nothing")
	}
}
