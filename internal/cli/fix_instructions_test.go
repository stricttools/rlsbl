package cli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// checkOutcome runs the check name through the application in the working
// directory and returns its result: status, message, and problems.
func checkOutcome(t *testing.T, app *strictcli.App, name string) map[string]any {
	t.Helper()
	r := app.Test([]string{"check", "--name", name, "--json"})
	var envelope struct {
		Payload []any `json:"payload"`
	}
	if err := json.Unmarshal([]byte(r.Stdout), &envelope); err != nil {
		t.Fatalf("not JSON: %v\n%s%s", err, r.Stdout, r.Stderr)
	}
	for _, res := range envelope.Payload {
		if m, ok := res.(map[string]any); ok && m["name"] == name {
			return m
		}
	}
	t.Fatalf("the check %s did not run: exit %d\n%s%s", name, r.ExitCode, r.Stdout, r.Stderr)
	return nil
}

// mustCheckStatus fails the test unless the check ended with status, and
// returns its text.
func mustCheckStatus(t *testing.T, app *strictcli.App, name, status string) string {
	t.Helper()
	got := checkOutcome(t, app, name)
	text := strings.TrimSpace(strings.Join([]string{asText(got["message"]), asText(got["problems"])}, "\n"))
	if got["status"] != status {
		t.Fatalf("%s ended %v, want %s:\n%s", name, got["status"], status, text)
	}
	return text
}

func asText(v any) string {
	switch v := v.(type) {
	case string:
		return v
	case []any:
		var parts []string
		for _, p := range v {
			parts = append(parts, asText(p))
		}
		return strings.Join(parts, "\n")
	case map[string]any:
		var parts []string
		for _, p := range v {
			parts = append(parts, asText(p))
		}
		return strings.Join(parts, "\n")
	}
	return ""
}

func TestTheCleanupTheResidueCheckNamesClearsAStandaloneRepositorysOldLayout(t *testing.T) {
	hygiene.Isolate(t)
	repo := scaffoldGoProject(t)
	repo.CommitFile(".rlsbl/config.json", "{}\n", "the old layout")
	testsupport.FakeDeletingSaferm(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	text := mustCheckStatus(t, app, "releasable-residue", "fail")
	if !strings.Contains(text, "`rlsbl monorepo cleanup` removes") {
		t.Fatalf("the finding names no cleanup:\n%s", text)
	}
	if r := app.Test([]string{"monorepo", "cleanup", "--auto-commit"}); r.ExitCode != 0 {
		t.Fatalf("the cleanup the finding names: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	mustCheckStatus(t, app, "releasable-residue", "pass")
	if tracked := repo.Git("ls-files", ".rlsbl"); tracked != "" {
		t.Errorf("the cleanup left the old layout tracked:\n%s", tracked)
	}
}

func TestTheCleanupTheResidueCheckNamesClearsAWorkspacesResidue(t *testing.T) {
	hygiene.Isolate(t)
	repo := monorepoFixture(t)
	repo.CommitFile("packages/widget/CHANGELOG.md", "# Changelog\n", "the member's own changelog")
	repo.CommitFile("packages/widget/.rlsbl/config.json", "{}\n", "the old layout")
	testsupport.FakeDeletingSaferm(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	text := mustCheckStatus(t, app, "releasable-residue", "fail")
	for _, want := range []string{"packages/widget/CHANGELOG.md", "packages/widget/.rlsbl/", "`rlsbl monorepo cleanup` removes"} {
		if !strings.Contains(text, want) {
			t.Fatalf("the finding does not name %q:\n%s", want, text)
		}
	}
	if r := app.Test([]string{"monorepo", "cleanup", "--auto-commit"}); r.ExitCode != 0 {
		t.Fatalf("the cleanup the finding names: exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	mustCheckStatus(t, app, "releasable-residue", "pass")
}

func TestMovingTheReleaseStateTheResidueCheckKeepsWhereItSaysClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	repo := scaffoldGoProject(t)
	repo.CommitFile(".strictmetadata/releases/old/v0.1.0.toml", "", "an old subject's archive")
	repo.CommitFile(".strictmetadata/changelog/old/0.1.0.jsonl", "", "an old subject's changelog")
	app := appWith(t, testsupport.NewFakeHTTP(t))
	text := mustCheckStatus(t, app, "releasable-residue", "fail")
	for _, want := range []string{
		".strictmetadata/releases/old/: ", "keeps it as .strictmetadata/retired-release-histories/old/releases/",
		".strictmetadata/changelog/old/: ", "keeps it as .strictmetadata/retired-release-histories/old/changelog/",
		"`rlsbl monorepo cleanup` keeps it: move it by hand where this item says",
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("the finding does not say %q:\n%s", want, text)
		}
	}
	if err := os.MkdirAll(filepath.Join(repo.Dir, ".strictmetadata/retired-release-histories/old"), 0o755); err != nil {
		t.Fatal(err)
	}
	repo.Git("mv", ".strictmetadata/releases/old", ".strictmetadata/retired-release-histories/old/releases")
	repo.Git("mv", ".strictmetadata/changelog/old", ".strictmetadata/retired-release-histories/old/changelog")
	mustCheckStatus(t, app, "releasable-residue", "pass")
}
