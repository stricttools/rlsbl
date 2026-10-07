package releaserecord_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

var (
	commitA = strings.Repeat("a", 40)
	commitB = strings.Repeat("b", 40)
	treeC   = strings.Repeat("c", 40)
)

func recorded(commit string) releaserecord.ReleaseCommit {
	return releaserecord.ReleaseCommit{Commit: commit, Trees: map[string]string{".": treeC}}
}

func mode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Mode().Perm()
}

func TestAWrittenArchiveReadsBackReadOnly(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	v := version(t, "1.2.0")
	spec := releaserecord.ArchiveSpec{
		ReleaseFile:   releaserecord.ReleaseFile{Bump: semver.Minor, Include: []string{"go"}, Exclude: []string{}, Description: "features", Context: "why"},
		Fate:          releaserecord.FateRecorded,
		ReleaseCommit: releaserecord.ReleaseCommit{Commit: commitA, Trees: map[string]string{"cmd/gadget": treeC, "lib": commitB}},
		ShippedAs:     "gadget@v1.2.0",
	}
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		rel, err := releaserecord.WriteArchive(e, root, dir, v, spec)
		if rel != ".strictmetadata/releases/gadget/v1.2.0.toml" {
			t.Errorf("written at %s", rel)
		}
		return err
	})
	mustNotFail(t, err)
	path := filepath.Join(root, ".strictmetadata/releases/gadget/v1.2.0.toml")
	if m := mode(t, path); m != 0o444 {
		t.Errorf("the archive's mode is %o", m)
	}
	requireContains(t, readText(t, filepath.Join(root, ".strictmetadata/releases/manifest.toml")), `owner = "rlsbl"`)
	a, err := releaserecord.ReadArchive(root, dir, v)
	mustNotFail(t, err)
	if a.Fate != releaserecord.FateRecorded || a.ReleaseCommit.Commit != commitA || a.ReleaseCommit.Trees["cmd/gadget"] != treeC || a.ShippedAs != "gadget@v1.2.0" || a.Context != "why" {
		t.Fatalf("read back %+v", a)
	}
}

func TestWriteArchiveRefusals(t *testing.T) {
	hygiene.Isolate(t)
	base := releaserecord.ReleaseFile{Bump: semver.Patch, Include: []string{}, Exclude: []string{}, Description: "fixes"}
	cases := []struct {
		name string
		spec releaserecord.ArchiveSpec
		want string
	}{
		{"no fate", releaserecord.ArchiveSpec{ReleaseFile: base}, "one of the fates"},
		{"bad commit", releaserecord.ArchiveSpec{ReleaseFile: base, Fate: releaserecord.FateRecorded, ReleaseCommit: recorded("HEAD")}, "git commit id"},
		{"no trees", releaserecord.ArchiveSpec{ReleaseFile: base, Fate: releaserecord.FateRecorded, ReleaseCommit: releaserecord.ReleaseCommit{Commit: commitA}}, "at least one released tree"},
		{"commit beside a marker", releaserecord.ArchiveSpec{ReleaseFile: base, Fate: releaserecord.FateUnrecoverable, ReleaseCommit: recorded(commitA)}, "carries no release commit"},
		{"never released shipped as", releaserecord.ArchiveSpec{ReleaseFile: base, Fate: releaserecord.FateNeverReleased, ShippedAs: "v1.0.0"}, "shipped under no tag"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hygiene.Isolate(t)
			root := t.TempDir()
			_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
				_, err := releaserecord.WriteArchive(e, root, releaserecord.ArchiveDir(releasable), version(t, "1.0.0"), c.spec)
				return err
			})
			if err == nil {
				t.Fatal("accepted")
			}
			requireContains(t, err.Error(), c.want)
			if _, err := os.Stat(filepath.Join(root, ".strictmetadata")); !os.IsNotExist(err) {
				t.Fatal("a refused archive wrote something")
			}
		})
	}
}

func TestWriteArchiveRefusesAnExistingArchive(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeArchive(t, root, "1.0.0", "never-released", "")
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		_, err := releaserecord.WriteArchive(e, root, releaserecord.ArchiveDir(releasable), version(t, "1.0.0"), releaserecord.ArchiveSpec{
			ReleaseFile: releaserecord.ReleaseFile{Bump: semver.Patch, Include: []string{}, Exclude: []string{}, Description: "x"},
			Fate:        releaserecord.FateUnrecoverable,
		})
		return err
	})
	if err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("got %v", err)
	}
}

const commentedReleaseFile = `# The operator's notes stay with the release.
format_version = 2
bump = "minor"
include = ["npm"]
exclude = []
description = "the next release" # inline note
`

func TestArchivingTheReleaseFileKeepsItAndAddsTheReleaseCommit(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	release := filepath.Join(root, fileName)
	testsupport.WriteFile(t, release, commentedReleaseFile)
	v := version(t, "0.4.0")
	archive := func() error {
		_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
			return releaserecord.ArchiveReleaseFile(e, root, releasable, v, recorded(commitA))
		})
		return err
	}
	mustNotFail(t, archive())
	if _, err := os.Stat(release); !os.IsNotExist(err) {
		t.Fatal("the release file is still there")
	}
	path := filepath.Join(root, ".strictmetadata/releases/gadget/v0.4.0.toml")
	text := readText(t, path)
	requireContains(t, text, "# The operator's notes stay with the release.", "# inline note", `release_commit = "`+commitA+`"`, "[released_trees]")
	if m := mode(t, path); m != 0o444 {
		t.Errorf("the archive's mode is %o", m)
	}
	a, err := releaserecord.ReadArchive(root, releaserecord.ArchiveDir(releasable), v)
	mustNotFail(t, err)
	if a.Fate != releaserecord.FateRecorded || a.ReleaseCommit.Trees["."] != treeC {
		t.Fatalf("read back %+v", a)
	}
	// A resumed release archiving the same release again does nothing.
	mustNotFail(t, archive())
	if readText(t, path) != text {
		t.Fatal("archiving again changed the archive")
	}
}

func TestArchivingRefusesANeverReleasedArchive(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, fileName), releaseFile)
	rel := writeArchive(t, root, "0.4.0", "never-released", "")
	before := readText(t, filepath.Join(root, rel))
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		return releaserecord.ArchiveReleaseFile(e, root, releasable, version(t, "0.4.0"), recorded(commitA))
	})
	if err == nil {
		t.Fatal("archived over a never-released archive")
	}
	requireContains(t, err.Error(), "refusing to archive the release of 0.4.0", "never released")
	if readText(t, filepath.Join(root, rel)) != before {
		t.Fatal("the never-released archive changed")
	}
	if _, err := os.Stat(filepath.Join(root, fileName)); err != nil {
		t.Fatal("the release file was consumed")
	}
}

func TestADryRunArchiveWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, fileName), releaseFile)
	res, err := writing(t, root, true, func(e *strictcli.Effects, _ git.Repo) error {
		return releaserecord.ArchiveReleaseFile(e, root, releasable, version(t, "0.4.0"), recorded(commitA))
	})
	mustNotFail(t, err)
	if _, err := os.Stat(filepath.Join(root, ".strictmetadata/releases/gadget/v0.4.0.toml")); !os.IsNotExist(err) {
		t.Fatal("a dry run wrote the archive")
	}
	requireContains(t, res.Stdout+res.Stderr, "v0.4.0.toml")
}

func TestWriteReleaseCommitMovesItAndKeepsShippedAs(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	rel := writeArchiveWith(t, root, "0.3.0", "recorded", commitA, "shipped_as = \"old@v0.3.0\"\n")
	path := filepath.Join(root, rel)
	mustNotFail(t, os.Chmod(path, 0o444))
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		return releaserecord.WriteReleaseCommit(e, root, releaserecord.ArchiveDir(releasable), version(t, "0.3.0"), recorded(commitB))
	})
	mustNotFail(t, err)
	a, err := releaserecord.ReadArchive(root, releaserecord.ArchiveDir(releasable), version(t, "0.3.0"))
	mustNotFail(t, err)
	if a.ReleaseCommit.Commit != commitB || a.ShippedAs != "old@v0.3.0" {
		t.Fatalf("read back %+v", a)
	}
	if m := mode(t, path); m != 0o444 {
		t.Errorf("the archive's mode is %o", m)
	}
}

func TestArchiveFieldWritersRefuseAnotherFate(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	writeArchive(t, root, "0.1.0", "unrecoverable", "")
	writeArchive(t, root, "0.2.0", "never-released", "")
	writeArchive(t, root, "0.3.0", "recorded", commitA)
	cases := []struct {
		name string
		do   func(e *strictcli.Effects) error
		want string
	}{
		{"release commit beside unrecoverable", func(e *strictcli.Effects) error {
			return releaserecord.WriteReleaseCommit(e, root, dir, version(t, "0.1.0"), recorded(commitB))
		}, "unrecoverable fate"},
		{"unrecoverable beside a release commit", func(e *strictcli.Effects) error {
			return releaserecord.MarkUnrecoverable(e, root, dir, version(t, "0.3.0"))
		}, "known commit"},
		{"unrecoverable beside never released", func(e *strictcli.Effects) error {
			return releaserecord.MarkUnrecoverable(e, root, dir, version(t, "0.2.0"))
		}, "never_released"},
		{"shipped as on never released", func(e *strictcli.Effects) error {
			_, err := releaserecord.RecordShippedAs(e, root, dir, version(t, "0.2.0"), "v0.2.0")
			return err
		}, "shipped under no tag"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hygiene.Isolate(t)
			_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error { return c.do(e) })
			if err == nil {
				t.Fatal("accepted")
			}
			requireContains(t, err.Error(), c.want)
		})
	}
}

func TestShippedAsIsRecordedOnce(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	writeArchive(t, root, "0.3.0", "unrecoverable", "")
	record := func(tag string) (bool, error) {
		var changed bool
		_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
			var err error
			changed, err = releaserecord.RecordShippedAs(e, root, dir, version(t, "0.3.0"), tag)
			return err
		})
		return changed, err
	}
	changed, err := record("old@v0.3.0")
	mustNotFail(t, err)
	if !changed {
		t.Fatal("the first record reported no change")
	}
	changed, err = record("old@v0.3.0")
	mustNotFail(t, err)
	if changed {
		t.Fatal("recording the same tag again reported a change")
	}
	if _, err := record("other@v0.3.0"); err == nil || !strings.Contains(err.Error(), "ships under one tag") {
		t.Fatalf("a second tag: %v", err)
	}
}

func TestANewerNoticeGoesFirst(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := releaserecord.ArchiveDir(releasable)
	writeArchive(t, root, "0.3.0", "recorded", commitA)
	for _, notice := range []string{"> deprecated", "> yanked"} {
		_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
			return releaserecord.RecordReleaseNotice(e, root, dir, version(t, "0.3.0"), notice)
		})
		mustNotFail(t, err)
	}
	a, err := releaserecord.ReadArchive(root, dir, version(t, "0.3.0"))
	mustNotFail(t, err)
	if strings.Join(a.ReleaseNotices, "|") != "> yanked|> deprecated" {
		t.Fatalf("notices %q", a.ReleaseNotices)
	}
}

func TestUnfinalizeRestoresAnEditableReleaseFile(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	rel := writeArchiveWith(t, root, "0.3.0", "recorded", commitA, "shipped_as = \"old@v0.3.0\"\nrelease_notices = [\"> deprecated\"]\n")
	path := filepath.Join(root, rel)
	var changed []string
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		var err error
		changed, err = releaserecord.Unfinalize(e, root, releasable, version(t, "0.3.0"))
		return err
	})
	mustNotFail(t, err)
	if strings.Join(changed, ",") != fileName+","+rel {
		t.Fatalf("changed %v", changed)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("the archive is still there")
	}
	f, err := releaserecord.ReadReleaseFile(root, releasable)
	mustNotFail(t, err)
	if f.Description != "the next release" {
		t.Fatalf("restored %+v", f)
	}
}

func TestUnfinalizeRefusesAReleaseFileHoldingSomeonesWork(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	rel := writeArchive(t, root, "0.3.0", "recorded", commitA)
	testsupport.WriteFile(t, filepath.Join(root, fileName), strings.Replace(releaseFile, "the next release", "someone's work", 1))
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		_, err := releaserecord.Unfinalize(e, root, releasable, version(t, "0.3.0"))
		return err
	})
	if err == nil || !strings.Contains(err.Error(), "holds someone's release fields") {
		t.Fatalf("got %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, rel)); err != nil {
		t.Fatal("the archive was removed")
	}
	// Emptying the release file, as the refusal says, clears it.
	testsupport.WriteFile(t, filepath.Join(root, fileName), "")
	_, err = writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		_, err := releaserecord.Unfinalize(e, root, releasable, version(t, "0.3.0"))
		return err
	})
	mustNotFail(t, err)
}

func TestAReleaseFileWriterRefusesWhatItsReaderRefuses(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		return releaserecord.WriteReleaseFile(e, root, releasable, releaserecord.ReleaseFile{Bump: semver.Patch, Include: []string{}, Exclude: []string{}, Description: " "})
	})
	if err == nil {
		t.Fatal("wrote a blank description")
	}
	_, err = writing(t, root, false, func(e *strictcli.Effects, _ git.Repo) error {
		return releaserecord.WriteReleaseFile(e, root, releasable, releaserecord.ReleaseFile{Bump: semver.Patch, Include: []string{"go"}, Exclude: []string{}, Description: "fixes"})
	})
	mustNotFail(t, err)
	f, err := releaserecord.ReadReleaseFile(root, releasable)
	mustNotFail(t, err)
	if f.Bump != semver.Patch || f.Include[0] != "go" {
		t.Fatalf("read back %+v", f)
	}
}
