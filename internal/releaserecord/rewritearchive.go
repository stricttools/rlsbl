package releaserecord

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
)

// rewriteArchiveFormatVersion is the format version of a history-rewrite archive.
const rewriteArchiveFormatVersion = 1

// rewriteArchiveTimeLayout names an archive by the UTC time its rewrite started,
// to the second.
const rewriteArchiveTimeLayout = "20060102T150405Z"

// The operations a history-rewrite archive records.
const (
	OperationScrub      = "scrub"
	OperationDeclassify = "declassify"
)

// RewriteArchive is the committed record of one history rewrite: the commit
// map, the tags it moved, and why. It is committed to the repository, so it
// must never carry what the rewrite removed: its fields are a closed set of
// commit ids, tag refs, the operation, its mode, and the operator's reason.
// No pattern, replacement, file path, or matched content is ever written.
// Every committed archive's map is one of the records `release reconcile`
// reads to explain a moved ref, the one that is present in a fresh clone,
// where safegit's journal is not.
type RewriteArchive struct {
	// Operation is what rewrote the history: OperationScrub or
	// OperationDeclassify.
	Operation string
	// Mode is the operation's mode ("pattern", "file", "recipe", "squash").
	Mode             string
	Reason           string
	OldHead          string
	NewHead          string
	CommitsRewritten int
	// Rewrites maps each old commit to the commit the rewrite wrote.
	Rewrites map[string]string
	Tags     []ArchivedTag
	// SquashCommits are the commits a declassification squashed each
	// proprietary period into, as the rewrite left them; empty for any
	// other operation. Every changelog entry of a squashed period names its
	// squash commit, so the changelog batch checks leave these commits out.
	SquashCommits []string
}

// ArchivedTag is one tag the rewrite moved.
type ArchivedTag struct {
	Refname   string `toml:"refname"`
	OldSHA    string `toml:"old_sha"`
	NewSHA    string `toml:"new_sha"`
	Annotated bool   `toml:"annotated"`
}

type rawRewriteArchive struct {
	FormatVersion    int64             `toml:"format_version,required"`
	Operation        string            `toml:"operation,required"`
	Mode             string            `toml:"mode,required"`
	Reason           string            `toml:"reason,required"`
	OldHead          string            `toml:"old_head,required"`
	NewHead          string            `toml:"new_head,required"`
	CommitsRewritten int64             `toml:"commits_rewritten,required"`
	Rewrites         map[string]string `toml:"rewrites,required"`
	Tags             []ArchivedTag     `toml:"tags"`
	SquashCommits    []string          `toml:"squash_commits"`
}

// RewriteArchivePath is the repository-relative path of the archive of a
// rewrite started at started.
func RewriteArchivePath(started time.Time) string {
	return declarations.HistoryRewritesDir + "/" + started.UTC().Format(rewriteArchiveTimeLayout) + ".toml"
}

// renderRewriteArchive writes the archive as TOML, keys in order.
func renderRewriteArchive(a RewriteArchive) []byte {
	q := tomledit.QuoteString
	var b strings.Builder
	b.WriteString("# The record of one history rewrite, written by rlsbl. It names commits and\n")
	b.WriteString("# tags only: never what the rewrite removed.\n")
	fmt.Fprintf(&b, "format_version = %d\n", rewriteArchiveFormatVersion)
	fmt.Fprintf(&b, "operation = %s\n", q(a.Operation))
	fmt.Fprintf(&b, "mode = %s\n", q(a.Mode))
	fmt.Fprintf(&b, "reason = %s\n", q(a.Reason))
	fmt.Fprintf(&b, "old_head = %s\n", q(a.OldHead))
	fmt.Fprintf(&b, "new_head = %s\n", q(a.NewHead))
	fmt.Fprintf(&b, "commits_rewritten = %d\n", a.CommitsRewritten)
	if len(a.SquashCommits) > 0 {
		quoted := make([]string, len(a.SquashCommits))
		for i, c := range a.SquashCommits {
			quoted[i] = q(c)
		}
		fmt.Fprintf(&b, "squash_commits = [%s]\n", strings.Join(quoted, ", "))
	}
	olds := make([]string, 0, len(a.Rewrites))
	for old := range a.Rewrites {
		olds = append(olds, old)
	}
	sort.Strings(olds)
	b.WriteString("\n[rewrites]\n")
	for _, old := range olds {
		fmt.Fprintf(&b, "%s = %s\n", tomledit.QuoteKey(old), q(a.Rewrites[old]))
	}
	for _, t := range a.Tags {
		b.WriteString("\n[[tags]]\n")
		fmt.Fprintf(&b, "refname = %s\n", q(t.Refname))
		fmt.Fprintf(&b, "old_sha = %s\n", q(t.OldSHA))
		fmt.Fprintf(&b, "new_sha = %s\n", q(t.NewSHA))
		fmt.Fprintf(&b, "annotated = %t\n", t.Annotated)
	}
	return []byte(b.String())
}

// parseRewriteArchive reads one archive strictly: an unknown key, a missing key,
// and another format version are refused, naming the file.
func parseRewriteArchive(rel string, data []byte) (RewriteArchive, error) {
	raw, err := tomledit.Unmarshal[rawRewriteArchive](data)
	if err != nil {
		return RewriteArchive{}, fmt.Errorf("the history-rewrite archive %s cannot be read: %v", rel, err)
	}
	if raw.FormatVersion != rewriteArchiveFormatVersion {
		return RewriteArchive{}, fmt.Errorf("the history-rewrite archive %s has format_version %d, and this rlsbl reads format %d", rel, raw.FormatVersion, rewriteArchiveFormatVersion)
	}
	if len(raw.SquashCommits) > 0 && raw.Operation != OperationDeclassify {
		return RewriteArchive{}, fmt.Errorf("the history-rewrite archive %s records squash_commits for the operation %q; only a %s records them", rel, raw.Operation, OperationDeclassify)
	}
	for _, c := range raw.SquashCommits {
		if !git.IsObjectID(c) {
			return RewriteArchive{}, fmt.Errorf("the history-rewrite archive %s records the squash commit %q, which is not a full commit id", rel, c)
		}
	}
	return RewriteArchive{
		Operation:        raw.Operation,
		Mode:             raw.Mode,
		Reason:           raw.Reason,
		OldHead:          raw.OldHead,
		NewHead:          raw.NewHead,
		CommitsRewritten: int(raw.CommitsRewritten),
		Rewrites:         raw.Rewrites,
		Tags:             raw.Tags,
		SquashCommits:    raw.SquashCommits,
	}, nil
}

// WriteRewriteArchive writes the archive of a rewrite started at started and
// returns its repository-relative path. An archive already at that path is
// refused: each rewrite has its own.
func WriteRewriteArchive(e *strictcli.Effects, root string, started time.Time, a RewriteArchive) (string, error) {
	rel := RewriteArchivePath(started)
	data := renderRewriteArchive(a)
	if _, err := parseRewriteArchive(rel, data); err != nil {
		return "", err
	}
	target := filepath.Join(root, filepath.FromSlash(rel))
	if _, err := os.Lstat(target); err == nil {
		return "", fmt.Errorf("refusing to write %s: an archive of another rewrite is already there", rel)
	} else if !errors.Is(err, fs.ErrNotExist) {
		return "", fmt.Errorf("reading %s: %w", rel, err)
	}
	if err := declarations.EnsureOwnedDirectory(e, root, declarations.HistoryRewritesDir); err != nil {
		return "", err
	}
	if _, err := e.Mkdir(filepath.Dir(target)); err != nil {
		return "", fmt.Errorf("creating %s: %w", declarations.HistoryRewritesDir, err)
	}
	if _, err := e.Write(target, data); err != nil {
		return "", fmt.Errorf("writing %s: %w", rel, err)
	}
	return rel, nil
}

// ReadRewriteArchives reads every committed history-rewrite archive of the
// repository rooted at root, in name order (the order the rewrites ran). An
// archive that cannot be read is an error: it is one of the records that
// explains a divergence, and passing over it would refuse a repair the
// repository has the evidence for.
func ReadRewriteArchives(root string) (map[string]RewriteArchive, []string, error) {
	dir := filepath.Join(root, filepath.FromSlash(declarations.HistoryRewritesDir))
	entries, err := os.ReadDir(dir)
	if errors.Is(err, fs.ErrNotExist) {
		return map[string]RewriteArchive{}, nil, nil
	}
	if err != nil {
		return nil, nil, fmt.Errorf("listing %s: %w", declarations.HistoryRewritesDir, err)
	}
	archives := map[string]RewriteArchive{}
	var names []string
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".toml") || entry.Name() == "manifest.toml" {
			continue
		}
		rel := path.Join(declarations.HistoryRewritesDir, entry.Name())
		data, err := os.ReadFile(filepath.Join(dir, entry.Name()))
		if err != nil {
			return nil, nil, fmt.Errorf("reading %s: %w", rel, err)
		}
		a, err := parseRewriteArchive(rel, data)
		if err != nil {
			return nil, nil, err
		}
		archives[rel] = a
		names = append(names, rel)
	}
	sort.Strings(names)
	return archives, names, nil
}

// DeclassifySquashCommits are the commits the committed history-rewrite
// archives of the repository rooted at root record as a declassification's
// squash commits, each followed through every later rewrite that moved it,
// so each is the id the history holds now.
func DeclassifySquashCommits(root string) (map[string]bool, error) {
	archives, names, err := ReadRewriteArchives(root)
	if err != nil {
		return nil, err
	}
	squashes := map[string]bool{}
	for _, name := range names {
		a := archives[name]
		moved := map[string]bool{}
		for c := range squashes {
			if next, ok := a.Rewrites[c]; ok {
				moved[next] = true
			} else {
				moved[c] = true
			}
		}
		squashes = moved
		if a.Operation == OperationDeclassify {
			for _, c := range a.SquashCommits {
				squashes[c] = true
			}
		}
	}
	return squashes, nil
}
