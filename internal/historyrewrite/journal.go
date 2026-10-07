// Package historyrewrite is rlsbl's account of history rewrites: `release
// scrub`, which rewrites the history through safegit and repairs every
// record the rewrite renamed (the changelog's commit ids, the archives'
// release commits, the tags, and the GitHub Release documents); `release
// reconcile`, which makes the tags and Releases on origin agree with what the
// records say was released, refusing whatever no record explains; and
// `release backfill`, which brings every version's archive into the fate
// model from the repository's own history.
//
// The three share one question, what accounts for a tag, which
// releaserecord.Explanations answers (archives, their shipped_as, the
// lifecycle-and-license record's unversioned tags and closed identities),
// and the repair of published refs and Releases after a rewrite, which
// `transition declassify` runs too.
package historyrewrite

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/rlsbl/internal/git"
)

// journalFile is safegit's rewrite journal, under the repository's common
// git directory: each rewrite appends a start record (its whole commit map,
// written before any ref moves), a refs record, and a complete record, all
// sharing one id.
const journalFile = "safegit/rewrite-maps.jsonl"

// Journal is the last rewrite safegit's journal records.
type Journal struct {
	ID        string
	Op        string
	Reason    string
	CreatedAt string
	// CommitMap maps each old commit to the commit the rewrite wrote.
	CommitMap map[string]string
	// Complete is false for a rewrite whose start record has no complete
	// record: it stopped part-way. Its map was written before any ref
	// moved, so it still names every commit it rewrote.
	Complete bool
	// Path is the journal's absolute path.
	Path string
}

// Label names the rewrite in a plan or an error.
func (j Journal) Label() string {
	return "safegit rewrite journal (" + j.ID + ")"
}

type journalRecord struct {
	ID        string            `json:"id"`
	Phase     string            `json:"phase"`
	Op        string            `json:"op"`
	Reason    string            `json:"reason"`
	CreatedAt string            `json:"created_at"`
	CommitMap map[string]string `json:"commit_map"`
}

// ReadJournal reads the last rewrite of safegit's journal in the
// repository: the rewrite whose start record comes last. found is false when
// there is no journal, or no start record in it. A line that is not a JSON
// object is an error naming the file and the line: a repair from part of a
// map would mis-repair silently.
func ReadJournal(repo git.Repo) (j Journal, found bool, err error) {
	common, err := repo.CommonDir()
	if err != nil {
		return Journal{}, false, err
	}
	path := filepath.Join(common, filepath.FromSlash(journalFile))
	f, err := os.Open(path)
	if errors.Is(err, fs.ErrNotExist) {
		return Journal{}, false, nil
	}
	if err != nil {
		return Journal{}, false, fmt.Errorf("reading %s: %w", path, err)
	}
	defer f.Close()
	starts := map[string]journalRecord{}
	complete := map[string]bool{}
	var order []string
	// A start record holds a whole commit map, so a line may be megabytes
	// long: lines are read without a length limit.
	reader := bufio.NewReader(f)
	for number := 1; ; number++ {
		line, readErr := reader.ReadString('\n')
		if readErr != nil && !errors.Is(readErr, io.EOF) {
			return Journal{}, false, fmt.Errorf("reading %s: %w", path, readErr)
		}
		if text := strings.TrimSpace(line); text != "" {
			var rec journalRecord
			if err := json.Unmarshal([]byte(text), &rec); err != nil {
				return Journal{}, false, fmt.Errorf("the safegit rewrite journal %s is corrupt at line %d: %v; a repair from part of a commit map would mis-repair silently, so nothing was read from it. Inspect the line (`safegit doctor` reports the journal's state)", path, number, err)
			}
			switch {
			case rec.ID == "" || rec.Phase == "":
			case rec.Phase == "start":
				if _, seen := starts[rec.ID]; !seen {
					starts[rec.ID] = rec
					order = append(order, rec.ID)
				}
			case rec.Phase == "complete":
				complete[rec.ID] = true
			}
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
	}
	if len(order) == 0 {
		return Journal{}, false, nil
	}
	last := starts[order[len(order)-1]]
	commitMap := last.CommitMap
	if commitMap == nil {
		commitMap = map[string]string{}
	}
	return Journal{
		ID:        last.ID,
		Op:        last.Op,
		Reason:    last.Reason,
		CreatedAt: last.CreatedAt,
		CommitMap: commitMap,
		Complete:  complete[last.ID],
		Path:      path,
	}, true, nil
}
