package changelog_test

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestVersionsAreListedHighestFirstAndAStrayFileIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := changelog.Dir("portal")
	got, err := changelog.Versions(root, dir)
	mustNotFail(t, err)
	if len(got) != 0 {
		t.Fatalf("a missing directory listed %v", got)
	}
	for _, v := range []string{"0.2.0", "0.10.0", "0.9.1"} {
		writeReleased(t, root, "portal", v, internalEntry(strings.ReplaceAll(v, ".", ""), sha1))
	}
	writeLines(t, root, dir+"/unreleased.jsonl")
	testsupport.WriteFile(t, filepath.Join(root, dir, "CHANGELOG.md"), "# Changelog\n")
	got, err = changelog.Versions(root, dir)
	mustNotFail(t, err)
	var names []string
	for _, v := range got {
		names = append(names, v.String())
	}
	if strings.Join(names, ",") != "0.10.0,0.9.1,0.2.0" {
		t.Fatalf("listed %v", names)
	}
	stray := filepath.Join(root, dir, "1.0.0-rc.1.jsonl")
	testsupport.WriteFile(t, stray, "")
	_, err = changelog.Versions(root, dir)
	if err == nil {
		t.Fatal("a pre-release file was passed over")
	}
	requireContains(t, err.Error(), "1.0.0-rc.1.jsonl", "rename it or remove it")
	mustNotFail(t, os.Remove(stray))
	_, err = changelog.Versions(root, dir)
	mustNotFail(t, err)
}

func TestReadingNamesEveryRefusedLine(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	rel := changelog.Dir("portal") + "/unreleased.jsonl"
	good := line(feature("1", "first", sha1))
	text := good + "\n\n" + `{"format_version":1,"commits":["` + sha1 + `"],"user_facing":false}` + "\n" + `{"format_version":2,"commits":[]}` + "\n"
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), text)
	_, err := changelog.ReadUnreleased(root, changelog.Dir("portal"))
	var fe *changelog.FileError
	if !errors.As(err, &fe) {
		t.Fatalf("want a file error, got %T: %v", err, err)
	}
	if len(fe.Lines) != 2 || fe.Lines[0].Line != 3 || fe.Lines[1].Line != 4 || fe.FormatVersionOnly() {
		t.Fatalf("refused lines: %v", err)
	}
	requireContains(t, err.Error(), "line 3 of "+rel, "line 4 of "+rel)
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), good+"\n")
	f, err := changelog.ReadUnreleased(root, changelog.Dir("portal"))
	mustNotFail(t, err)
	if len(f.Lines) != 1 || f.Lines[0].Number != 1 || f.Released {
		t.Fatalf("read %+v", f)
	}
}

func TestAMissingUnreleasedFileIsEmptyAndAMissingVersionIsAnError(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	f, err := changelog.ReadUnreleased(root, changelog.Dir("portal"))
	mustNotFail(t, err)
	if len(f.Lines) != 0 || f.Path != changelog.Dir("portal")+"/unreleased.jsonl" {
		t.Fatalf("read %+v", f)
	}
	if _, err := changelog.ReadVersion(root, changelog.Dir("portal"), version(t, "1.0.0")); err == nil {
		t.Fatal("a missing released file read")
	}
}

func appending(t *testing.T, root string, dryRun bool, fn func(e *strictcli.Effects) error) (strictcli.Result, error) {
	t.Helper()
	return writing(t, root, dryRun, func(e *strictcli.Effects, _ git.Repo) error { return fn(e) })
}

func TestAppendCreatesTheDirectoryWithItsManifestAndKeepsEveryLine(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := changelog.Dir("portal")
	_, err := appending(t, root, false, func(e *strictcli.Effects) error {
		return changelog.AppendEntry(e, root, dir, feature("1", "first", sha1))
	})
	mustNotFail(t, err)
	if got := readText(t, filepath.Join(root, ".strictmetadata", "changelog", "manifest.toml")); got != "owner = \"rlsbl\"\n" {
		t.Errorf("manifest: %q", got)
	}
	unreleased := filepath.Join(root, filepath.FromSlash(dir), "unreleased.jsonl")
	// A line written by hand keeps its bytes when another is appended.
	handWritten := strings.Replace(line(feature("1", "first", sha1)), `"user_facing":true`, `"user_facing": true`, 1)
	testsupport.WriteFile(t, unreleased, handWritten)
	_, err = appending(t, root, false, func(e *strictcli.Effects) error {
		return changelog.AppendEntry(e, root, dir, internalEntry("2", sha1))
	})
	mustNotFail(t, err)
	if got, want := readText(t, unreleased), handWritten+"\n"+line(internalEntry("2", sha1))+"\n"; got != want {
		t.Fatalf("got:\n%s\nwant:\n%s", got, want)
	}
	if mode(t, unreleased) != 0o644 {
		t.Errorf("mode %o", mode(t, unreleased))
	}
}

func TestAnInvalidEntryIsNotAppended(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := appending(t, root, false, func(e *strictcli.Effects) error {
		return changelog.AppendEntry(e, root, changelog.Dir("portal"), changelog.Entry{ID: id("1"), Commits: []string{sha1}, UserFacing: true})
	})
	if err == nil {
		t.Fatal("a user-facing entry without a description was appended")
	}
	if _, err := os.Stat(filepath.Join(root, ".strictmetadata")); !os.IsNotExist(err) {
		t.Fatalf("a refused append wrote .strictmetadata (%v)", err)
	}
}

func TestAppendUnderDryRunWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := appending(t, root, true, func(e *strictcli.Effects) error {
		return changelog.AppendEntry(e, root, changelog.Dir("portal"), feature("1", "first", sha1))
	})
	mustNotFail(t, err)
	if _, err := os.Stat(filepath.Join(root, ".strictmetadata")); !os.IsNotExist(err) {
		t.Fatalf("a dry run wrote .strictmetadata (%v)", err)
	}
}

func TestAmendingAReleasedVersionKeepsItReadOnly(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	rel := writeReleased(t, root, "portal", "1.0.0", feature("1", "first", sha1))
	_, err := appending(t, root, false, func(e *strictcli.Effects) error {
		return changelog.AppendToVersion(e, root, changelog.Dir("portal"), version(t, "1.0.0"), feature("2", "second", sha1))
	})
	mustNotFail(t, err)
	path := filepath.Join(root, filepath.FromSlash(rel))
	if mode(t, path) != 0o444 {
		t.Errorf("mode %o", mode(t, path))
	}
	if got := readText(t, path); got != line(feature("1", "first", sha1))+"\n"+line(feature("2", "second", sha1))+"\n" {
		t.Errorf("got:\n%s", got)
	}
	_, err = appending(t, root, false, func(e *strictcli.Effects) error {
		return changelog.AppendToVersion(e, root, changelog.Dir("portal"), version(t, "2.0.0"), feature("3", "third", sha1))
	})
	if err == nil {
		t.Fatal("amending a version without a file succeeded")
	}
}

func TestFinalizeLocksTheReleaseAndLeavesAnEmptyUnreleasedFile(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := changelog.Dir("portal")
	writeLines(t, root, dir+"/unreleased.jsonl", feature("1", "first", sha1))
	_, err := appending(t, root, false, func(e *strictcli.Effects) error {
		return changelog.Finalize(e, root, dir, version(t, "1.0.0"))
	})
	mustNotFail(t, err)
	released := filepath.Join(root, filepath.FromSlash(dir), "1.0.0.jsonl")
	if mode(t, released) != 0o444 || readText(t, released) != line(feature("1", "first", sha1))+"\n" {
		t.Errorf("released file: mode %o, %q", mode(t, released), readText(t, released))
	}
	if got := readText(t, filepath.Join(root, filepath.FromSlash(dir), "unreleased.jsonl")); got != "" {
		t.Errorf("unreleased file: %q", got)
	}
	_, err = appending(t, root, false, func(e *strictcli.Effects) error {
		return changelog.Finalize(e, root, dir, version(t, "1.0.0"))
	})
	if err == nil {
		t.Fatal("a released file was overwritten")
	}
	requireContains(t, err.Error(), "1.0.0.jsonl already exists", "saferm")
}

func TestFinalizeRefusesAMissingUnreleasedFile(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := appending(t, root, false, func(e *strictcli.Effects) error {
		return changelog.Finalize(e, root, changelog.Dir("portal"), version(t, "1.0.0"))
	})
	if err == nil {
		t.Fatal("finalizing without an unreleased file succeeded")
	}
	requireContains(t, err.Error(), "unreleased.jsonl does not exist")
}

func TestUnfinalizePutsTheReleasedLinesAheadOfTheEntriesWrittenSince(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	dir := changelog.Dir("portal")
	writeReleased(t, root, "portal", "1.0.0", feature("1", "released", sha1))
	writeLines(t, root, dir+"/unreleased.jsonl", feature("2", "since", sha1))
	var changed []string
	_, err := appending(t, root, false, func(e *strictcli.Effects) error {
		var err error
		changed, err = changelog.Unfinalize(e, root, dir, version(t, "1.0.0"))
		return err
	})
	mustNotFail(t, err)
	if strings.Join(changed, ",") != dir+"/unreleased.jsonl,"+dir+"/1.0.0.jsonl" {
		t.Errorf("changed %v", changed)
	}
	unreleased := filepath.Join(root, filepath.FromSlash(dir), "unreleased.jsonl")
	if got := readText(t, unreleased); got != line(feature("1", "released", sha1))+"\n"+line(feature("2", "since", sha1))+"\n" {
		t.Errorf("got:\n%s", got)
	}
	if mode(t, unreleased) != 0o644 {
		t.Errorf("mode %o", mode(t, unreleased))
	}
	if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(dir), "1.0.0.jsonl")); !os.IsNotExist(err) {
		t.Errorf("the released file remains (%v)", err)
	}
	_, err = appending(t, root, false, func(e *strictcli.Effects) error {
		changed, err = changelog.Unfinalize(e, root, dir, version(t, "1.0.0"))
		return err
	})
	mustNotFail(t, err)
	if len(changed) != 0 {
		t.Errorf("unfinalizing a version without a file changed %v", changed)
	}
}
