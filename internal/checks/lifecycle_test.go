package checks

import (
	"fmt"
	"regexp"
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/confidential"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestAMissingRecordFailsUntilALicenseIsDeclared(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{recordFile: ""})
	got := runCheck(t, inputs(t, r.Dir), "lifecycle-record-valid")
	mustStatus(t, got, "fail")
	mustMention(t, got, "rlsbl transition license --subject <releasable>")
	// What `rlsbl transition license` writes.
	r.Write(recordFile, publicRecord)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "lifecycle-record-valid"), "pass")
}

func TestARecordTheSchemaRefusesFailsAndLeavesItsReadersUnanswered(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{recordFile: publicRecord + "surprise = 1\n"})
	got := runCheck(t, inputs(t, r.Dir), "lifecycle-record-valid")
	mustStatus(t, got, "fail")
	mustMention(t, got, recordFile)
	refused := runCheck(t, inputs(t, r.Dir), "license-consistency")
	if !strings.Contains(refused.Unanswered, "lifecycle-record-valid") {
		t.Errorf("a reader of an unreadable record answered: %s", refused)
	}
}

func TestConfidentialTermFieldsInTheRecordAreRefused(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{recordFile: strings.Replace(publicRecord, "format_version = 1\n", "format_version = 1\ncodenames = [\"gizmo\"]\n", 1)})
	got := runCheck(t, inputs(t, r.Dir), "lifecycle-record-valid")
	mustStatus(t, got, "fail")
	mustMention(t, got, "codenames")
	// The fix: remove the field; confidential terms live in the list.
	r.Write(recordFile, publicRecord)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "lifecycle-record-valid"), "pass")
}

func TestARecordKeepsWhatItHeldAtTheNearestReleaseCommit(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{recordFile: movedRecord})
	released := r.Head()
	r.Git("tag", "v1.0.0", released)
	writeArchive(t, r, "portal", "1.0.0", released, map[string]string{".": strings.Repeat("e", 40)})
	r.CommitFile("main.go", "package main\n\nfunc main() {}\n", "Add the entry point")
	got := runCheck(t, inputs(t, r.Dir), "lifecycle-record-valid")
	mustStatus(t, got, "pass")
	mustMention(t, got, "1 nearest release commit(s)")
	dropped := strings.Replace(movedRecord, "value = \"https://github.com/acme/oldportal\"", "value = \"https://github.com/acme/elsewhere\"", 1)
	r.Write(recordFile, dropped)
	got = runCheck(t, inputs(t, r.Dir), "lifecycle-record-valid")
	mustStatus(t, got, "fail")
	mustMention(t, got, released)
	r.Write(recordFile, movedRecord)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "lifecycle-record-valid"), "pass")
}

// withGizmo writes a confidential-term list naming "gizmo", another
// repository's private name, where in looks for the list.
func withGizmo(t *testing.T, in Inputs) Inputs {
	t.Helper()
	testsupport.WriteTermList(t, in.TermList, testsupport.TermListOf("gizmo"))
	return in
}

// pushedToOrigin gives r a bare origin holding its main branch as it is now,
// with the remote-tracking branch a fetch leaves.
func pushedToOrigin(r *testsupport.Repo) {
	r.AddBareRemote("origin")
	r.Git("push", "-q", "origin", "main")
	r.Git("fetch", "-q", "origin")
}

var hitIDPattern = regexp.MustCompile(`hit ([0-9a-f]{16})`)

// hitIDs are the hit ids an outcome names, in order.
func hitIDs(got result) []string {
	var ids []string
	for _, m := range hitIDPattern.FindAllStringSubmatch(got.texts(), -1) {
		ids = append(ids, m[1])
	}
	return ids
}

// writeResolution writes a resolutions file holding r, as the confidential
// commands write it.
func writeResolution(t *testing.T, repo *testsupport.Repo, r confidential.Resolution) {
	t.Helper()
	repo.Write(confidential.StoreFile, string(confidential.RenderResolutions([]confidential.Resolution{r})))
}

func TestAConfidentialTermInAnUnpushedFileFailsUntilJudgedAFalsePositive(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	pushedToOrigin(r)
	added := r.CommitFile("README.md", "portal talks to Gizmo.\n", "Describe the portal")
	in := withGizmo(t, inputs(t, r.Dir))
	got := runCheck(t, in, "confidential-terms")
	mustStatus(t, got, "fail")
	mustMention(t, got, "README.md in commit "+added[:12]+", line 1, column 17", ">>Gizmo<<", "the term \"gizmo\"", "origin/main..HEAD", "rlsbl confidential judge-false-positive")
	ids := hitIDs(got)
	if len(ids) != 1 {
		t.Fatalf("hit ids %v in %s", ids, got)
	}
	// The fix the refusal names: a recorded false-positive judgment.
	writeResolution(t, r, confidential.Resolution{Hit: ids[0], Location: "README.md, line 1, column 17", Decision: confidential.FalsePositive, Certainty: 80, Reason: "a word in a sentence", Recorded: "2026-10-01"})
	got = runCheck(t, in, "confidential-terms")
	mustStatus(t, got, "pass")
	mustMention(t, got, "(1 resolved)", "passed over as a false positive (80% certain): a word in a sentence")
}

func TestAnOwnerApprovalResolvesAHit(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	pushedToOrigin(r)
	r.CommitFile("README.md", "portal talks to Gizmo.\n", "Describe the portal")
	in := withGizmo(t, inputs(t, r.Dir))
	ids := hitIDs(runCheck(t, in, "confidential-terms"))
	writeResolution(t, r, confidential.Resolution{Hit: ids[0], Location: "README.md, line 1, column 17", Decision: confidential.Approved, Reason: "the owner publishes this name", Recorded: "2026-10-01"})
	got := runCheck(t, in, "confidential-terms")
	mustStatus(t, got, "pass")
	mustMention(t, got, "published with the owner's approval: the owner publishes this name")
}

func TestEveryHitIsReportedAtOnce(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	pushedToOrigin(r)
	r.CommitFile("README.md", "portal talks to Gizmo.\nand gizmo answers\n", "Describe the portal")
	named := r.CommitFile("main.go", "package main\n", "Serve what gizmo asks for")
	in := withGizmo(t, inputs(t, r.Dir))
	got := runCheck(t, in, "confidential-terms")
	mustStatus(t, got, "fail")
	mustMention(t, got, "3 unresolved confidential-term hit(s)", "the message of commit "+named[:12], "line 2, column 5")
	if ids := hitIDs(got); len(ids) != 3 {
		t.Fatalf("hit ids %v in %s", ids, got)
	}
}

func TestALineTheFileAlreadyHeldIsNotAHit(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"README.md": "portal talks to Gizmo.\nmore\n"})
	pushedToOrigin(r)
	r.CommitFile("README.md", "portal talks to Gizmo.\nmore text\n", "Reword")
	mustStatus(t, runCheck(t, withGizmo(t, inputs(t, r.Dir)), "confidential-terms"), "pass")
}

func TestThePathOfAnAddedFileIsScanned(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	pushedToOrigin(r)
	added := r.CommitFile("notes/gizmo-plan.md", "the plan\n", "Plan the work")
	got := runCheck(t, withGizmo(t, inputs(t, r.Dir)), "confidential-terms")
	mustStatus(t, got, "fail")
	mustMention(t, got, "the path of a file commit "+added[:12]+" adds")
}

func TestATermAddedAndRemovedInTheUnpushedRangeFailsUntilTheHistoryIsRewritten(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	pushedToOrigin(r)
	pushed := r.Head()
	added := r.CommitFile("notes/plan.md", "talk to Gizmo first\n", "Plan the work")
	r.CommitFile("notes/plan.md", "talk to the server first\n", "Reword the plan")
	in := withGizmo(t, inputs(t, r.Dir))
	got := runCheck(t, in, "confidential-terms")
	mustStatus(t, got, "fail")
	mustMention(t, got, "notes/plan.md in commit "+added[:12]+", line 1, column 9", "origin/main..HEAD")
	// The other fix: rewrite the unpushed commits so that none carries it.
	r.Git("reset", "-q", "--hard", pushed)
	r.CommitFile("notes/plan.md", "talk to the server first\n", "Plan the work")
	mustStatus(t, runCheck(t, in, "confidential-terms"), "pass")
}

// Binary blobs and text that is not UTF-8 are not scanned, and do not stop
// the scan of the rest.
func TestAnUnpushedBinaryOrNonUTF8FileIsNotScanned(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	pushedToOrigin(r)
	r.Write("logo.bin", "\x00\xff\xfe gizmo \x01")
	r.Write("latin1.txt", "caf\xe9 au lait\n")
	r.Git("add", "-A")
	r.Git("commit", "-q", "-m", "Add a logo and a note")
	in := withGizmo(t, inputs(t, r.Dir))
	mustStatus(t, runCheck(t, in, "confidential-terms"), "pass")
	named := r.CommitFile("notes.md", "ask gizmo\n", "Take notes")
	got := runCheck(t, in, "confidential-terms")
	mustStatus(t, got, "fail")
	mustMention(t, got, "notes.md in commit "+named[:12])
}

func TestWithNoRemoteTrackingBranchTheWholeHistoryIsScanned(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	named := r.CommitFile("notes.md", "gizmo\n", "Take notes")
	r.CommitFile("notes.md", "nothing\n", "Clear the notes")
	got := runCheck(t, withGizmo(t, inputs(t, r.Dir)), "confidential-terms")
	mustStatus(t, got, "fail")
	mustMention(t, got, "notes.md in commit "+named[:12], "the history of HEAD")
}

func TestAMissingTermListHasNoTermsAndSaysSo(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	r.CommitFile("notes.md", "gizmo\n", "Take notes")
	got := runCheck(t, inputs(t, r.Dir), "confidential-terms")
	mustStatus(t, got, "pass")
	mustMention(t, got, "does not exist, so there are no confidential terms to scan for")
}

func TestAConfidentialRepositoryIsNotScanned(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{recordFile: proprietaryRecord, "README.md": "gizmo\n"})
	mustStatus(t, runCheck(t, withGizmo(t, inputs(t, r.Dir)), "confidential-terms"), "skip")
}

func TestRepositoryVisibilityHoldsGitHubToTheRecord(t *testing.T) {
	hygiene.Isolate(t)
	r := newRepo(t, fmt.Sprintf(githubPortal, "none"), map[string]string{"VERSION": "1.0.0\n", "go.mod": "module github.com/acme/portal\n\ngo 1.26\n", recordFile: publicRecord})
	get := []string{"api", "--method", "GET", "repos/acme/portal"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: get, Stdout: `{"full_name":"acme/portal","visibility":"private","private":true,"archived":false}`},
		testsupport.GHAnswer{Args: get, Stdout: `{"full_name":"acme/portal","visibility":"public","private":false,"archived":false}`},
	)
	got := runCheck(t, inputs(t, r.Dir), "repository-visibility")
	mustStatus(t, got, "fail")
	mustMention(t, got, "make the GitHub repository public")
	// After the owner makes the repository public.
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "repository-visibility"), "pass")
}

func TestAProprietaryReleasableNeedsAPrivateRepository(t *testing.T) {
	hygiene.Isolate(t)
	r := newRepo(t, fmt.Sprintf(githubPortal, "none"), map[string]string{"VERSION": "1.0.0\n", "go.mod": "module github.com/acme/portal\n\ngo 1.26\n", recordFile: proprietaryRecord})
	get := []string{"api", "--method", "GET", "repos/acme/portal"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: get, Stdout: `{"full_name":"acme/portal","visibility":"public","private":false,"archived":false}`},
		testsupport.GHAnswer{Args: get, Stdout: `{"full_name":"acme/portal","visibility":"private","private":true,"archived":false}`},
	)
	got := runCheck(t, inputs(t, r.Dir), "repository-visibility")
	mustStatus(t, got, "fail")
	mustMention(t, got, "Make the GitHub repository private")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "repository-visibility"), "pass")
}

func TestRepositoryVisibilitySkipsWithoutAGitHubRepository(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "repository-visibility"), "skip")
}
