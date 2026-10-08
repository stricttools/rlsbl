package changelog

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
)

// minAbbreviation is git's shortest abbreviation of a commit id: a shorter
// id in an entry is never matched by prefix.
const minAbbreviation = 4

// mapCommit maps one possibly abbreviated id through a rewrite map:
// exactly, or by the one key it is a prefix of. ambiguous is true when it
// is a prefix of more than one key.
func mapCommit(h string, rewrites map[string]string) (next string, ambiguous bool) {
	if next, ok := rewrites[h]; ok {
		return next, false
	}
	if len(h) < minAbbreviation {
		return "", false
	}
	var matches []string
	for old := range rewrites {
		if strings.HasPrefix(old, h) {
			matches = append(matches, old)
		}
	}
	switch len(matches) {
	case 0:
		return "", false
	case 1:
		return rewrites[matches[0]], false
	}
	return "", true
}

// RewriteHolds reports whether the commit map rewrites holds the id h: h is
// one of its keys, or the prefix of one or more of them, so it named a
// commit the rewrite took in.
func RewriteHolds(h string, rewrites map[string]string) bool {
	next, ambiguous := mapCommit(h, rewrites)
	return next != "" || ambiguous
}

// CanRemap reports whether a remap with rewrites would rewrite the id h: h
// matches one key exactly or is a prefix of one key alone. A history
// rewrite asks it before changing anything, to know whether its journal
// repairs a commit id the changelog names.
func CanRemap(h string, rewrites map[string]string) bool {
	next, _ := mapCommit(h, rewrites)
	return next != ""
}

// Dirs are every changelog directory of the repository, repository-relative
// and sorted: each releasable's, declared or not, and each retired
// subject's, since all of them name commits a history rewrite renames.
func Dirs(root string) ([]string, error) {
	var dirs []string
	for _, parent := range []struct{ base, inner string }{
		{declarations.ChangelogRoot, ""},
		{declarations.RetiredHistoriesRoot, "changelog"},
	} {
		items, err := os.ReadDir(absolute(root, parent.base))
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", parent.base, err)
		}
		for _, item := range items {
			if !item.IsDir() {
				continue
			}
			dir := path.Join(parent.base, item.Name(), parent.inner)
			if info, err := os.Stat(absolute(root, dir)); err == nil && info.IsDir() {
				dirs = append(dirs, dir)
			} else if err != nil && !errors.Is(err, fs.ErrNotExist) {
				return nil, fmt.Errorf("reading %s: %w", dir, err)
			}
		}
	}
	sort.Strings(dirs)
	return dirs, nil
}

// RemapGlobs are the repository-relative patterns of every changelog file a
// history rewrite must remap commit ids in, for safegit's --remap-shas-in
// (Go path.Match: '*' never crosses '/'). They cover changelog directories
// present only in the history being rewritten as well.
func RemapGlobs() []string {
	return []string{
		declarations.ChangelogRoot + "/*/*.jsonl",
		declarations.RetiredHistoriesRoot + "/*/changelog/*.jsonl",
	}
}

// RemappedFile is one file a remap rewrote.
type RemappedFile struct {
	// Path is repository-relative.
	Path            string
	EntriesModified int
	CommitsRemapped int
}

// RemapReport is what a remap did and could not do.
type RemapReport struct {
	Files []RemappedFile
	// Unmapped are, per file, the ids that matched no key of the map; whether
	// that is a problem (they still resolve, or they do not) is the
	// caller's question.
	Unmapped map[string][]string
	// Ambiguous are, per file, the abbreviated ids that are a prefix of more
	// than one key, left as they were.
	Ambiguous map[string][]string
}

// Remap rewrites commit ids in every changelog file of every changelog
// directory (Dirs) through rewrites, a map from old full ids to new ones
// (git.ParseRewriteMap reads one). Only files holding a mapped id are
// written; every other line keeps its bytes, and each file keeps its mode
// (a released file stays read-only). An entry whose commits map to one
// commit (a squash folds several into one) names it once.
func Remap(e *strictcli.Effects, root string, rewrites map[string]string) (RemapReport, error) {
	report := RemapReport{Unmapped: map[string][]string{}, Ambiguous: map[string][]string{}}
	dirs, err := Dirs(root)
	if err != nil {
		return report, err
	}
	for _, dir := range dirs {
		files, err := ReadAll(root, dir)
		if err != nil {
			return report, err
		}
		for _, f := range files {
			changed, err := remapFile(e, root, f, rewrites, &report)
			if err != nil {
				return report, err
			}
			if changed.Path != "" {
				report.Files = append(report.Files, changed)
			}
		}
	}
	return report, nil
}

func remapFile(e *strictcli.Effects, root string, f *File, rewrites map[string]string, report *RemapReport) (RemappedFile, error) {
	result := RemappedFile{}
	lines := texts(f)
	for i, l := range f.Lines {
		entry := l.Entry
		commits := make([]string, len(entry.Commits))
		changed := false
		for j, h := range entry.Commits {
			next, ambiguous := mapCommit(h, rewrites)
			switch {
			case next != "":
				commits[j] = next
				changed = true
				result.CommitsRemapped++
			case ambiguous:
				commits[j] = h
				report.Ambiguous[f.Path] = appendOnce(report.Ambiguous[f.Path], h)
			default:
				commits[j] = h
				report.Unmapped[f.Path] = appendOnce(report.Unmapped[f.Path], h)
			}
		}
		if !changed {
			continue
		}
		// A squash maps several commits to one, so an entry naming two
		// folded commits names the squash commit once.
		entry.Commits = dedupe(commits)
		if err := Validate(entry); err != nil {
			return RemappedFile{}, fmt.Errorf("line %d of %s: %w", l.Number, f.Path, err)
		}
		lines[i] = Serialize(entry)
		result.EntriesModified++
	}
	if result.EntriesModified == 0 {
		return RemappedFile{}, nil
	}
	if err := rewrite(e, root, f, lines); err != nil {
		return RemappedFile{}, err
	}
	result.Path = f.Path
	return result, nil
}

func appendOnce(list []string, v string) []string {
	for _, x := range list {
		if x == v {
			return list
		}
	}
	return append(list, v)
}

// UnresolvedCommits are, per changelog file of every changelog directory,
// the commit ids that do not resolve in repo. A history rewrite asks it
// before and after it remaps.
func UnresolvedCommits(repo git.Repo, root string) (map[string][]string, error) {
	dirs, err := Dirs(root)
	if err != nil {
		return nil, err
	}
	res := newResolver(repo)
	out := map[string][]string{}
	for _, dir := range dirs {
		files, err := ReadAll(root, dir)
		if err != nil {
			return nil, err
		}
		for _, f := range files {
			for _, l := range f.Lines {
				for _, h := range l.Entry.Commits {
					full, err := res.resolve(h)
					if err != nil {
						return nil, err
					}
					if full == "" {
						out[f.Path] = appendOnce(out[f.Path], h)
					}
				}
			}
		}
	}
	return out, nil
}
