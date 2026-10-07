package releaserecord_test

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// decide asks the version decision the way the release does: the latest
// release judged first, then the decision from it.
func decide(t *testing.T, dir, current string, bump semver.Bump) (releaserecord.Decision, error) {
	t.Helper()
	var d releaserecord.Decision
	err := reading(t, dir, func(r *releaserecord.Record) error {
		latest, err := r.Latest("HEAD")
		if err != nil {
			return err
		}
		d, err = r.DecideVersion(version(t, current), bump, latest)
		return err
	})
	return d, err
}

// released is a repository whose 0.29.3 is recorded and tagged.
func released(t *testing.T) history {
	t.Helper()
	h := newHistory(t, 2)
	writeArchive(t, h.repo.Dir, "0.29.3", "recorded", h.commits[0])
	h.repo.Git("tag", "v0.29.3", h.commits[0])
	return h
}

func TestAnEmptyRecordIsAFirstRelease(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	d, err := decide(t, h.repo.Dir, "0.1.0", semver.Minor)
	mustNotFail(t, err)
	if !d.FirstRelease || d.Version.String() != "0.1.0" || d.Bump != "" || d.Tag != "v0.1.0" {
		t.Fatalf("decision %+v", d)
	}
}

func TestARecordOfOnlyNeverReleasedNumbersIsAFirstRelease(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	writeArchive(t, h.repo.Dir, "0.0.9", "never-released", "")
	d, err := decide(t, h.repo.Dir, "0.1.0", semver.Patch)
	mustNotFail(t, err)
	if !d.FirstRelease || d.Version.String() != "0.1.0" {
		t.Fatalf("decision %+v", d)
	}
}

func TestAReleasedCurrentVersionIsBumpedFrom(t *testing.T) {
	hygiene.Isolate(t)
	h := released(t)
	d, err := decide(t, h.repo.Dir, "0.29.3", semver.Minor)
	mustNotFail(t, err)
	if d.FirstRelease || d.Version.String() != "0.30.0" || d.Bump != semver.Minor || d.Tag != "v0.30.0" {
		t.Fatalf("decision %+v", d)
	}
}

func TestTheAbandonedAttemptStateIsRefusedNamingAbandon(t *testing.T) {
	hygiene.Isolate(t)
	h := released(t)
	_, err := decide(t, h.repo.Dir, "0.29.4", semver.Patch)
	if err == nil {
		t.Fatal("the version files' unrecorded number was released as it is")
	}
	requireContains(t, err.Error(), "the version files say 0.29.4", "latest release is 0.29.3", "rlsbl release abandon --approve-consequential")
	// Recording 0.29.4 never released, as abandon does, clears it: the
	// release bumps from it.
	writeArchive(t, h.repo.Dir, "0.29.4", "never-released", "")
	d, err := decide(t, h.repo.Dir, "0.29.4", semver.Minor)
	mustNotFail(t, err)
	if d.Version.String() != "0.30.0" {
		t.Fatalf("decision %+v", d)
	}
}

func TestReusingANeverReleasedNumberIsRefusedNamingTheWayPast(t *testing.T) {
	hygiene.Isolate(t)
	h := released(t)
	writeArchive(t, h.repo.Dir, "0.29.4", "never-released", "")
	writeArchive(t, h.repo.Dir, "0.29.5", "never-released", "")
	_, err := decide(t, h.repo.Dir, "0.29.3", semver.Patch)
	if err == nil {
		t.Fatal("released under a never-released number")
	}
	requireContains(t, err.Error(), "gives 0.29.4", "set the version files to 0.29.5", "gives 0.29.6")
	// Following it clears it.
	d, err := decide(t, h.repo.Dir, "0.29.5", semver.Patch)
	mustNotFail(t, err)
	if d.Version.String() != "0.29.6" {
		t.Fatalf("decision %+v", d)
	}
	// A bump that does not reach one is unaffected.
	d, err = decide(t, h.repo.Dir, "0.29.3", semver.Minor)
	mustNotFail(t, err)
	if d.Version.String() != "0.30.0" {
		t.Fatalf("decision %+v", d)
	}
}

func TestVersionFilesBehindTheLatestReleaseAreRefused(t *testing.T) {
	hygiene.Isolate(t)
	h := released(t)
	_, err := decide(t, h.repo.Dir, "0.29.1", semver.Patch)
	if err == nil {
		t.Fatal("released below the latest release")
	}
	requireContains(t, err.Error(), "0.29.1, which is behind the latest release, 0.29.3", "Set the version files to 0.29.3")
	if strings.Contains(err.Error(), "abandon") {
		t.Fatalf("a version behind the latest release was pointed at abandon: %v", err)
	}
	d, err := decide(t, h.repo.Dir, "0.29.3", semver.Patch)
	mustNotFail(t, err)
	if d.Version.String() != "0.29.4" {
		t.Fatalf("decision %+v", d)
	}
}

func TestAnUnrecoverableCurrentVersionWithoutItsTagIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 2)
	writeArchive(t, h.repo.Dir, "0.27.0", "unrecoverable", "")
	_, err := decide(t, h.repo.Dir, "0.27.0", semver.Minor)
	if err == nil {
		t.Fatal("an untagged unrecoverable version was read as a first release")
	}
	requireContains(t, err.Error(), "version 0.27.0 was released before", "marked unrecoverable", "git tag v0.27.0 <release-commit>")
	// Restoring the tag clears it.
	h.repo.Git("tag", "v0.27.0", h.commits[0])
	d, err := decide(t, h.repo.Dir, "0.27.0", semver.Minor)
	mustNotFail(t, err)
	if d.Version.String() != "0.28.0" {
		t.Fatalf("decision %+v", d)
	}
}

func TestADestroyedTagNamesItsReleaseCommit(t *testing.T) {
	hygiene.Isolate(t)
	h := released(t)
	h.repo.Git("tag", "-d", "v0.29.3")
	_, err := decide(t, h.repo.Dir, "0.29.3", semver.Patch)
	if err == nil {
		t.Fatal("a destroyed tag was not noticed")
	}
	requireContains(t, err.Error(), "git tag v0.29.3 "+h.commits[0], "tag_format in .strictmetadata/releasables/releasables.toml")
	h.repo.Git("tag", "v0.29.3", h.commits[0])
	_, err = decide(t, h.repo.Dir, "0.29.3", semver.Patch)
	mustNotFail(t, err)
}

func TestAFinalizedChangelogContradictsAFirstRelease(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	finalized := filepath.Join(h.repo.Dir, ".strictmetadata/changelog/gadget/0.1.0.jsonl")
	testsupport.WriteFile(t, finalized, "")
	_, err := decide(t, h.repo.Dir, "0.1.0", semver.Patch)
	if err == nil {
		t.Fatal("a version with a finalized changelog was released as a first release")
	}
	requireContains(t, err.Error(), "its finalized changelog .strictmetadata/changelog/gadget/0.1.0.jsonl exists", `no tag "v0.1.0"`)
}

func TestTheRenameAsksTheBumpedVersionOfAnUnrecoverableVersion(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	writeArchive(t, h.repo.Dir, "0.27.0", "unrecoverable", "")
	mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		d, err := r.BumpedVersion(version(t, "0.27.0"), semver.Minor)
		if err == nil && d.Version.String() != "0.28.0" {
			t.Errorf("decision %+v", d)
		}
		return err
	}))
}

func TestAnUndeclaredBumpIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	h := released(t)
	if _, err := decide(t, h.repo.Dir, "0.29.3", ""); err == nil {
		t.Fatal("an empty bump was given a default")
	}
}

