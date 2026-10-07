package releaserecord_test

import (
	"errors"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

const fileName = ".strictmetadata/releases/gadget/unreleased.toml"

func parseRefusal(t *testing.T, text string) string {
	t.Helper()
	_, err := releaserecord.ParseReleaseFile(fileName, []byte(text))
	if err == nil {
		t.Fatalf("the release file was accepted:\n%s", text)
	}
	var fe *releaserecord.FileError
	if !errors.As(err, &fe) || fe.File != fileName {
		t.Fatalf("the refusal does not name the file: %v", err)
	}
	if strings.Count(err.Error(), fileName) != 1 {
		t.Fatalf("the file is named other than once: %v", err)
	}
	return err.Error()
}

func TestAMinimalReleaseFileParses(t *testing.T) {
	hygiene.Isolate(t)
	f, err := releaserecord.ParseReleaseFile(fileName, []byte(releaseFile+"context = \"  why  \"\n"))
	mustNotFail(t, err)
	if f.Bump != semver.Minor || len(f.Include) != 1 || f.Include[0] != "npm" || len(f.Exclude) != 0 {
		t.Fatalf("parsed %+v", f)
	}
	if f.Description != "the next release" || f.Context != "why" {
		t.Fatalf("the prose is not trimmed: %+v", f)
	}
}

func TestEveryBumpParses(t *testing.T) {
	hygiene.Isolate(t)
	for _, b := range semver.Bumps {
		text := strings.Replace(releaseFile, `"minor"`, `"`+string(b)+`"`, 1)
		f, err := releaserecord.ParseReleaseFile(fileName, []byte(text))
		mustNotFail(t, err)
		if f.Bump != b {
			t.Fatalf("bump %s parsed as %s", b, f.Bump)
		}
	}
}

func TestReleaseFileRefusals(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		name, text string
		wants      []string
	}{
		{"missing bump", strings.Replace(releaseFile, "bump = \"minor\"\n", "", 1), []string{"bump"}},
		{"unknown bump", strings.Replace(releaseFile, `"minor"`, `"huge"`, 1), []string{"bump"}},
		{"prerelease bump", strings.Replace(releaseFile, `"minor"`, `"prerelease"`, 1), []string{"pre-release channel"}},
		{"preid", releaseFile + "preid = \"rc\"\n", []string{"preid is refused", "pre-release channel"}},
		{"blog", releaseFile + "blog = true\n", []string{"blog is refused"}},
		{"first format", strings.Replace(releaseFile, "format_version = 2", "format_version = 1", 1), []string{"rlsbl migrate records"}},
		{"missing format version", strings.Replace(releaseFile, "format_version = 2\n", "", 1), []string{"format_version"}},
		{"first format release commit", releaseFile + "candidate_sha = \"" + strings.Repeat("a", 40) + "\"\n", []string{"candidate_sha", "rlsbl migrate records"}},
		{"overlap", strings.Replace(releaseFile, "exclude = []", `exclude = ["npm"]`, 1), []string{"npm"}},
		{"blank description", strings.Replace(releaseFile, `"the next release"`, `"   "`, 1), []string{"description must be set"}},
		{"unknown key", releaseFile + "targets = []\n", []string{"targets"}},
		{"include not strings", strings.Replace(releaseFile, `["npm"]`, `[1]`, 1), []string{"include"}},
		{"flow-owned release commit", releaseFile + "release_commit = \"" + strings.Repeat("a", 40) + "\"\n\n[released_trees]\n\".\" = \"" + strings.Repeat("b", 40) + "\"\n", []string{"release_commit, released_trees", "only rlsbl writes these"}},
		{"flow-owned marker", releaseFile + "never_released = true\n", []string{"never_released"}},
		{"two fates", releaseFile + "never_released = true\nunrecoverable = true\n", nil},
		{"not toml", "bump = \n", nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hygiene.Isolate(t)
			requireContains(t, parseRefusal(t, c.text), c.wants...)
		})
	}
}

func TestAMissingReleaseFileNamesReleaseInit(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	_, err := releaserecord.ReadReleaseFile(root, releasable)
	if err == nil {
		t.Fatal("a missing release file was read")
	}
	requireContains(t, err.Error(), fileName, "rlsbl release init")
	testsupport.WriteFile(t, filepath.Join(root, fileName), releaseFile)
	_, err = releaserecord.ReadReleaseFile(root, releasable)
	mustNotFail(t, err)
}

func TestPristineReleaseFiles(t *testing.T) {
	hygiene.Isolate(t)
	type verdict struct {
		text string
		want bool
	}
	for _, c := range []verdict{
		{"", true},
		{"  \n", true},
		{"bump = \"\"\ndescription = \"\"\n", true},
		{"bump = \"patch\"\ndescription = \"\"\n", false},
		{"bump = \"\"\ndescription = \"x\"\n", false},
		{"this is [not toml", false},
	} {
		if got := releaserecord.IsPristineReleaseFile([]byte(c.text)); got != c.want {
			t.Errorf("IsPristineReleaseFile(%q) = %v", c.text, got)
		}
	}
	for _, c := range []verdict{
		{"", true},
		{"[releasables.gadget]\nbump = \"\"\ndescription = \"\"\n", true},
		{"[releasables.gadget]\nbump = \"minor\"\ndescription = \"\"\n", false},
		{"releasables = 3\n", false},
		{"[releasables]\ngadget = 1\n", false},
	} {
		if got := releaserecord.IsPristineBatchReleaseFile([]byte(c.text)); got != c.want {
			t.Errorf("IsPristineBatchReleaseFile(%q) = %v", c.text, got)
		}
	}
}

const batchFile = `format_version = 2

[releasables.gadget]
bump = "patch"
include = ["go"]
exclude = []
description = "gadget fixes"

[releasables.widget]
bump = "minor"
include = []
exclude = ["npm"]
description = "widget features"
context = "why"
`

func TestABatchReleaseFileParsesAndRendersBack(t *testing.T) {
	hygiene.Isolate(t)
	b, err := releaserecord.ParseBatchReleaseFile(releaserecord.BatchReleaseFilePath, []byte(batchFile))
	mustNotFail(t, err)
	if got := strings.Join(b.Names(), ","); got != "gadget,widget" {
		t.Fatalf("names %s", got)
	}
	if b.Releasables["widget"].Bump != semver.Minor || b.Releasables["widget"].Context != "why" {
		t.Fatalf("widget %+v", b.Releasables["widget"])
	}
	again, err := releaserecord.ParseBatchReleaseFile(releaserecord.BatchReleaseFilePath, releaserecord.RenderBatchReleaseFile(b))
	mustNotFail(t, err)
	if again.Releasables["gadget"].Description != "gadget fixes" || again.Releasables["widget"].Exclude[0] != "npm" {
		t.Fatalf("the rendering does not read back: %+v", again)
	}
}

func TestBatchReleaseFileRefusals(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		name, text string
		wants      []string
	}{
		{"packages", strings.Replace(batchFile, "[releasables.gadget]", "[packages.gadget]", 1), []string{"[packages] section is refused"}},
		{"no releasables", "format_version = 2\n", []string{"releasables"}},
		{"empty releasables", "format_version = 2\n[releasables]\n", []string{"names no releasable"}},
		{"preid in a table", strings.Replace(batchFile, "context = \"why\"\n", "preid = \"rc\"\n", 1), []string{"[releasables.widget] preid is refused"}},
		{"unknown key in a table", strings.Replace(batchFile, "context = \"why\"\n", "targets = []\n", 1), []string{"targets"}},
		{"blank description", strings.Replace(batchFile, `"gadget fixes"`, `" "`, 1), []string{"[releasables.gadget] description must be set"}},
		{"flow-owned field", strings.Replace(batchFile, "context = \"why\"\n", "shipped_as = \"v1.0.0\"\n", 1), []string{"[releasables.widget] shipped_as"}},
		{"first format", strings.Replace(batchFile, "format_version = 2", "format_version = 1", 1), []string{"rlsbl migrate records"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hygiene.Isolate(t)
			_, err := releaserecord.ParseBatchReleaseFile(releaserecord.BatchReleaseFilePath, []byte(c.text))
			if err == nil {
				t.Fatalf("accepted:\n%s", c.text)
			}
			requireContains(t, err.Error(), append([]string{releaserecord.BatchReleaseFilePath}, c.wants...)...)
		})
	}
}
