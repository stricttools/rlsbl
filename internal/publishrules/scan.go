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

	"github.com/stricttools/rlsbl/internal/git"
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

// LoadScanner is NewScanner for the repository at root on the date of on,
// its lifecycle-and-license record and the index at indexPath read from
// disk. An empty indexPath is refused.
func LoadScanner(root, indexPath string, on time.Time) (*Scanner, error) {
	if indexPath == "" {
		return nil, fmt.Errorf("scanning for confidential names needs the confidential-name index's path (index.DefaultPath())")
	}
	record, err := lifecycle.Load(root)
	if err != nil {
		return nil, err
	}
	idx, err := index.Load(indexPath)
	if err != nil {
		return nil, err
	}
	return NewScanner(record, on, idx)
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

// blobReadBatch bounds how many blobs one git cat-file reads at a time, so
// a long range is not held in memory whole.
const blobReadBatch = 256

// ScanRange refuses, naming the commit, the path or the message, the line,
// the column, and the term, every confidential name in the commits
// reachable from include and from no exclude revision: each commit's
// message, and every text blob it adds or changes. A name added and removed
// again inside the range is refused like one still present, since the push
// carries every commit. A blob that is not UTF-8 text, or holds a NUL byte,
// is binary and not scanned. what names the range in the refusal.
func (s *Scanner) ScanRange(repo git.Repo, include, exclude []string, what string) error {
	if !s.active {
		return nil
	}
	commits, err := repo.RangeChanges(include, exclude)
	if err != nil {
		return err
	}
	var problems []string
	report := func(where string, content string) {
		for _, m := range s.index.Scan(content) {
			problems = append(problems, fmt.Sprintf("%s, line %d, column %d: %q", where, m.Line, m.Column, m.Term))
		}
	}
	// Each blob is read and scanned once; its matches are reported at every
	// commit and path that adds it.
	type site struct{ commit, path string }
	sites := map[string][]site{}
	var order []string
	for i := len(commits) - 1; i >= 0; i-- {
		c := commits[i]
		report(fmt.Sprintf("commit %s, its message", c.SHA), c.Message)
		for _, ch := range c.Changes {
			if _, seen := sites[ch.Blob]; !seen {
				order = append(order, ch.Blob)
			}
			sites[ch.Blob] = append(sites[ch.Blob], site{c.SHA, ch.Path})
		}
	}
	for start := 0; start < len(order); start += blobReadBatch {
		batch := order[start:min(start+blobReadBatch, len(order))]
		contents, err := repo.Blobs(batch)
		if err != nil {
			return err
		}
		for _, blob := range batch {
			content := contents[blob]
			if strings.IndexByte(content, 0) >= 0 || !utf8.ValidString(content) {
				continue
			}
			matches := s.index.Scan(content)
			for _, at := range sites[blob] {
				for _, m := range matches {
					problems = append(problems, fmt.Sprintf("commit %s, %s, line %d, column %d: %q", at.commit, at.path, m.Line, m.Column, m.Term))
				}
			}
		}
	}
	if len(problems) == 0 {
		return nil
	}
	return fmt.Errorf("%s: a public repository publishes no confidential name, and these commits of %s carry names from the confidential-name index; rewrite those commits so that no commit of the range carries a name (a history rewrite, such as `rlsbl release scrub`, before anything is pushed), then run the command again:\n  - %s",
		lifecycle.RuleConfidentialNames, what, strings.Join(problems, "\n  - "))
}
