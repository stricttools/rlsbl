package releasenotes_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/releasenotes"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The releasable every fixture releases.
const releasable = "portal"

var (
	commitA = strings.Repeat("a", 40)
	commitB = strings.Repeat("b", 40)
	markerA = "<!-- rlsbl-ci-sha: " + commitA + " -->"
	markerB = "<!-- rlsbl-ci-sha: " + commitB + " -->"
)

const deprecated = "> **Deprecated:** tagged but never published to any registry. Use v0.4.1 instead."

func version(t *testing.T, s string) semver.Version {
	t.Helper()
	v, err := semver.Parse(s)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func scheme(t *testing.T) workspace.TagScheme {
	t.Helper()
	s, err := workspace.NewTagScheme("v{version}")
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// writeArchive writes the archive of v. fate is "recorded" (naming commit),
// "unrecoverable", "never-released", or "unstated"; extra adds top-level
// fields.
func writeArchive(t *testing.T, root, v, fate, commit, extra string) {
	t.Helper()
	body := "format_version = 2\nbump = \"patch\"\ninclude = [\"npm\"]\nexclude = []\ndescription = \"A fix release\"\n" + extra
	switch fate {
	case "recorded":
		body += "release_commit = \"" + commit + "\"\n\n[released_trees]\n\".\" = \"" + strings.Repeat("e", 40) + "\"\n"
	case "unrecoverable":
		body += "unrecoverable = true\n"
	case "never-released":
		body += "never_released = true\n"
	case "unstated":
	default:
		t.Fatalf("unknown fate %q", fate)
	}
	rel := releaserecord.ArchivePath(releaserecord.ArchiveDir(releasable), version(t, v))
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), body)
}

// writeReleased writes v's released changelog file with one user-facing
// fix.
func writeReleased(t *testing.T, root, v, description string) {
	t.Helper()
	e := changelog.Entry{ID: strings.Repeat("0", 47) + "1", Commits: []string{commitA[:12]}, UserFacing: true, Type: changelog.TypeFix, Description: description}
	path := filepath.Join(root, filepath.FromSlash(changelog.Dir(releasable)), v+".jsonl")
	testsupport.WriteFile(t, path, changelog.Serialize(e)+"\n")
	if err := os.Chmod(path, 0o444); err != nil {
		t.Fatal(err)
	}
}

func mustBody(t *testing.T, d releasenotes.Document) string {
	t.Helper()
	body, err := d.Body()
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func TestTheBodyIsTheNotesThenTheMarker(t *testing.T) {
	hygiene.Isolate(t)
	d := releasenotes.Document{Tag: "v0.4.0", Version: version(t, "0.4.0"), Notes: "### Fixes\n\n- Fixed bug Y\n", ReleaseCommit: commitA}
	if got := mustBody(t, d); got != "### Fixes\n\n- Fixed bug Y\n\n"+markerA+"\n" {
		t.Fatalf("body %q", got)
	}
	if d.Title() != "v0.4.0" || d.Prerelease() {
		t.Fatalf("title %q, pre-release %v", d.Title(), d.Prerelease())
	}
}

func TestEmptyNotesStillNameTheVersion(t *testing.T) {
	hygiene.Isolate(t)
	d := releasenotes.Document{Tag: "v0.4.0", Version: version(t, "0.4.0"), ReleaseCommit: commitA}
	if got := mustBody(t, d); got != "Release 0.4.0\n\n"+markerA+"\n" {
		t.Fatalf("body %q", got)
	}
}

func TestADocumentNamingNoReleaseCommitCarriesNoMarker(t *testing.T) {
	hygiene.Isolate(t)
	d := releasenotes.Document{Tag: "v0.4.0", Version: version(t, "0.4.0"), Notes: "- Fixed bug Y", Notices: []string{deprecated}}
	if got := mustBody(t, d); got != deprecated+"\n\n- Fixed bug Y\n" {
		t.Fatalf("body %q", got)
	}
	if _, err := d.Marker(); err == nil {
		t.Fatal("a document naming no release commit produced a marker")
	}
}

func TestAnAbbreviatedReleaseCommitIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	d := releasenotes.Document{Tag: "v0.4.0", Version: version(t, "0.4.0"), ReleaseCommit: commitA[:12]}
	if _, err := d.Body(); err == nil || !strings.Contains(err.Error(), "40-digit") {
		t.Fatalf("an abbreviated release commit: %v", err)
	}
}

// Deprecate puts its notice and a blank line on top of the body GitHub
// holds; composing from the record afterwards reproduces that document, so
// a later rewrite keeps the notice where deprecate put it.
func TestTheComposedBodyIsWhatANoticeOnTopOfTheBodyMakes(t *testing.T) {
	hygiene.Isolate(t)
	plain := releasenotes.Document{Tag: "v0.4.0", Version: version(t, "0.4.0"), Notes: "- Fixed bug Y", ReleaseCommit: commitA}
	before := mustBody(t, plain)
	yanked := "> **Yanked:** broken."
	withOne := plain
	withOne.Notices = []string{deprecated}
	if got := mustBody(t, withOne); got != releasenotes.ComposeBody([]string{deprecated}, before) || got != deprecated+"\n\n"+before {
		t.Fatalf("one notice: %q", got)
	}
	withTwo := plain
	withTwo.Notices = []string{yanked, deprecated}
	if got := mustBody(t, withTwo); got != yanked+"\n\n"+deprecated+"\n\n"+before {
		t.Fatalf("two notices: %q", got)
	}
	if !withTwo.Prerelease() {
		t.Error("a version carrying a notice is not marked pre-release")
	}
}

func TestWithMarker(t *testing.T) {
	hygiene.Isolate(t)
	if _, changed := releasenotes.WithMarker("notes\n\n"+markerA+"\n", markerA); changed {
		t.Error("a body already carrying the marker was rewritten")
	}
	got, changed := releasenotes.WithMarker("notes\n\n"+markerB+"\n", markerA)
	if !changed || got != "notes\n\n"+markerA+"\n" || strings.Contains(got, commitB) {
		t.Errorf("a stale marker: %q", got)
	}
	if got, _ := releasenotes.WithMarker("notes\n", markerA); got != "notes\n\n"+markerA+"\n" {
		t.Errorf("a body without a marker: %q", got)
	}
}

func TestKeepingMarkerOfKeepsOnlyWhatTheRecordCannotSay(t *testing.T) {
	hygiene.Isolate(t)
	unrecoverable := releasenotes.Document{Tag: "v0.4.0", Version: version(t, "0.4.0")}
	if got := unrecoverable.KeepingMarkerOf("x\n\n" + markerB + "\n"); got.ReleaseCommit != commitB {
		t.Errorf("an unrecoverable version lost the marker GitHub holds: %+v", got)
	}
	if got := unrecoverable.KeepingMarkerOf("x\n"); got.ReleaseCommit != "" {
		t.Errorf("a marker was invented: %+v", got)
	}
	recorded := releasenotes.Document{Tag: "v0.4.0", Version: version(t, "0.4.0"), ReleaseCommit: commitA}
	if got := recorded.KeepingMarkerOf("x\n\n" + markerB + "\n"); got.ReleaseCommit != commitA {
		t.Errorf("a body outranked the record: %+v", got)
	}
}

func TestReadComposesFromTheChangelogAndTheArchive(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeReleased(t, root, "0.4.0", "Fixed bug Y")
	writeArchive(t, root, "0.4.0", "recorded", commitA, "shipped_as = \"portal@v0.4.0\"\nrelease_notices = ["+quoteTOML(deprecated)+"]\n")
	d, err := releasenotes.Read(root, releasable, scheme(t), version(t, "0.4.0"))
	if err != nil {
		t.Fatal(err)
	}
	if d.Tag != "portal@v0.4.0" || d.ReleaseCommit != commitA || len(d.Notices) != 1 || d.Notices[0] != deprecated {
		t.Fatalf("document %+v", d)
	}
	if d.Notes != "A fix release\n\n### Fixes\n\n- Fixed bug Y" {
		t.Fatalf("notes %q", d.Notes)
	}
	if got := mustBody(t, d); got != deprecated+"\n\nA fix release\n\n### Fixes\n\n- Fixed bug Y\n\n"+markerA+"\n" {
		t.Fatalf("body %q", got)
	}
}

func TestReadAnswersWhereTheTagAndTheArchiveDisagree(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile("a.txt", "a\n", "the release")
	repo.CommitFile("b.txt", "b\n", "a later commit")
	repo.Git("tag", "v0.4.0")
	writeReleased(t, repo.Dir, "0.4.0", "Fixed bug Y")
	writeArchive(t, repo.Dir, "0.4.0", "recorded", first, "")
	d, err := releasenotes.Read(repo.Dir, releasable, scheme(t), version(t, "0.4.0"))
	if err != nil || d.ReleaseCommit != first {
		t.Fatalf("document %+v, error %v", d, err)
	}
}

func TestReadOfAnUnrecoverableVersionNamesNoCommit(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeReleased(t, root, "0.4.0", "Fixed bug Y")
	writeArchive(t, root, "0.4.0", "unrecoverable", "", "")
	d, err := releasenotes.Read(root, releasable, scheme(t), version(t, "0.4.0"))
	if err != nil || d.ReleaseCommit != "" || d.Tag != "v0.4.0" {
		t.Fatalf("document %+v, error %v", d, err)
	}
}

func TestReadRefusesWhatHasNoRelease(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeReleased(t, root, "0.4.0", "Fixed bug Y")
	if _, err := releasenotes.Read(root, releasable, scheme(t), version(t, "0.4.0")); err == nil || !strings.Contains(err.Error(), "rlsbl release backfill --dry-run") {
		t.Fatalf("no archive: %v", err)
	}
	writeArchive(t, root, "0.4.0", "unstated", "", "")
	if _, err := releasenotes.Read(root, releasable, scheme(t), version(t, "0.4.0")); err == nil || !strings.Contains(err.Error(), "states no fate") {
		t.Fatalf("an archive stating no fate: %v", err)
	}
	writeArchive(t, root, "0.4.0", "never-released", "", "")
	if _, err := releasenotes.Read(root, releasable, scheme(t), version(t, "0.4.0")); err == nil || !strings.Contains(err.Error(), "never_released") {
		t.Fatalf("a never-released version: %v", err)
	}
}

func TestReadRefusesAVersionWithoutAChangelogFile(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeArchive(t, root, "0.4.0", "recorded", commitA, "")
	if _, err := releasenotes.Read(root, releasable, scheme(t), version(t, "0.4.0")); err == nil || !strings.Contains(err.Error(), "0.4.0.jsonl") {
		t.Fatalf("no changelog file: %v", err)
	}
}

// quoteTOML is s as a TOML basic string.
func quoteTOML(s string) string {
	return `"` + strings.NewReplacer(`\`, `\\`, `"`, `\"`).Replace(s) + `"`
}
