package lifecycleops

import (
	"fmt"

	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/git"
)

// squashRange is one proprietary period's commits on the release branch's
// first-parent history: First through Last, both inclusive, folded into one
// commit by the declassification.
type squashRange struct {
	From string `json:"from"`
	// Until is empty for a period still open.
	Until string `json:"until"`
	First string `json:"first"`
	Last  string `json:"last"`
	Count int    `json:"count"`
}

// describe names the range's period for the operator.
func (r squashRange) describe() string {
	if r.Until == "" {
		return "the proprietary period from " + r.From
	}
	return "the proprietary period from " + r.From + " until " + r.Until
}

// declassifySquashPlan computes the squash ranges of a declassification:
// for each proprietary period (the record's ProprietaryPeriods, disjoint and
// in date order), the commits of history (the release branch's first-parent
// history, oldest first) whose committer date falls in it, by the
// committer's own calendar date. A period no commit falls in has nothing to
// squash. A period whose commits are not contiguous on the history is
// refused, naming the first commit that interrupts it, and so is a merge
// commit inside a range, since folding it would drop the history it merged
// in; both are refused before anything is rewritten. The ranges come oldest
// first.
func declassifySquashPlan(history []git.FirstParentCommit, periods []lifecycle.Period) ([]squashRange, error) {
	var ranges []squashRange
	for _, p := range periods {
		first, last := -1, -1
		for i, c := range history {
			if p.Contains(c.Committed) {
				if first < 0 {
					first = i
				}
				last = i
			}
		}
		if first < 0 {
			continue
		}
		r := squashRange{From: day(p.From), First: history[first].SHA, Last: history[last].SHA, Count: last - first + 1}
		if !p.Open() {
			r.Until = day(p.Until)
		}
		for i := first; i <= last; i++ {
			c := history[i]
			if !p.Contains(c.Committed) {
				return nil, fmt.Errorf("the commits of %s are not contiguous on the release branch's first-parent history: %s (%q, committed %s) lies outside the period, between %s and %s, which lie inside it, so the period cannot be squashed into one commit keeping the public history around it; nothing was rewritten", r.describe(), c.SHA, c.Subject, c.Committed.Format("2006-01-02 15:04:05 -0700"), history[first].SHA, history[last].SHA)
			}
			if len(c.Parents) > 1 {
				return nil, fmt.Errorf("%s (%q) inside %s is a merge commit, and squashing the period would drop the history it merged in; nothing was rewritten", c.SHA, c.Subject, r.describe())
			}
		}
		ranges = append(ranges, r)
	}
	return ranges, nil
}
