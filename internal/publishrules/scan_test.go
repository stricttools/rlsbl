package publishrules_test

import (
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/confidential"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// gadgetList is a confidential-term list naming another repository's
// private names.
func gadgetList(t *testing.T) *confidential.List {
	t.Helper()
	l, err := confidential.FromEntries("the test list", []confidential.Entry{
		{Term: "gadget", Added: "2026-10-01", Reason: "a private project"},
		{Term: "moonbeam", Added: "2026-10-01", Reason: "a private project's codename"},
	})
	if err != nil {
		t.Fatal(err)
	}
	return l
}

func scanner(t *testing.T, recordText string, resolutions ...confidential.Resolution) *publishrules.Scanner {
	t.Helper()
	s, err := publishrules.NewScanner(record(t, recordText), today, gadgetList(t), []string{"portal"}, resolutions)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

var hitID = regexp.MustCompile(`hit ([0-9a-f]{16})`)

// A confidential term in a published text refuses it, naming the text, the
// line, the column, the hit's id, and the fix; a recorded false-positive
// judgment, or removing the term, clears it.
func TestAConfidentialTermInAPublishedTextIsRefusedUntilResolved(t *testing.T) {
	hygiene.Isolate(t)
	s := scanner(t, publicRecord)
	body := publishrules.Text{Name: "the GitHub Release body", Content: "## 1.2.0\n\n- Talks to the Gadget server now\n"}
	err := s.ScanTexts([]publishrules.Text{body, {Name: "the CHANGELOG.md section of 1.2.0", Content: "nothing here\n"}})
	if err == nil {
		t.Fatal("a published text naming a confidential term was not refused")
	}
	for _, want := range []string{"the GitHub Release body, line 3, column 16: - Talks to the >>Gadget<< server now", "rlsbl confidential judge-false-positive", "rlsbl confidential approve-hit"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not say %q:\n%v", want, err)
		}
	}
	m := hitID.FindStringSubmatch(err.Error())
	if m == nil {
		t.Fatalf("the refusal names no hit id:\n%v", err)
	}
	judged := scanner(t, publicRecord, confidential.Resolution{Hit: m[1], Location: "the GitHub Release body, line 3, column 16", Decision: confidential.FalsePositive, Certainty: 75, Reason: "a word for a device", Recorded: "2026-10-01"})
	if err := judged.ScanTexts([]publishrules.Text{body}); err != nil {
		t.Errorf("the judged hit is still refused: %v", err)
	}
	if used := judged.Used(); len(used) != 1 || used[0].Hit != m[1] {
		t.Errorf("the scanner does not report the resolution it used: %+v", used)
	}
	body.Content = "## 1.2.0\n\n- Talks to the server now\n"
	if err := s.ScanTexts([]publishrules.Text{body}); err != nil {
		t.Errorf("the reworded text is still refused: %v", err)
	}
}

// A term matches anywhere, ignoring case, a longer word carrying it
// included: whether that is a false positive is judged per hit.
func TestATermMatchesInsideLongerWords(t *testing.T) {
	hygiene.Isolate(t)
	err := scanner(t, publicRecord).ScanTexts([]publishrules.Text{{Name: "notes", Content: "gadgetry and moonbeams\n"}})
	if err == nil || !strings.Contains(err.Error(), ">>gadget<<ry") || !strings.Contains(err.Error(), ">>moonbeam<<s") {
		t.Errorf("err = %v", err)
	}
}

// A confidential repository's pushes and Releases are not scanned, and its
// packed artifacts, which a registry makes public, are.
func TestAConfidentialRepositoryScansOnlyItsArtifacts(t *testing.T) {
	hygiene.Isolate(t)
	s := scanner(t, confidentialRecord)
	if s.Active() {
		t.Error("the scanner is active in a confidential repository")
	}
	if err := s.ScanTexts([]publishrules.Text{{Name: "notes", Content: "gadget\n"}}); err != nil {
		t.Errorf("err = %v", err)
	}
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, "client/README.md"), "talks to the gadget server\n")
	err := s.ScanArtifacts(root, []publishrules.Artifact{{Label: "the wheel", Files: []publishrules.PackedFile{{Entry: "README.md", Source: "client/README.md"}}}})
	if err == nil || !strings.Contains(err.Error(), ">>gadget<<") {
		t.Errorf("a packed artifact of a confidential repository was not scanned: %v", err)
	}
}

// Every text file a packed artifact carries is scanned; a binary file is
// not.
func TestPackedTextFilesAreScanned(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, "portal/README.md"), "Built for\nmoonbeam.\n")
	testsupport.WriteFile(t, filepath.Join(root, "portal/logo.bin"), "gadget\x00\x01")
	artifacts := []publishrules.Artifact{{Label: "the `npm pack` tarball", Files: []publishrules.PackedFile{
		{Entry: "README.md", Source: "portal/README.md"},
		{Entry: "logo.bin", Source: "portal/logo.bin"},
	}}}
	err := scanner(t, publicRecord).ScanArtifacts(root, artifacts)
	if err == nil || !strings.Contains(err.Error(), "README.md in the `npm pack` tarball, line 2, column 1: >>moonbeam<<.") {
		t.Errorf("err = %v", err)
	}
	if err != nil && strings.Contains(err.Error(), "logo.bin") {
		t.Errorf("a binary file was scanned:\n%v", err)
	}
}

func TestAScannerNeedsTheList(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := publishrules.NewScanner(record(t, publicRecord), today, nil, nil, nil); err == nil {
		t.Error("a scanner without a list was made")
	}
}
