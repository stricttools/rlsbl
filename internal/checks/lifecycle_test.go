package checks

import (
	"fmt"
	"strings"
	"testing"

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

func TestCodenamesOfAPublicRepositoryAreRefused(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{recordFile: strings.Replace(publicRecord, "format_version = 1\n", "format_version = 1\ncodenames = [\"gizmo\"]\n", 1)})
	got := runCheck(t, inputs(t, r.Dir), "lifecycle-record-valid")
	mustStatus(t, got, "fail")
	mustMention(t, got, "codenames")
	// The fix the refusal names: remove the codenames field.
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

// confidentialIndex holds the names of another, confidential repository.
const confidentialIndex = "format_version = 1\n\n[[repositories]]\nsubjects = [\"secret\"]\nnames = [\"gizmo\"]\n"

// pushedToOrigin gives r a bare origin holding its main branch as it is now,
// with the remote-tracking branch a fetch leaves.
func pushedToOrigin(r *testsupport.Repo) {
	r.AddBareRemote("origin")
	r.Git("push", "-q", "origin", "main")
	r.Git("fetch", "-q", "origin")
}

func TestAConfidentialNameInATrackedFileFailsUntilItIsRemoved(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"README.md": "portal talks to Gizmo.\n"})
	pushedToOrigin(r)
	in := inputs(t, r.Dir)
	testsupport.WriteFile(t, in.IndexPath, confidentialIndex)
	got := runCheck(t, in, "confidential-names")
	mustStatus(t, got, "fail")
	mustMention(t, got, "README.md", `"gizmo"`)
	r.Write("README.md", "portal talks to a server.\n")
	mustStatus(t, runCheck(t, in, "confidential-names"), "pass")
}

func TestAConfidentialNameAddedAndRemovedInTheUnpushedRangeFailsUntilTheHistoryIsRewritten(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	pushedToOrigin(r)
	pushed := r.Head()
	added := r.CommitFile("notes/plan.md", "talk to Gizmo first\n", "Plan the work")
	r.CommitFile("notes/plan.md", "talk to the server first\n", "Reword the plan")
	in := inputs(t, r.Dir)
	testsupport.WriteFile(t, in.IndexPath, confidentialIndex)
	got := runCheck(t, in, "confidential-names")
	mustStatus(t, got, "fail")
	mustMention(t, got, "commit "+added+", notes/plan.md, line 1, column 9", `"gizmo"`, "origin/main..HEAD")
	// The fix the refusal names: rewrite the unpushed commits so that none
	// carries the name.
	r.Git("reset", "-q", "--hard", pushed)
	r.CommitFile("notes/plan.md", "talk to the server first\n", "Plan the work")
	mustStatus(t, runCheck(t, in, "confidential-names"), "pass")
}

func TestAConfidentialNameInAnUnpushedCommitMessageFails(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	pushedToOrigin(r)
	named := r.CommitFile("main.go", "package main\n", "Serve what gizmo asks for")
	in := inputs(t, r.Dir)
	testsupport.WriteFile(t, in.IndexPath, confidentialIndex)
	got := runCheck(t, in, "confidential-names")
	mustStatus(t, got, "fail")
	mustMention(t, got, "commit "+named+", its message", `"gizmo"`)
	r.Git("commit", "-q", "--amend", "-m", "Serve what the server asks for")
	mustStatus(t, runCheck(t, in, "confidential-names"), "pass")
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
	in := inputs(t, r.Dir)
	testsupport.WriteFile(t, in.IndexPath, confidentialIndex)
	mustStatus(t, runCheck(t, in, "confidential-names"), "pass")
	named := r.CommitFile("notes.md", "ask gizmo\n", "Take notes")
	got := runCheck(t, in, "confidential-names")
	mustStatus(t, got, "fail")
	mustMention(t, got, "commit "+named+", notes.md")
}

func TestWithNoRemoteTrackingBranchTheWholeHistoryIsScanned(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	named := r.CommitFile("notes.md", "gizmo\n", "Take notes")
	r.CommitFile("notes.md", "nothing\n", "Clear the notes")
	in := inputs(t, r.Dir)
	testsupport.WriteFile(t, in.IndexPath, confidentialIndex)
	got := runCheck(t, in, "confidential-names")
	mustStatus(t, got, "fail")
	mustMention(t, got, "commit "+named+", notes.md", "the history of HEAD")
}

func TestAConfidentialRepositoryCarriesItsOwnNames(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{recordFile: proprietaryRecord, "README.md": "gizmo\n"})
	in := inputs(t, r.Dir)
	testsupport.WriteFile(t, in.IndexPath, confidentialIndex)
	mustStatus(t, runCheck(t, in, "confidential-names"), "skip")
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
