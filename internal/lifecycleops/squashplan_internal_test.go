package lifecycleops

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
)

func date(y int, m time.Month, d int) time.Time { return time.Date(y, m, d, 0, 0, 0, 0, time.UTC) }

// historyOf is a first-parent history of one commit per date, named c0,
// c1, ..., each committed at noon in the given offset.
func historyOf(offset *time.Location, dates ...time.Time) []git.FirstParentCommit {
	out := make([]git.FirstParentCommit, len(dates))
	for i, d := range dates {
		sha := fmt.Sprintf("%040d", i)
		var parents []string
		if i > 0 {
			parents = []string{out[i-1].SHA}
		}
		out[i] = git.FirstParentCommit{SHA: sha, Parents: parents, Committed: time.Date(d.Year(), d.Month(), d.Day(), 12, 0, 0, 0, offset), Subject: fmt.Sprintf("c%d", i)}
	}
	return out
}

func TestDeclassifySquashPlanFindsOneRangePerPeriod(t *testing.T) {
	hygiene.Isolate(t)
	history := historyOf(time.UTC,
		date(2026, 1, 10),                  // c0 public
		date(2026, 2, 5),                   // c1 first period
		date(2026, 2, 28),                  // c2 first period, its last day
		date(2026, 3, 1),                   // c3 public: the period's until is the first day after it
		date(2026, 5, 2), date(2026, 5, 3)) // c4, c5 the open period
	periods := []lifecycle.Period{
		{From: date(2026, 2, 1), Until: date(2026, 3, 1)},
		{From: date(2026, 4, 1), Until: date(2026, 4, 15)},
		{From: date(2026, 5, 1)},
	}
	ranges, err := declassifySquashPlan(history, periods)
	if err != nil {
		t.Fatal(err)
	}
	want := []squashRange{
		{From: "2026-02-01", Until: "2026-03-01", First: history[1].SHA, Last: history[2].SHA, Count: 2},
		{From: "2026-05-01", First: history[4].SHA, Last: history[5].SHA, Count: 2},
	}
	if fmt.Sprint(ranges) != fmt.Sprint(want) {
		t.Fatalf("ranges %+v, want %+v", ranges, want)
	}
}

func TestDeclassifySquashPlanReadsTheCommittersOwnDate(t *testing.T) {
	hygiene.Isolate(t)
	// Noon on 2026-02-01 at UTC-14 is 2026-02-02 in UTC; the committer's
	// own date is the first, which the period does not hold.
	behind := time.FixedZone("behind", -14*3600)
	history := historyOf(behind, date(2026, 2, 1), date(2026, 2, 2))
	ranges, err := declassifySquashPlan(history, []lifecycle.Period{{From: date(2026, 2, 2)}})
	if err != nil {
		t.Fatal(err)
	}
	if len(ranges) != 1 || ranges[0].First != history[1].SHA || ranges[0].Count != 1 {
		t.Fatalf("ranges %+v", ranges)
	}
}

func TestDeclassifySquashPlanRefusesAnInterruptedPeriodAndAMerge(t *testing.T) {
	hygiene.Isolate(t)
	// c2's committer date is before the period, between two commits inside
	// it: the period's commits are not contiguous.
	history := historyOf(time.UTC, date(2026, 1, 10), date(2026, 2, 5), date(2026, 1, 20), date(2026, 2, 9))
	_, err := declassifySquashPlan(history, []lifecycle.Period{{From: date(2026, 2, 1), Until: date(2026, 3, 1)}})
	if err == nil || !strings.Contains(err.Error(), history[2].SHA+` ("c2"`) || !strings.Contains(err.Error(), "not contiguous") {
		t.Fatalf("an interrupted period: %v", err)
	}

	history = historyOf(time.UTC, date(2026, 1, 10), date(2026, 2, 5), date(2026, 2, 9))
	history[1].Parents = append(history[1].Parents, strings.Repeat("f", 40))
	_, err = declassifySquashPlan(history, []lifecycle.Period{{From: date(2026, 2, 1)}})
	if err == nil || !strings.Contains(err.Error(), history[1].SHA) || !strings.Contains(err.Error(), "merge commit") {
		t.Fatalf("a merge inside a period: %v", err)
	}

	ranges, err := declassifySquashPlan(history[:1], []lifecycle.Period{{From: date(2026, 2, 1)}})
	if err != nil || len(ranges) != 0 {
		t.Fatalf("a period no commit falls in: %+v, %v", ranges, err)
	}
}
