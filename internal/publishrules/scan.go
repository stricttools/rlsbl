package publishrules

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"
)

// Text is one published text: its name in a refusal (a file, "the GitHub
// Release body", "the CHANGELOG.md section of 1.2.0") and its content.
type Text struct {
	Name    string
	Content string
}

// Scanner scans what a public repository publishes for the names the
// machine-local confidential-name index holds. In a confidential repository
// it scans nothing: the repository's own names are its to publish where it
// may, and its public outputs are refused by the publishing rules.
type Scanner struct {
	index  *index.Index
	active bool
}

// NewScanner is the scanner for a repository whose record is record, on the
// date of on, against the loaded index.
func NewScanner(record *lifecycle.Record, on time.Time, idx *index.Index) (*Scanner, error) {
	if idx == nil {
		return nil, fmt.Errorf("scanning for confidential names needs the confidential-name index; load it with index.Load(index.DefaultPath())")
	}
	return &Scanner{index: idx, active: !record.Confidential(on)}, nil
}

// Active reports whether the scanner scans: the repository is public.
func (s *Scanner) Active() bool { return s.active }

// ScanTexts refuses, naming the text, the line, the column, and the term,
// every confidential name found in texts.
func (s *Scanner) ScanTexts(texts []Text) error {
	if !s.active {
		return nil
	}
	var problems []string
	for _, t := range texts {
		for _, m := range s.index.Scan(t.Content) {
			problems = append(problems, fmt.Sprintf("%s, line %d, column %d: %q", t.Name, m.Line, m.Column, m.Term))
		}
	}
	if len(problems) == 0 {
		return nil
	}
	return fmt.Errorf("%s: a public repository publishes no confidential name, and these published texts carry names from the confidential-name index; remove each name from the text (or reword it), then run the command again:\n  - %s",
		lifecycle.RuleConfidentialNames, strings.Join(problems, "\n  - "))
}

// ScanArtifacts scans every text file the artifacts carry: a wheel entry
// from the wheel itself, every other file from the repository rooted at
// root. A file that is not UTF-8 text, or holds a NUL byte, is binary and
// not scanned.
func (s *Scanner) ScanArtifacts(root string, artifacts []Artifact) error {
	if !s.active {
		return nil
	}
	var texts []Text
	for _, a := range artifacts {
		for _, f := range a.Files {
			data := f.content
			if data == nil {
				if f.Source == "" {
					continue
				}
				read, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(f.Source)))
				if err != nil {
					return fmt.Errorf("reading %s, which %s carries: %w", f.Source, a.Label, err)
				}
				data = read
			}
			if bytes.IndexByte(data, 0) >= 0 || !utf8.Valid(data) {
				continue
			}
			texts = append(texts, Text{Name: f.Entry + " in " + a.Label, Content: string(data)})
		}
	}
	return s.ScanTexts(texts)
}
