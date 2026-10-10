package publishrules

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/stricttools/strictspec/go/confidential"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/git"
)

// Text is one published text: where it is ("README.md", "the GitHub Release
// body of v1.2.3", "the changelog section of 1.2.0") and its content.
type Text struct {
	Name    string
	Content string
}

// ResolveFix is what every refusal of an unresolved confidential-term hit
// tells the agent to do.
const ResolveFix = "Judge each hit. When you are at least 70% certain it is a false positive, record it: " +
	"`rlsbl confidential judge-false-positive --hit <id> --certainty <70-100> --reason <one line, without the term>`. " +
	"Every other hit goes to the owner, who either has it fixed (reword the text; a commit not yet pushed is rewritten " +
	"with `safegit scrub match`, then `rlsbl changelog remap --from-journal`) or approves publishing it, which only the owner may do, hit by hit: " +
	"`rlsbl confidential approve-hit --hit <id> --reason <the owner's reason>`. Then run the command again."

// Scanner finds the confidential-term list's entries in what a public
// repository publishes, and refuses while a hit is neither judged a false
// positive nor approved by the owner in the repository's resolutions
// (.strictmetadata/confidential-hits/resolutions.toml). In a confidential
// repository it scans nothing: the repository's own text is its to carry,
// and its public outputs are refused by the publishing rules.
type Scanner struct {
	list        *confidential.List
	matcher     *confidential.Matcher
	resolutions []confidential.Resolution
	active      bool
	used        map[string]confidential.Resolution
}

// NewScanner is the scanner for a repository whose record is record, on the
// date of on, known by repositoryNames (confidential.RepositoryNames), with
// the list and the repository's recorded resolutions.
func NewScanner(record *lifecycle.Record, on time.Time, list *confidential.List, repositoryNames []string, resolutions []confidential.Resolution) (*Scanner, error) {
	if list == nil {
		return nil, fmt.Errorf("scanning for confidential terms needs the confidential-term list; load it with confidential.Load")
	}
	return &Scanner{
		list:        list,
		matcher:     list.For(repositoryNames),
		resolutions: resolutions,
		active:      !record.Confidential(on),
		used:        map[string]confidential.Resolution{},
	}, nil
}

// LoadScanner is NewScanner for the repository repo is rooted at, on the
// date of on: its lifecycle-and-license record, its names (its directory's
// and its origin's), and its resolutions read from disk, and the list read
// from loc.
func LoadScanner(repo git.Repo, loc confidential.Location, on time.Time) (*Scanner, error) {
	return LoadScannerAt(repo, repo.Dir(), loc, on)
}

// LoadScannerAt is LoadScanner with the record and the resolutions read from
// root, a checkout of repo's history (the release's checkout), while the
// repository's names come from repo.
func LoadScannerAt(repo git.Repo, root string, loc confidential.Location, on time.Time) (*Scanner, error) {
	if loc.List == "" || loc.Identity == "" {
		return nil, fmt.Errorf("scanning for confidential terms needs the confidential-term list's location (confidential.DefaultLocation)")
	}
	record, err := lifecycle.Load(root)
	if err != nil {
		return nil, err
	}
	names, err := RepositoryNames(repo)
	if err != nil {
		return nil, err
	}
	list, err := confidential.Load(loc)
	if err != nil {
		return nil, err
	}
	resolutions, err := confidential.LoadResolutions(root)
	if err != nil {
		return nil, err
	}
	return NewScanner(record, on, list, names, resolutions)
}

// RepositoryNames are the names of the repository repo belongs to, which an
// entry's except scope names: its main work tree's directory (a linked work
// tree, such as the release's checkout, answers for the repository it
// belongs to), and its origin's last path segment when it has an origin.
func RepositoryNames(repo git.Repo) ([]string, error) {
	root := repo.Dir()
	common, err := repo.CommonDir()
	if err != nil {
		return nil, err
	}
	if filepath.Base(common) == ".git" {
		root = filepath.Dir(common)
	}
	origin := ""
	configured, err := repo.RemoteConfigured("origin")
	if err != nil {
		return nil, err
	}
	if configured {
		if origin, err = repo.RemoteURL("origin"); err != nil {
			return nil, err
		}
	}
	return confidential.RepositoryNames(root, origin)
}

// Active reports whether the scanner scans: the repository is public.
func (s *Scanner) Active() bool { return s.active }

// Matcher is the matcher of the entries that apply to the repository.
func (s *Scanner) Matcher() *confidential.Matcher { return s.matcher }

// Resolutions are the repository's recorded resolutions.
func (s *Scanner) Resolutions() []confidential.Resolution {
	return append([]confidential.Resolution(nil), s.resolutions...)
}

// Status is one sentence about the list: that it does not exist, so there
// are no terms, or how many entries it holds and how many apply here.
func (s *Scanner) Status() string {
	if s.list.Missing() {
		return s.list.Status()
	}
	return fmt.Sprintf("%s, %d of which apply to this repository", s.list.Status(), s.matcher.Len())
}

// Used is every resolution that resolved a hit of a scan this scanner made,
// in hit-id order: what the release records.
func (s *Scanner) Used() []confidential.Resolution {
	out := make([]confidential.Resolution, 0, len(s.used))
	for _, r := range s.used {
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Hit < out[j].Hit })
	return out
}

// Resolve splits hits by whether a recorded resolution resolves them, and
// remembers every resolution that did (see Used).
func (s *Scanner) Resolve(hits []confidential.Hit) confidential.Outcome {
	o := confidential.Resolve(hits, s.resolutions)
	for _, r := range o.Used() {
		s.used[r.Hit] = r
	}
	return o
}

// Refuse refuses, naming what, every unresolved hit of hits.
func (s *Scanner) Refuse(what string, hits []confidential.Hit) error {
	return s.Resolve(hits).Refusal(what, ResolveFix)
}

// HitsInTexts are the hits in texts; none in a confidential repository.
func (s *Scanner) HitsInTexts(texts []Text) []confidential.Hit {
	if !s.active {
		return nil
	}
	var hits []confidential.Hit
	for _, t := range texts {
		hits = append(hits, s.matcher.Scan(t.Name, t.Content)...)
	}
	return hits
}

// ScanTexts refuses every unresolved hit in texts.
func (s *Scanner) ScanTexts(texts []Text) error {
	if !s.active {
		return nil
	}
	names := make([]string, len(texts))
	for i, t := range texts {
		names[i] = t.Name
	}
	return s.Refuse(strings.Join(names, ", "), s.HitsInTexts(texts))
}

// ScanArtifacts scans every text file the artifacts carry: a wheel entry
// from the wheel itself, every other file from the repository rooted at
// root. A file that is not UTF-8 text, or holds a NUL byte, is binary and
// not scanned.
func (s *Scanner) ScanArtifacts(root string, artifacts []Artifact) error {
	if !s.active || s.matcher.Len() == 0 {
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
	return s.Refuse("the packed artifacts", s.HitsInTexts(texts))
}

// HitsInRange are the hits in the commits reachable from include and from
// no exclude revision: each commit's whole message (trailers included), the
// path of every file it adds, and every line it adds or changes in a text
// file (a line the file already held in the version the commit changes is
// not the commit's to answer for). A term added and removed again inside
// the range is a hit like one still present, since the push carries every
// commit. A blob that is not UTF-8 text, or holds a NUL byte, is binary and
// not scanned. None in a confidential repository.
func (s *Scanner) HitsInRange(repo git.Repo, include, exclude []string) ([]confidential.Hit, error) {
	if !s.active || s.matcher.Len() == 0 {
		return nil, nil
	}
	commits, err := repo.RangeChanges(include, exclude)
	if err != nil {
		return nil, err
	}
	terms, literal := s.matcher.Terms()
	var hits []confidential.Hit
	for i := len(commits) - 1; i >= 0; i-- {
		c := commits[i]
		short := c.SHA[:min(len(c.SHA), 12)]
		hits = append(hits, s.matcher.Scan("the message of commit "+short, c.Message)...)
		var paths []string
		seen := map[string]bool{}
		for _, ch := range c.Changes {
			if ch.OldBlob == "" {
				hits = append(hits, s.matcher.Scan("the path of a file commit "+short+" adds", ch.Path)...)
			}
			if !seen[ch.Path] {
				seen[ch.Path] = true
				paths = append(paths, ch.Path)
			}
		}
		if literal {
			holding, err := repo.FilesHolding(c.SHA, paths, terms)
			if err != nil {
				return nil, err
			}
			keep := map[string]bool{}
			for _, p := range holding {
				keep[p] = true
			}
			paths = paths[:0]
			for p := range seen {
				if keep[p] {
					paths = append(paths, p)
				}
			}
			sort.Strings(paths)
		}
		if len(paths) == 0 {
			continue
		}
		wanted := map[string]bool{}
		for _, p := range paths {
			wanted[p] = true
		}
		var objects []string
		for _, ch := range c.Changes {
			if wanted[ch.Path] {
				objects = append(objects, ch.Blob)
				if ch.OldBlob != "" {
					objects = append(objects, ch.OldBlob)
				}
			}
		}
		blobs, err := repo.Blobs(objects)
		if err != nil {
			return nil, fmt.Errorf("reading the files commit %s changes, to scan them for confidential terms: %w", short, err)
		}
		for _, ch := range c.Changes {
			if !wanted[ch.Path] {
				continue
			}
			after := blobs[ch.Blob]
			if strings.IndexByte(after, 0) >= 0 || !utf8.ValidString(after) {
				continue
			}
			before := ""
			if ch.OldBlob != "" {
				before = blobs[ch.OldBlob]
			}
			hits = append(hits, addedLineHits(s.matcher, fmt.Sprintf("%s in commit %s", ch.Path, short), before, after, ch.OldBlob == "")...)
		}
	}
	return hits, nil
}

// addedLineHits are the hits of after on the lines it adds against before:
// a line of after counts as added unless the same line occurs in before, each
// line of before accounting for one occurrence. Every line of a new file is
// added.
func addedLineHits(m *confidential.Matcher, where, before, after string, isNew bool) []confidential.Hit {
	remaining := map[string]int{}
	if !isNew {
		for _, line := range strings.Split(before, "\n") {
			remaining[line]++
		}
	}
	added := map[int]bool{}
	for i, line := range strings.Split(after, "\n") {
		if remaining[line] > 0 {
			remaining[line]--
			continue
		}
		added[i+1] = true
	}
	var out []confidential.Hit
	for _, h := range m.Scan(where, after) {
		if added[h.Line] {
			out = append(out, h)
		}
	}
	return out
}

// ScanRange refuses every unresolved hit in the commits reachable from
// include and from no exclude revision (see HitsInRange). what names the
// range in the refusal.
func (s *Scanner) ScanRange(repo git.Repo, include, exclude []string, what string) error {
	hits, err := s.HitsInRange(repo, include, exclude)
	if err != nil {
		return err
	}
	return s.Refuse(what, hits)
}
