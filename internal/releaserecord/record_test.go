package releaserecord_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// history is a repository with three commits, one per release, the last
// one HEAD.
type history struct {
	repo    *testsupport.Repo
	commits []string
}

func newHistory(t *testing.T, n int) history {
	t.Helper()
	repo := testsupport.NewRepo(t)
	h := history{repo: repo}
	for i := 0; i < n; i++ {
		h.commits = append(h.commits, repo.CommitFile("file.txt", strings.Repeat("x", i+1)+"\n", "commit"))
	}
	return h
}

func TestArchivedVersionsAreListedHighestFirst(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	got, err := releaserecord.ArchivedVersions(root, dir)
	mustNotFail(t, err)
	if len(got) != 0 {
		t.Fatalf("a missing directory listed %v", got)
	}
	for _, v := range []string{"0.2.0", "0.10.0", "0.9.1"} {
		writeArchive(t, root, v, "never-released", "")
	}
	for _, name := range []string{"unreleased.toml", "version", "undo-audits.jsonl", "notes.md"} {
		testsupport.WriteFile(t, filepath.Join(root, dir, name), "")
	}
	got, err = releaserecord.ArchivedVersions(root, dir)
	mustNotFail(t, err)
	var names []string
	for _, v := range got {
		names = append(names, v.String())
	}
	if strings.Join(names, ",") != "0.10.0,0.9.1,0.2.0" {
		t.Fatalf("listed %v", names)
	}
}

func TestAnArchiveNameRlsblCannotReadIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	testsupport.WriteFile(t, filepath.Join(root, dir, "v1.0.0-rc.1.toml"), "")
	_, err := releaserecord.ArchivedVersions(root, dir)
	if err == nil {
		t.Fatal("a pre-release archive was passed over")
	}
	requireContains(t, err.Error(), "v1.0.0-rc.1.toml", "rename or remove")
	mustNotFail(t, os.Remove(filepath.Join(root, dir, "v1.0.0-rc.1.toml")))
	_, err = releaserecord.ArchivedVersions(root, dir)
	mustNotFail(t, err)
}

func TestAnEntryWhoseTagAgreesReads(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	writeArchive(t, h.repo.Dir, "0.1.0", "recorded", h.commits[0])
	h.repo.Git("tag", "v0.1.0", h.commits[0])
	mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		e, err := r.Entry(version(t, "0.1.0"))
		if err == nil && (e.Fate != releaserecord.FateRecorded || e.ReleaseCommit != h.commits[0]) {
			t.Errorf("entry %+v", e)
		}
		return err
	}))
}

func TestAnAbsentTagIsNoDisagreement(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	writeArchive(t, h.repo.Dir, "0.1.0", "recorded", h.commits[0])
	mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		_, err := r.Entry(version(t, "0.1.0"))
		return err
	}))
}

func TestATagPointingElsewhereIsTheDisagreementError(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 2)
	writeArchive(t, h.repo.Dir, "0.1.0", "recorded", h.commits[0])
	h.repo.Git("tag", "v0.1.0", h.commits[1])
	read := func() error {
		return reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
			_, err := r.Entry(version(t, "0.1.0"))
			return err
		})
	}
	msg := readError(t, read(), releaserecord.ReasonDisagreement)
	requireContains(t, msg, h.commits[0], h.commits[1], "git tag -f v0.1.0 "+h.commits[0], "rlsbl release reconcile")
	// Re-pointing the tag, as the error says, clears it.
	h.repo.Git("tag", "-f", "v0.1.0", h.commits[0])
	mustNotFail(t, read())
}

func TestTheDisagreementUsesTheReleasablesScheme(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 2)
	writeArchive(t, h.repo.Dir, "0.1.0", "recorded", h.commits[0])
	h.repo.Git("tag", "v0.1.0", h.commits[1])
	h.repo.Git("tag", "gadget@v0.1.0", h.commits[1])
	err := readingScheme(t, h.repo.Dir, "gadget@v{version}", func(r *releaserecord.Record) error {
		_, err := r.Entry(version(t, "0.1.0"))
		return err
	})
	requireContains(t, readError(t, err, releaserecord.ReasonDisagreement), `"gadget@v0.1.0"`)
}

func TestAnUnrecoverableEntryReadsAsSuch(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	writeArchive(t, h.repo.Dir, "0.1.0", "unrecoverable", "")
	mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		e, err := r.Entry(version(t, "0.1.0"))
		if err == nil && (e.Fate != releaserecord.FateUnrecoverable || e.ReleaseCommit != "") {
			t.Errorf("entry %+v", e)
		}
		return err
	}))
}

func TestAnArchiveStatingNoFateIsTheNoFateError(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	writeArchive(t, h.repo.Dir, "0.1.0", "unstated", "")
	read := func() error {
		return reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
			_, err := r.Entry(version(t, "0.1.0"))
			return err
		})
	}
	msg := readError(t, read(), releaserecord.ReasonNoFate)
	requireContains(t, msg, "records no fate", `"v0.1.0" does not exist locally`, "rlsbl release backfill --dry-run", "never_released = true")
	h.repo.Git("tag", "v0.1.0", h.commits[0])
	msg = readError(t, read(), releaserecord.ReasonNoFate)
	requireContains(t, msg, "points at "+h.commits[0])
	// Declaring the fate, as the error offers, clears it.
	writeArchive(t, h.repo.Dir, "0.1.0", "never-released", "")
	mustNotFail(t, read())
}

func TestNearestIsTheHighestReleaseTheCheckoutContains(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 3)
	writeArchive(t, h.repo.Dir, "0.1.0", "recorded", h.commits[0])
	writeArchive(t, h.repo.Dir, "0.2.0", "unrecoverable", "")
	writeArchive(t, h.repo.Dir, "0.3.0", "recorded", h.commits[1])
	writeArchive(t, h.repo.Dir, "0.4.0", "never-released", "")
	// A release on a branch HEAD does not contain.
	h.repo.Git("checkout", "-q", "-b", "side", h.commits[0])
	side := h.repo.CommitFile("side.txt", "side\n", "side")
	h.repo.Git("checkout", "-q", "main")
	writeArchive(t, h.repo.Dir, "0.5.0", "recorded", side)
	mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		e, err := r.Nearest("HEAD")
		if err != nil {
			return err
		}
		if e == nil || e.Version.String() != "0.3.0" {
			t.Errorf("nearest %+v", e)
		}
		at, err := r.Nearest(h.commits[0])
		if err != nil {
			return err
		}
		if at == nil || at.Version.String() != "0.1.0" {
			t.Errorf("nearest at the first commit %+v", at)
		}
		return nil
	}))
}

func TestNoArchiveAndNoTagIsNoRelease(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	h.repo.Git("tag", "latest", h.commits[0])
	h.repo.Git("tag", "widget@v0.1.0", h.commits[0])
	mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		e, err := r.Nearest("HEAD")
		if e != nil {
			t.Errorf("nearest %+v", e)
		}
		return err
	}))
}

func TestAnEmptyRecordUnderTheSchemesTagsIsTheUnbackfilledError(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	for _, tag := range []string{"v0.1.0", "v0.2.0", "v0.3.0", "v0.10.0", "vNext"} {
		h.repo.Git("tag", tag, h.commits[0])
	}
	read := func(fn func(r *releaserecord.Record) error) error { return reading(t, h.repo.Dir, fn) }
	nearest := func(r *releaserecord.Record) error { return errorOf(r.Nearest("HEAD")) }
	for _, fn := range []func(r *releaserecord.Record) error{
		nearest,
		func(r *releaserecord.Record) error { return errorOf(r.Latest("HEAD")) },
		func(r *releaserecord.Record) error { return errorOf(r.AtCommit(h.commits[0])) },
		func(r *releaserecord.Record) error { return errorOf(r.RequireCheckoutContainsLatest("HEAD")) },
	} {
		readError(t, read(fn), releaserecord.ReasonUnbackfilled)
	}
	msg := readError(t, read(nearest), releaserecord.ReasonUnbackfilled)
	requireContains(t, msg, `"v{version}"`, "v0.10.0, v0.3.0, v0.2.0 (and others)", "rlsbl release backfill --dry-run")
	if strings.Contains(msg, "vNext") || strings.Contains(msg, "adopt-tags") {
		t.Errorf("the error names what it should not:\n%s", msg)
	}
	// One archive is enough to silence it.
	writeArchive(t, h.repo.Dir, "0.10.0", "recorded", h.commits[0])
	mustNotFail(t, read(nearest))
}

func TestAnotherSchemesTagsDoNotTripTheUnbackfilledError(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	h.repo.Git("tag", "widget@v0.1.0", h.commits[0])
	h.repo.Git("tag", "kernel/vulkan/v0.1.0", h.commits[0])
	mustNotFail(t, readingScheme(t, h.repo.Dir, "kernel/v{version}", func(r *releaserecord.Record) error {
		_, err := r.Nearest("HEAD")
		return err
	}))
	h.repo.Git("tag", "kernel/v0.1.0", h.commits[0])
	err := readingScheme(t, h.repo.Dir, "kernel/v{version}", func(r *releaserecord.Record) error {
		_, err := r.Nearest("HEAD")
		return err
	})
	readError(t, err, releaserecord.ReasonUnbackfilled)
}

func TestAForkNamesAdoptTagsFirst(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	h.repo.Git("tag", "v0.1.0", h.commits[0])
	err := readingFork(t, h.repo.Dir, "v{version}", "https://github.com/upstream/gadget", func(r *releaserecord.Record) error {
		return errorOf(r.Nearest("HEAD"))
	})
	requireContains(t, readError(t, err, releaserecord.ReasonUnbackfilled), "fork of https://github.com/upstream/gadget", "rlsbl upstream adopt-tags --dry-run")
}

func TestAMissingReleaseCommitIsIndeterminable(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	missing := strings.Repeat("d", 40)
	writeArchive(t, h.repo.Dir, "0.1.0", "recorded", missing)
	err := reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		_, err := r.Nearest("HEAD")
		return err
	})
	requireContains(t, readError(t, err, releaserecord.ReasonIndeterminable), missing, "git fetch --unshallow", "git fetch origin "+missing)
}

func TestAShallowHistoryIsIndeterminableNotAbsent(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 3)
	shallow := filepath.Join(t.TempDir(), "shallow")
	if _, stderr, code := testsupport.RunGit(t, filepath.Dir(shallow), "clone", "-q", "--depth", "1", "file://"+h.repo.Dir, shallow); code != 0 {
		t.Fatal(stderr)
	}
	writeArchive(t, shallow, "0.1.0", "recorded", h.commits[0])
	err := reading(t, shallow, func(r *releaserecord.Record) error {
		_, err := r.Nearest("HEAD")
		return err
	})
	readError(t, err, releaserecord.ReasonIndeterminable)
}

func TestAtCommitNamesTheReleaseACommitIs(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 3)
	writeArchive(t, h.repo.Dir, "0.1.0", "recorded", h.commits[0])
	writeArchive(t, h.repo.Dir, "0.2.0", "recorded", h.commits[2])
	mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
		for i, want := range []string{"0.1.0", "", "0.2.0"} {
			e, err := r.AtCommit(h.commits[i])
			if err != nil {
				return err
			}
			got := ""
			if e != nil {
				got = e.Version.String()
			}
			if got != want {
				t.Errorf("commit %d names %q, want %q", i, got, want)
			}
		}
		return nil
	}))
}

func TestTheLatestReleaseFact(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 2)
	latest := func() releaserecord.LatestFact {
		var fact releaserecord.LatestFact
		mustNotFail(t, reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
			var err error
			fact, err = r.Latest("HEAD")
			return err
		}))
		return fact
	}
	if f := latest(); f.Released || f.Label() != "(none)" || f.State() != "" {
		t.Fatalf("no release: %+v %s", f, f.Label())
	}
	writeArchive(t, h.repo.Dir, "0.1.0", "recorded", h.commits[0])
	if f := latest(); f.Label() != "0.1.0" || !f.InCheckout || f.State() != "recorded" {
		t.Fatalf("contained: %+v %s", f, f.Label())
	}
	h.repo.Git("checkout", "-q", "-b", "side", h.commits[0])
	side := h.repo.CommitFile("side.txt", "side\n", "side")
	h.repo.Git("checkout", "-q", "main")
	writeArchive(t, h.repo.Dir, "0.2.0", "recorded", side)
	if f := latest(); f.Label() != "0.2.0 (not in this checkout's history)" || f.InCheckout {
		t.Fatalf("outside: %+v %s", f, f.Label())
	}
	writeArchive(t, h.repo.Dir, "0.3.0", "unrecoverable", "")
	writeArchive(t, h.repo.Dir, "0.4.0", "never-released", "")
	if f := latest(); f.Label() != "0.3.0 (commit not recoverable; 0.4.0 archived but never released)" || f.State() != "unrecoverable" {
		t.Fatalf("unrecoverable: %+v %s", f, f.Label())
	}
}

func TestPreparingAReleaseRequiresTheLatestRelease(t *testing.T) {
	hygiene.Isolate(t)
	h := newHistory(t, 1)
	h.repo.Git("checkout", "-q", "-b", "side")
	side := h.repo.CommitFile("side.txt", "side\n", "side")
	h.repo.Git("checkout", "-q", "main")
	writeArchive(t, h.repo.Dir, "0.1.0", "recorded", side)
	require := func() error {
		return reading(t, h.repo.Dir, func(r *releaserecord.Record) error {
			_, err := r.RequireCheckoutContainsLatest("HEAD")
			return err
		})
	}
	requireContains(t, readError(t, require(), releaserecord.ReasonLatestNotInCheckout), "does not contain the latest release, 0.1.0", side, "git pull")
	// Bringing the checkout up to date, as the error says, clears it.
	h.repo.Git("merge", "-q", "--ff-only", "side")
	mustNotFail(t, require())
}

func TestLatestReleasedVersionReadsOnlyTheArchives(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	_, ok, err := releaserecord.LatestReleasedVersion(root, dir)
	mustNotFail(t, err)
	if ok {
		t.Fatal("an empty record has a latest release")
	}
	writeArchive(t, root, "0.1.0", "unrecoverable", "")
	writeArchive(t, root, "0.2.0", "never-released", "")
	v, ok, err := releaserecord.LatestReleasedVersion(root, dir)
	mustNotFail(t, err)
	if !ok || semver.Compare(v, version(t, "0.1.0")) != 0 {
		t.Fatalf("latest %v %v", v, ok)
	}
	writeArchive(t, root, "0.3.0", "unstated", "")
	if _, _, err := releaserecord.LatestReleasedVersion(root, dir); err == nil || !strings.Contains(err.Error(), "records no fate") {
		t.Fatalf("an unstated archive: %v", err)
	}
}

func TestEntryTagPrefersShippedAs(t *testing.T) {
	hygiene.Isolate(t)
	s := scheme(t, "gadget@v{version}")
	e := releaserecord.Entry{Version: version(t, "1.2.3")}
	if e.Tag(s) != "gadget@v1.2.3" {
		t.Fatalf("tag %s", e.Tag(s))
	}
	e.ShippedAs = "v1.2.3"
	if e.Tag(s) != "v1.2.3" {
		t.Fatalf("tag %s", e.Tag(s))
	}
}
