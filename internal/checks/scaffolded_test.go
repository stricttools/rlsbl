package checks

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/workflows"
)

const publishWorkflow = "name: Publish\non:\n  release:\n    types: [published]\njobs:\n  publish:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm publish\n"

func TestAPublishWorkflowOfAProjectPublishingNothingFailsUntilDeleted(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{".github/workflows/release.yml": publishWorkflow})
	got := runCheck(t, inputs(t, r.Dir), "publish-mode-workflow")
	mustStatus(t, got, "fail")
	mustMention(t, got, ".github/workflows/release.yml", "saferm delete", `publish_mode = "ci"`)
	// The fix the finding names: delete the workflow.
	if err := os.Remove(r.Path(".github/workflows/release.yml")); err != nil {
		t.Fatal(err)
	}
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "publish-mode-workflow"), "pass")
}

func TestAPublishWorkflowIsWhereAPublishingProjectKeepsIt(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "ci", map[string]string{".github/workflows/publish.yml": publishWorkflow})
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "publish-mode-workflow"), "pass")
}

func TestTheGeneratedPublishRouterIsNoMembersOwnPublishWorkflow(t *testing.T) {
	hygiene.Isolate(t)
	declared := strings.Replace(workspaceWidgetGadget, "dev_only = true\nreleasable = false", "releasable = \"widget\"", 1)
	r := newRepo(t, declared, map[string]string{
		"widget/go.mod": "module github.com/acme/repo/widget\n\ngo 1.26\n",
		"gadget/go.mod": "module github.com/acme/repo/gadget\n\ngo 1.26\n",
		".strictmetadata/releases/widget/version": "1.0.0\n",
		".strictmetadata/releases/gadget/version": "1.0.0\n",
		workflows.PublishPath:                     workflows.Header + "\n" + publishWorkflow,
		"docs/README.md":                          "docs\n",
	})
	// docs/ lies in the root member's territory, which publishes nothing.
	mustStatus(t, runCheck(t, inputs(t, r.Path("docs")), "publish-mode-workflow"), "pass")
}

const conflicted = "name: CI\n<<<<<<< ours\non: push\n=======\non: [push, pull_request]\n>>>>>>> theirs\njobs: {}\n"

func TestAMergeConflictInAScaffoldedFileFailsUntilResolved(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{".github/workflows/ci.yml": conflicted})
	got := runCheck(t, inputs(t, r.Dir), "scaffold-conflicts")
	mustStatus(t, got, "fail")
	mustMention(t, got, ".github/workflows/ci.yml: lines 2-6", "resolve each marked region")
	r.Write(".github/workflows/ci.yml", "name: CI\non: [push, pull_request]\njobs: {}\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "scaffold-conflicts"), "pass")
}

func TestAManagedFileTheStateRecordsIsReadForConflicts(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"scripts/check.sh": conflicted})
	r.Write(".strictmetadata/.scaffold-state/scaffold-state.toml", scaffold.RenderState(scaffold.State{RlsblVersion: "0.132.0", Files: map[string]string{"scripts/check.sh": strings.Repeat("a", 64)}}))
	got := runCheck(t, inputs(t, r.Dir), "scaffold-conflicts")
	mustStatus(t, got, "fail")
	mustMention(t, got, "scripts/check.sh")
	r.Write("scripts/check.sh", "#!/bin/sh\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "scaffold-conflicts"), "pass")
}

func TestAnUnreplacedPlaceholderFailsUntilScaffoldFinishesTheFile(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		".github/workflows/ci.yml": "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {{action \"actions/checkout\"}}\n      - run: go test {{goPackages}} # ${{ github.sha }}\n",
		".goreleaser.yml":          goreleaserLdflags + "      - -X main.commit={{ .Commit }}\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "scaffold-unreplaced-vars")
	mustStatus(t, got, "fail")
	mustMention(t, got, ".github/workflows/ci.yml", `{{action "actions/checkout"}}`, "{{goPackages}}", "rlsbl scaffold")
	if strings.Contains(got.texts(), ".goreleaser.yml") || strings.Contains(got.texts(), "github.sha") {
		t.Errorf("a goreleaser template or a GitHub expression was read as a placeholder: %s", got)
	}
	// What a finished scaffold writes.
	r.Write(".github/workflows/ci.yml", "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v5\n      - run: go test ./... # ${{ github.sha }}\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "scaffold-unreplaced-vars"), "pass")
}

func TestScaffoldPlaceholdersAfterADollarAreGitHubExpressions(t *testing.T) {
	hygiene.Isolate(t)
	got := scaffoldPlaceholders("a ${{name}} b {{name}}{{other}} {{#if x}}c{{/if}} {{ spaced }}")
	want := []string{"{{#if x}}", "{{/if}}", "{{name}}", "{{other}}"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("placeholders %v, want %v", got, want)
	}
}

func TestAMembersGitignoreLackingScaffoldsLinesWarnsUntilScaffoldMergesThem(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	entries, err := scaffold.GitignoreEntries()
	if err != nil {
		t.Fatal(err)
	}
	full := strings.Join(entries, "\n") + "\n"
	r.Write("gadget/.gitignore", full)
	r.Write("widget/.gitignore", "node_modules/\n")
	got := runCheck(t, inputs(t, r.Dir), "scaffold-gitignore-stale")
	mustStatus(t, got, "warn")
	mustMention(t, got, "widget/.gitignore lacks", "*.local-only", "rlsbl scaffold")
	if strings.Contains(got.texts(), "gadget") || strings.Contains(got.texts(), "root") {
		t.Errorf("a complete .gitignore or the root member was reported: %s", got)
	}
	// What `rlsbl scaffold` merges in.
	r.Write("widget/.gitignore", "node_modules/\n\n"+full)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "scaffold-gitignore-stale"), "pass")
}
