package releaseops

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// auditFormatVersion is the format of one line of the undo audits.
const auditFormatVersion = 1

// AuditPath is the repository-relative path of a releasable's undo audits:
// one JSON object per line, one line per `release undo`, written and
// committed before the undo deletes anything.
func AuditPath(releasable string) string {
	return declarations.ReleasesDir(releasable) + "/undo-audits.jsonl"
}

// auditRecord is one undo's line: what it was about to destroy and the
// evidence that cleared it.
type auditRecord struct {
	FormatVersion int    `json:"format_version"`
	RecordedAt    string `json:"recorded_at"`
	Version       string `json:"version"`
	Tag           string `json:"tag"`
	// Latest is whether the undo reverted the latest release (its commits
	// reverted) or an earlier one (its refs and Release deleted only).
	Latest          bool       `json:"latest"`
	Verdict         Verdict    `json:"verdict"`
	Reason          string     `json:"reason"`
	Evidence        []Evidence `json:"evidence"`
	RevertedCommits []string   `json:"reverted_commits"`
	DeletedTags     []string   `json:"deleted_tags"`
	ReleaseDeleted  bool       `json:"release_deleted"`
}

// readAudits reads the undo audits as they stand, every line checked to be
// a JSON object: the file is rewritten whole to add a line, and a line that
// cannot be read would be carried along unread, so it is refused, naming
// it, with the file left as it is.
func readAudits(s Selection) (string, error) {
	rel := AuditPath(s.Releasable.Name)
	data, err := os.ReadFile(s.abs(rel))
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("reading %s: %w", rel, err)
	}
	for i, line := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var record map[string]any
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			return "", fmt.Errorf("line %d of %s is not a JSON object (%v), and the file is the record of past undos, which this undo appends to. Undo refused: nothing was destroyed, and the file is untouched. Repair that line, commit it, and run the undo again", i+1, rel, err)
		}
	}
	return string(data), nil
}

// appendAudit writes existing with record added as its last line, by
// atomic replacement.
func appendAudit(e *strictcli.Effects, s Selection, existing string, record auditRecord) error {
	rel := AuditPath(s.Releasable.Name)
	line, err := json.Marshal(record)
	if err != nil {
		return err
	}
	if existing != "" && !strings.HasSuffix(existing, "\n") {
		existing += "\n"
	}
	abs := s.abs(rel)
	tmp := abs + ".rlsbl-writing"
	if _, err := e.Write(tmp, existing+string(line)+"\n"); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	if _, err := e.Rename(tmp, abs); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	return nil
}
