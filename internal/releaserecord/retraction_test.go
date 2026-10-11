package releaserecord_test

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
)

// retracted is a repository whose 0.5.0 was released at its first commit and
// is retracted by 0.5.1, the retraction version, at its third; its second
// commit is unreleased work. Both tags exist.
func retracted(t *testing.T) history {
	t.Helper()
	h := newHistory(t, 3)
	writeArchiveWith(t, h.repo.Dir, "0.5.0", "recorded", h.commits[0], "retracted_by = \"0.5.1\"\n")
	writeArchiveWith(t, h.repo.Dir, "0.5.1", "recorded", h.commits[2], "retracted_by = \"0.5.1\"\nretracts = [\"0.5.0\"]\n")
	h.repo.Git("tag", "v0.5.0", h.commits[0])
	h.repo.Git("tag", "v0.5.1", h.commits[2])
	return h
}

func TestTheRetractionFieldsReadBack(t *testing.T) {
	hygiene.Isolate(t)
	h := retracted(t)
	dir := releaserecord.ArchiveDir(releasable)
	a, err := releaserecord.ReadArchive(h.repo.Dir, dir, version(t, "0.5.0"))
	mustNotFail(t, err)
	if a.Fate != releaserecord.FateRecorded || a.RetractedBy != "0.5.1" || len(a.Retracts) != 0 {
		t.Fatalf("the retracted release read back %+v", a)
	}
	b, err := releaserecord.ReadArchive(h.repo.Dir, dir, version(t, "0.5.1"))
	mustNotFail(t, err)
	if b.RetractedBy != "0.5.1" || len(b.Retracts) != 1 || b.Retracts[0].String() != "0.5.0" {
		t.Fatalf("the retraction version read back %+v", b)
	}
}

func TestTheRetractionFieldsAreRefusedWhereTheyCannotHold(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	for name, c := range map[string]struct{ v, fate, extra, want string }{
		"a never-released version retracted":      {"0.1.0", "never-released", "retracted_by = \"0.1.1\"\n", "retracted_by"},
		"a retraction without a fate":             {"0.1.0", "unstated", "retracted_by = \"0.1.1\"\n", "retracted_by"},
		"retracts on another version":             {"0.1.0", "recorded", "retracted_by = \"0.1.1\"\nretracts = [\"0.0.9\"]\n", "retracts"},
		"retracts without retracted_by":           {"0.1.1", "recorded", "retracts = [\"0.1.0\"]\n", "retracts"},
		"a retraction version retracting nothing": {"0.1.1", "recorded", "retracted_by = \"0.1.1\"\n", "retracts"},
		"a retraction retracting itself":          {"0.1.1", "recorded", "retracted_by = \"0.1.1\"\nretracts = [\"0.1.1\"]\n", "retracts"},
		"a version that is no version":            {"0.1.0", "recorded", "retracted_by = \"v0.1.1\"\n", "retracted_by"},
	} {
		t.Run(name, func(t *testing.T) {
			writeArchiveWith(t, root, c.v, c.fate, commitA, c.extra)
			_, err := releaserecord.ReadArchive(root, dir, version(t, c.v))
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("want a refusal naming %s, got %v", c.want, err)
			}
		})
	}
	// The editable release file never carries them.
	if _, err := releaserecord.ParseReleaseFile("unreleased.toml", []byte(releaseFile+"retracted_by = \"0.1.1\"\n")); err == nil || !strings.Contains(err.Error(), "retracted_by") {
		t.Fatalf("a release file carrying retracted_by: %v", err)
	}
}

func TestMarkRetractedRecordsTheRetractionOnce(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	writeArchive(t, root, "0.5.0", "recorded", commitA)
	writeArchive(t, root, "0.4.0", "never-released", "")
	mark := func(v, by string) (bool, error) {
		var changed bool
		_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
			var err error
			changed, err = releaserecord.MarkRetracted(e, root, dir, version(t, v), version(t, by))
			return err
		})
		return changed, err
	}
	changed, err := mark("0.5.0", "0.5.1")
	mustNotFail(t, err)
	if !changed {
		t.Fatal("the first mark changed nothing")
	}
	text := readText(t, filepath.Join(root, ".strictmetadata/releases/gadget/v0.5.0.toml"))
	requireContains(t, text, `retracted_by = "0.5.1"`, `release_commit = "`+commitA+`"`)
	if changed, err = mark("0.5.0", "0.5.1"); err != nil || changed {
		t.Fatalf("marking again: changed %v, %v", changed, err)
	}
	if _, err := mark("0.5.0", "0.5.2"); err == nil || !strings.Contains(err.Error(), "already retracted by 0.5.1") {
		t.Fatalf("a second retraction: %v", err)
	}
	if _, err := mark("0.4.0", "0.5.1"); err == nil || !strings.Contains(err.Error(), "never released") {
		t.Fatalf("a never-released version: %v", err)
	}
}

func TestAWrittenRetractionArchiveReadsBack(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	spec := releaserecord.ArchiveSpec{
		ReleaseFile:   releaserecord.ReleaseFile{Bump: semver.Patch, Include: []string{"go"}, Exclude: []string{}, Description: "Retracts v0.5.0 and v0.5.1: published by mistake."},
		Fate:          releaserecord.FateRecorded,
		ReleaseCommit: recorded(commitB),
		RetractedBy:   version(t, "0.5.1"),
		Retracts:      []semver.Version{version(t, "0.5.0")},
	}
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		_, err := releaserecord.WriteArchive(e, root, dir, version(t, "0.5.1"), spec)
		return err
	})
	mustNotFail(t, err)
	a, err := releaserecord.ReadArchive(root, dir, version(t, "0.5.1"))
	mustNotFail(t, err)
	if a.RetractedBy != "0.5.1" || len(a.Retracts) != 1 || a.ReleaseCommit.Commit != commitB {
		t.Fatalf("read back %+v", a)
	}
}

func TestRetractedVersionsDoNotCountAsReleases(t *testing.T) {
	hygiene.Isolate(t)
	h := retracted(t)
	_, found, err := releaserecord.LatestReleasedVersion(h.repo.Dir, releaserecord.ArchiveDir(releasable))
	mustNotFail(t, err)
	if found {
		t.Fatal("a record of retracted versions has a latest release")
	}
	has, err := releaserecord.HasRelease(h.repo.Dir, releasable)
	mustNotFail(t, err)
	if has {
		t.Fatal("a record of retracted versions holds a release")
	}
	mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		fact, err := r.Latest("HEAD")
		if err != nil {
			return err
		}
		if fact.Released || fact.Label() != "(none) (0.5.1, 0.5.0 retracted)" {
			t.Errorf("latest %+v %s", fact, fact.Label())
		}
		return nil
	}))
	// A release above them is the latest release.
	writeArchive(t, h.repo.Dir, "0.6.0", "unrecoverable", "")
	v, found, err := releaserecord.LatestReleasedVersion(h.repo.Dir, releaserecord.ArchiveDir(releasable))
	mustNotFail(t, err)
	if !found || v.String() != "0.6.0" {
		t.Fatalf("latest %v %v", v, found)
	}
}

func TestTheUnreleasedRangeStartsAfterTheRetractedReleaseNotTheRetraction(t *testing.T) {
	hygiene.Isolate(t)
	h := retracted(t)
	mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		e, err := r.Nearest("HEAD")
		if err != nil {
			return err
		}
		if e == nil || e.Version.String() != "0.5.0" || e.RetractedBy != "0.5.1" {
			t.Errorf("nearest %+v", e)
		}
		return nil
	}))
}

func TestABumpLandingOnARetractedNumberIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	h := retracted(t)
	_, err := decide(t, h.repo.Dir, "0.5.0", semver.Patch)
	if err == nil {
		t.Fatal("a release landed on the retracted 0.5.1")
	}
	requireContains(t, err.Error(), "gives 0.5.1", "retracted", "set the version files to 0.5.1", "gives 0.5.2")
	// Following it clears it.
	d, err := decide(t, h.repo.Dir, "0.5.1", semver.Patch)
	mustNotFail(t, err)
	if d.Version.String() != "0.5.2" || d.FirstRelease {
		t.Fatalf("decision %+v", d)
	}
	// Version files naming a number the record does not hold: with every
	// version retracted, nothing was released, so it is a first release.
	d, err = decide(t, h.repo.Dir, "0.1.0", semver.Minor)
	mustNotFail(t, err)
	if !d.FirstRelease || d.Version.String() != "0.1.0" {
		t.Fatalf("decision %+v", d)
	}
}
