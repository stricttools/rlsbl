package cli

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestTheChangelogCommandsAreClassified(t *testing.T) {
	hygiene.Isolate(t)
	group, ok := appWith(t, testsupport.NewFakeHTTP(t)).Groups()["changelog"]
	if !ok {
		t.Fatal("no changelog group is registered")
	}
	for _, name := range []string{"add", "generate", "amend", "edit", "remove", "remap"} {
		cmd, ok := group.Commands[name]
		if !ok || cmd.Effect != strictcli.EffectMutating || cmd.Consequential {
			t.Errorf("changelog %s: registered %v: %+v", name, ok, cmd)
		}
	}
}

// ghReleaseEdit are the gh answers `release edit 0.4.0` needs for
// acme/portal, the rewrite answering with exit.
func ghReleaseEdit(exit int) []testsupport.GHAnswer {
	return []testsupport.GHAnswer{
		{Args: []string{"auth", "status", "--hostname", "github.com"}},
		{Args: []string{"release", "view", "v0.4.0", "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}, Stdout: "v0.4.0\n"},
		{Args: []string{"release", "edit", "v0.4.0", "--repo", "acme/portal", "--notes-file", "-", "--title", "v0.4.0", "--prerelease=false"}, Exit: exit, Stderr: "HTTP 502\n"},
	}
}

func TestAmendRewritesTheReleaseAndAFailedRewriteNamesReleaseEdit(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	repo.Git("remote", "add", "origin", "https://github.com/acme/portal.git")
	testsupport.FakeSafegit(t)
	more := repo.CommitFile("b.txt", "b\n", "more")
	testsupport.FakeGH(t, ghReleaseEdit(1)...)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"changelog", "amend", "--version", "0.4.0", "--commits", more, "--description", "Also this", "--type", "fix"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "its GitHub Release was not rewritten") || !strings.Contains(r.Stderr, "`rlsbl release edit 0.4.0`") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if !strings.Contains(repo.Git("log", "-1", "--format=%s"), "changelog: amend 0.4.0: Also this") {
		t.Fatal("the amend was not committed before the rewrite")
	}

	gh := testsupport.FakeGH(t, ghReleaseEdit(0)...)
	r = app.Test([]string{"release", "edit", "0.4.0"})
	if r.ExitCode != 0 {
		t.Fatalf("the named fix failed: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	var body string
	for _, c := range gh.Calls() {
		if len(c.Args) > 1 && c.Args[0] == "release" && c.Args[1] == "edit" {
			body = c.Stdin
		}
	}
	if !strings.Contains(body, "Also this") {
		t.Fatalf("the rewritten notes lack the amended entry: %q", body)
	}
}

func TestAmendDeclaresNoIDAndEditTakesIt(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	testsupport.FakeSafegit(t)
	more := repo.CommitFile("b.txt", "b\n", "more")
	app := appWith(t, testsupport.NewFakeHTTP(t))
	id := strings.Repeat("0", 47) + "1"
	r := app.Test([]string{"changelog", "amend", "--version", "0.4.0", "--commits", more, "--id", id, "--no-user-facing"})
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, "--id") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"changelog", "add", "--commits", more, "--no-user-facing"})
	if r.ExitCode != 0 {
		t.Fatalf("add: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	added := strings.TrimSpace(repo.Git("show", "HEAD:.strictmetadata/changelog/portal/unreleased.jsonl"))
	_, rest, _ := strings.Cut(added, `"id":"`)
	newID, _, _ := strings.Cut(rest, `"`)
	r = app.Test([]string{"changelog", "edit", "--id", newID, "--user-facing", "--description", "Now public", "--type", "feature"})
	if r.ExitCode != 0 {
		t.Fatalf("edit: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if text := repo.Git("show", "HEAD:.strictmetadata/changelog/portal/unreleased.jsonl"); !strings.Contains(text, `"user_facing":true,"description":"Now public","type":"feature"`) {
		t.Fatalf("after the edit: %s", text)
	}
	r = app.Test([]string{"changelog", "edit", "--id", newID, "--unset-description", "--no-user-facing"})
	if r.ExitCode != 0 {
		t.Fatalf("clearing: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if text := repo.Git("show", "HEAD:.strictmetadata/changelog/portal/unreleased.jsonl"); strings.Contains(text, "Now public") {
		t.Fatalf("the description was not cleared: %s", text)
	}
}

func TestTheSelectorsAndSourcesAreDeclared(t *testing.T) {
	hygiene.Isolate(t)
	releaseCommandsProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	id := strings.Repeat("0", 47) + "1"
	for _, argv := range [][]string{
		{"changelog", "remove", "--id", id, "--commits", "abc1234"},
		{"changelog", "remove"},
		{"changelog", "edit", "--id", id},
		{"changelog", "remap"},
	} {
		if r := app.Test(argv); r.ExitCode == 0 {
			t.Errorf("%q was accepted:\n%s", argv, r.Stdout)
		}
	}
	if r := app.Test([]string{"changelog", "add", "--commits", "", "--no-user-facing"}); r.ExitCode != 1 || !strings.Contains(r.Stderr, "--commits must not be empty") {
		t.Errorf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestRemapReadsAMapFileRelativeToTheWorkingDirectory(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	testsupport.FakeSafegit(t)
	old := repo.Git("rev-parse", "HEAD~1")
	next := strings.Repeat("9", 40)
	repo.Write("map.txt", old+" "+next+"\n")
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"changelog", "remap", "--map-file", "map.txt"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if text := repo.Git("show", "HEAD:.strictmetadata/changelog/portal/0.4.0.jsonl"); !strings.Contains(text, next) {
		t.Fatalf("the released file was not remapped: %s", text)
	}
}

func TestChangelogGenerateUnderDryRunRendersAndWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"changelog", "generate", "--dry-run"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "## 0.4.0") || !strings.Contains(r.Stdout, "Would commit CHANGELOG.md") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Fatalf("a dry run changed the tree:\n%s", status)
	}
}

func TestChangelogRemoveRefusesAnIDNoEntryHas(t *testing.T) {
	hygiene.Isolate(t)
	releaseCommandsProject(t)
	id := strings.Repeat("0", 48) + "99"
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"changelog", "remove", "--id", id})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, `no changelog entry of "portal" has the id `+id) {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}
