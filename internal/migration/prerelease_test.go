package migration

import (
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/semver"
)

// entryLine is a first-format changelog line with id, naming commit.
func entryLine(id, commit, description string) string {
	return `{"format_version":1,"id":"` + id + `","commits":["` + commit + `"],"user_facing":true,"description":"` + description + `","type":"feature"}` + "\n"
}

// Ids of the pre-release fixtures' changelog lines.
const (
	rcID        = "18dc47e50f0bbc65d295f29408ec48a78b9f83f1c6eb8385"
	duplicateID = "18dc47e50f0bbc65d295f29408ec48a78b9f83f1c6eb8386"
	betaID      = "18dc47e50f0bbc65d295f29408ec48a78b9f83f1c6eb8387"
)

// descriptionsOf are the descriptions of a changelog file's entries, in
// order.
func descriptionsOf(f *changelog.File) []string {
	var out []string
	for _, e := range f.Entries() {
		out = append(out, e.Description)
	}
	return out
}

// recordedUnversionedTag is the record's unversioned tag named tag, and false when
// it holds none.
func recordedUnversionedTag(t *testing.T, f *fixture, tag string) (lifecycle.UnversionedTag, bool) {
	t.Helper()
	rec, err := lifecycle.Load(f.repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, u := range rec.UnversionedTags() {
		if u.Tag == tag {
			return u, true
		}
	}
	return lifecycle.UnversionedTag{}, false
}

// A pre-release of a stable version that was released folds into the stable
// version's changelog file, an entry naming the commits an entry of that
// file already names left out as the Python's changelog left it out; the
// pre-release's archive is not carried, and its tag is recorded as an
// unversioned tag.
func TestAPreReleaseOfAReleasedVersionFoldsIntoItsFileAndItsTagIsRecorded(t *testing.T) {
	hygiene.Isolate(t)
	f := standalone(t)
	released := f.read(".rlsbl/releases/v0.1.0.toml")
	rcCommit := f.repo.Git("rev-parse", "HEAD~1")
	f.repo.Git("tag", "v0.1.0-rc.1", rcCommit)
	stableCommit := strings.TrimSpace(strings.SplitN(strings.SplitN(released, `candidate_sha = "`, 2)[1], `"`, 2)[0])
	f.write(".rlsbl/releases/v0.1.0-rc.1.toml", strings.Replace(oldArchive(rcCommit, f.repo.Git("rev-parse", rcCommit+"^{tree}"), "."), `bump = "minor"`, `bump = "prerelease"`, 1))
	f.write(".rlsbl/changes/0.1.0-rc.1.jsonl", entryLine(rcID, rcCommit, "A release-candidate feature")+entryLine(duplicateID, stableCommit, "The first feature, as the candidate described it"))
	f.write(".rlsbl/changes/0.1.0-rc.1.md", "## 0.1.0-rc.1\n")
	f.commit("a pre-release of 0.1.0")

	f.migrate()

	file, err := changelog.ReadVersion(f.repo.Dir, changelog.Dir("portal"), semver.Version{Minor: 1})
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(descriptionsOf(file), "|"); got != "The first feature|A release-candidate feature" {
		t.Fatalf("0.1.0's changelog file holds: %s", got)
	}
	for _, gone := range []string{".strictmetadata/changelog/portal/0.1.0-rc.1.jsonl", ".strictmetadata/releases/portal/v0.1.0-rc.1.toml"} {
		if f.exists(gone) {
			t.Errorf("the pre-release's %s was carried", gone)
		}
	}
	tag, ok := recordedUnversionedTag(t, f, "v0.1.0-rc.1")
	if !ok {
		t.Fatal("the pre-release tag is not an unversioned tag of the record")
	}
	contains(t, tag.Reason, "pre-release tag")
	contains(t, tag.Reason, "pre-release channel")
	contains(t, f.read("CHANGELOG.md"), "A release-candidate feature")
}

// A pre-release of a stable version never released folds into the
// unreleased file, and its tag is recorded as an unversioned tag.
func TestAPreReleaseOfAnUnreleasedVersionFoldsIntoTheUnreleasedFile(t *testing.T) {
	hygiene.Isolate(t)
	f := standalone(t)
	betaCommit := f.repo.Head()
	f.repo.Git("tag", "v0.2.0-beta.1", betaCommit)
	f.write(".rlsbl/releases/v0.2.0-beta.1.toml", strings.Replace(oldArchive(betaCommit, f.repo.Git("rev-parse", "HEAD^{tree}"), "."), `bump = "minor"`, `bump = "prerelease"`, 1))
	f.write(".rlsbl/changes/0.2.0-beta.1.jsonl", entryLine(betaID, betaCommit, "A beta feature"))
	f.commit("a pre-release of 0.2.0")

	f.migrate()

	unreleased, err := changelog.ReadUnreleased(f.repo.Dir, changelog.Dir("portal"))
	if err != nil {
		t.Fatal(err)
	}
	var ids []string
	for _, e := range unreleased.Entries() {
		ids = append(ids, e.ID)
	}
	if len(ids) != 2 || ids[1] != betaID {
		t.Fatalf("the unreleased file holds: %+v", unreleased.Entries())
	}
	if f.exists(".strictmetadata/changelog/portal/0.2.0.jsonl") || f.exists(".strictmetadata/changelog/portal/0.2.0-beta.1.jsonl") {
		t.Error("a released file was written for a version never released")
	}
	if _, ok := recordedUnversionedTag(t, f, "v0.2.0-beta.1"); !ok {
		t.Fatal("the pre-release tag is not an unversioned tag of the record")
	}
}
