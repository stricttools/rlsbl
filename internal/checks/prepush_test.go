package checks

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// pushLine is one pre-push hook line pushing local to main over remote.
func pushLine(local, remote string) string {
	return "refs/heads/main " + local + " refs/heads/main " + remote
}

func TestPushChecksSkipOutsideAPush(t *testing.T) {
	hygiene.Isolate(t)
	r, _, _ := releasedPortal(t)
	for _, name := range []string{"prepush-changelog-coverage", "prepush-manual-warning"} {
		got := runCheck(t, inputs(t, r.Dir), name)
		mustStatus(t, got, "skip")
		mustMention(t, got, "not in a push")
	}
}

func TestAPushedCommitWithoutAnEntryFailsUntilOneDescribesIt(t *testing.T) {
	hygiene.Isolate(t)
	r, released, next := releasedPortal(t)
	in := inputs(t, r.Dir)
	in.PushLines = []string{pushLine(next, released)}
	got := runCheck(t, in, "prepush-changelog-coverage")
	mustStatus(t, got, "fail")
	mustMention(t, got, next[:12], "rlsbl changelog add --commits")
	// What `rlsbl changelog add --commits <next>` writes.
	r.Write(unreleased, entryLine("1", next))
	mustStatus(t, runCheck(t, in, "prepush-changelog-coverage"), "pass")
}

func TestAPushOverACommitThisCloneLacksNamesTheFetch(t *testing.T) {
	hygiene.Isolate(t)
	origin, _, _ := releasedPortal(t)
	clone := origin.Clone()
	theirs := origin.CommitFile("theirs.txt", "theirs\n", "A commit only origin has")
	ours := clone.CommitFile("ours.txt", "ours\n", "A commit only the clone has")
	clone.Write(unreleased, entryLine("1", ours))
	in := inputs(t, clone.Dir)
	in.PushLines = []string{pushLine(ours, theirs)}
	got := runCheck(t, in, "prepush-changelog-coverage")
	mustStatus(t, got, "fail")
	mustMention(t, got, theirs, "git fetch origin")
	clone.Git("fetch", "-q", "origin")
	got = runCheck(t, in, "prepush-changelog-coverage")
	if strings.Contains(got.texts(), "git fetch origin") {
		t.Errorf("the fetch did not clear the finding: %s", got)
	}
}

func TestAnIgnoredChangelogRecordFailsUntilTheRuleIsChanged(t *testing.T) {
	hygiene.Isolate(t)
	r, _, _ := releasedPortal(t)
	r.Write(".gitignore", "CHANGELOG.md\n")
	got := runCheck(t, inputs(t, r.Dir), "prepush-gitignore-guard")
	mustStatus(t, got, "fail")
	mustMention(t, got, "CHANGELOG.md", "git check-ignore -v")
	r.Write(".gitignore", "experiments/\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "prepush-gitignore-guard"), "pass")
}

func TestAPushToAReleaseBranchByHandFails(t *testing.T) {
	hygiene.Isolate(t)
	r, released, next := releasedPortal(t)
	in := inputs(t, r.Dir)
	in.PushLines = []string{pushLine(next, released)}
	got := runCheck(t, in, "prepush-manual-warning")
	mustStatus(t, got, "fail")
	mustMention(t, got, "main", "rlsbl release run")
	in.PushLines = []string{"refs/heads/main " + next + " refs/heads/topic " + strings.Repeat("0", 40)}
	mustStatus(t, runCheck(t, in, "prepush-manual-warning"), "pass")
}

func TestUnreadablePushLinesLeaveThePushChecksUnanswered(t *testing.T) {
	hygiene.Isolate(t)
	r, _, _ := releasedPortal(t)
	in := inputs(t, r.Dir)
	in.PushLines = []string{"refs/heads/main only-two"}
	got := runCheck(t, in, "prepush-manual-warning")
	if !strings.Contains(got.Unanswered, "pre-push input") {
		t.Errorf("unreadable push lines were not refused: %s", got)
	}
}
