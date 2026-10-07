package publishrules_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/lifecycle/index"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// diskWriter performs the index's writes, for a fixture.
type diskWriter struct{}

func (diskWriter) WriteFile(path string, data []byte) error { return os.WriteFile(path, data, 0o644) }
func (diskWriter) MkdirAll(path string) error               { return os.MkdirAll(path, 0o755) }

// gadgetIndex is an index holding one confidential repository's names.
func gadgetIndex(t *testing.T) *index.Index {
	t.Helper()
	idx, err := index.Load(filepath.Join(t.TempDir(), index.FileName))
	if err != nil {
		t.Fatal(err)
	}
	if err := idx.Upsert(diskWriter{}, "https://github.com/acme/gadget.git", []string{"gadget", "moonbeam"}); err != nil {
		t.Fatal(err)
	}
	return idx
}

// A confidential name in a published text refuses it, naming the text, the
// line, and the term; removing the name clears it.
func TestAConfidentialNameInAPublishedTextIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	s, err := publishrules.NewScanner(record(t, publicRecord), today, gadgetIndex(t))
	if err != nil {
		t.Fatal(err)
	}
	body := publishrules.Text{Name: "the GitHub Release body", Content: "## 1.2.0\n\n- Talks to the Gadget server now\n"}
	err = s.ScanTexts([]publishrules.Text{body, {Name: "the CHANGELOG.md section of 1.2.0", Content: "nothing here\n"}})
	if err == nil {
		t.Fatal("a published text naming a confidential term was not refused")
	}
	if !strings.Contains(err.Error(), `the GitHub Release body, line 3, column 16: "gadget"`) {
		t.Errorf("the refusal does not name the text, line, and term:\n%v", err)
	}
	if strings.Contains(err.Error(), "CHANGELOG.md") {
		t.Errorf("the clean text was named:\n%v", err)
	}
	// The fix the refusal names: remove the name.
	body.Content = "## 1.2.0\n\n- Talks to the server now\n"
	if err := s.ScanTexts([]publishrules.Text{body}); err != nil {
		t.Errorf("the reworded text is still refused: %v", err)
	}
}

// Only whole tokens match: a longer word carrying a name is not one.
func TestAConfidentialNameMatchesWholeTokensOnly(t *testing.T) {
	hygiene.Isolate(t)
	s, err := publishrules.NewScanner(record(t, publicRecord), today, gadgetIndex(t))
	if err != nil {
		t.Fatal(err)
	}
	if err := s.ScanTexts([]publishrules.Text{{Name: "notes", Content: "gadgetry and moonbeams\n"}}); err != nil {
		t.Errorf("a longer word was refused: %v", err)
	}
}

// A confidential repository's own outputs are not scanned.
func TestAConfidentialRepositoryIsNotScanned(t *testing.T) {
	hygiene.Isolate(t)
	s, err := publishrules.NewScanner(record(t, confidentialRecord), today, gadgetIndex(t))
	if err != nil {
		t.Fatal(err)
	}
	if s.Active() {
		t.Error("the scanner is active in a confidential repository")
	}
	if err := s.ScanTexts([]publishrules.Text{{Name: "notes", Content: "gadget\n"}}); err != nil {
		t.Errorf("err = %v", err)
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
	s, err := publishrules.NewScanner(record(t, publicRecord), today, gadgetIndex(t))
	if err != nil {
		t.Fatal(err)
	}
	err = s.ScanArtifacts(root, artifacts)
	if err == nil || !strings.Contains(err.Error(), "README.md in the `npm pack` tarball, line 2, column 1: \"moonbeam\"") {
		t.Errorf("err = %v", err)
	}
	if err != nil && strings.Contains(err.Error(), "logo.bin") {
		t.Errorf("a binary file was scanned:\n%v", err)
	}
}

func TestAScannerNeedsTheIndex(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := publishrules.NewScanner(record(t, publicRecord), today, nil); err == nil {
		t.Error("a scanner without an index was made")
	}
}
