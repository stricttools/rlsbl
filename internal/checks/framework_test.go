package checks

import (
	"fmt"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// keptChecks is every check rlsbl keeps or adds, by name.
var keptChecks = []string{
	"branch-sync", "changelog-batch-commits", "changelog-batch-entries", "changelog-coverage",
	"changelog-entry", "changelog-format-version", "changelog-hashes", "changelog-orphans",
	"changelog-range", "changelog-schema", "changelog-user-facing", "ci-publish-secrets",
	"confidential-names", "cross-repo-path-sources", "declarations-valid", "dep-floors", "dep-locks",
	"description-consistency", "dev-only-boundary", "dev-overlay-drift", "dunder-version-missing",
	"go-companion-tags", "go-deprecation-published", "go-module-identity", "go-module-major-suffix",
	"go-toolchain-declared", "go-workspace-replace", "go-workspace-require-current", "ldflags-symbol",
	"license-consistency", "license-file", "lifecycle-record-valid", "lock", "member-pytest-config",
	"mixed-tag-schemes", "name-consistency", "nested-member-runner-exclusion",
	"nested-member-upload-contents", "nested-member-uv-sources", "npm-private-mismatch",
	"npm-token-synced", "old-repo-archived", "path-tag-format-go-member", "prepush-changelog-coverage",
	"prepush-gitignore-guard", "prepush-manual-warning", "private-repo-publishing",
	"publish-mode-workflow", "releasable-residue", "repository-visibility", "router-filters-fresh",
	"scaffold-conflicts", "scaffold-gitignore-stale", "scaffold-unreplaced-vars",
	"selfdoc-version-drift", "stash-free", "strictcode", "strictspec-generated-format",
	"target-matrix-fresh", "target-version-readable", "test-suite", "test-suite-workspace",
	"testisolation-floor", "unpublished-refs", "unversioned-boundary", "upload-private-paths",
	"version-consistency", "workspace-ci-router", "workspace-ci-synced", "workspace-stale-entries",
	"workspace-targets", "workspace-unbuildable", "workspace-unregistered",
}

// droppedChecks are checks the rewrite drops or moves to strictcode.
var droppedChecks = []string{
	"private-hook-stale", "requires-services", "layers-violations", "subtree-remote-reachable",
	"wrapper-producer", "lint", "lint-scope-guard", "format", "format-scope-guard", "type-check",
	"type-check-scope-guard", "deps-unused", "deps-undeclared", "deps-stale",
	"deps-runtime-test-only", "deps-dev-in-lib", "dead-modules", "dead-modules-stale",
	"circular-deps", "strictspec-certificate-gate", "library-lint", "dead-workspace-packages",
	"ruff-lint", "root-rlsbl-conflict", "config-schema", "changelog-format-version-gate",
}

func declaredNames(t *testing.T) []string {
	t.Helper()
	decls, err := declared()
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for name := range decls {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func TestTheRegistryHoldsEveryKeptCheckAndNoDroppedOne(t *testing.T) {
	hygiene.Isolate(t)
	want := append([]string(nil), keptChecks...)
	sort.Strings(want)
	if got := declaredNames(t); !slices.Equal(got, want) {
		t.Errorf("checks.toml declares\n  %v\nwant\n  %v", got, want)
	}
	for _, name := range droppedChecks {
		if slices.Contains(declaredNames(t), name) {
			t.Errorf("checks.toml still declares the dropped check %s", name)
		}
	}
}

func TestTheStrictcodeOptionDefaultsToError(t *testing.T) {
	hygiene.Isolate(t)
	reg, err := options.Shipped()
	if err != nil {
		t.Fatal(err)
	}
	decl, ok := reg.Declaration("strictcode")
	if !ok || decl.Default != "error" || decl.Values != "error > warn > off" {
		t.Errorf("rlsbl:strictcode is %+v (declared %v), want default error ranked error > warn > off", decl, ok)
	}
	for _, name := range keptChecks {
		if _, ok := reg.Declaration(name); !ok {
			t.Errorf("the shipped options registry declares no rlsbl:%s", name)
		}
	}
}

func TestEveryImplementedCheckIsDeclaredInItsForm(t *testing.T) {
	hygiene.Isolate(t)
	decls, err := declared()
	if err != nil {
		t.Fatal(err)
	}
	for _, family := range families {
		for _, c := range family() {
			d, ok := decls[c.name]
			switch {
			case !ok:
				t.Errorf("%s is implemented but not declared", c.name)
			case (c.errorRun != nil) == (c.warnRun != nil):
				t.Errorf("%s has %v error and %v warning implementations; it needs one", c.name, c.errorRun != nil, c.warnRun != nil)
			case c.errorRun != nil && d.Severity != "error", c.warnRun != nil && d.Severity != "warn":
				t.Errorf("%s is declared %s, and its implementation is of the other form", c.name, d.Severity)
			}
		}
	}
}

func TestEveryDeclaredCheckIsImplementedOnce(t *testing.T) {
	hygiene.Isolate(t)
	implemented := Implemented()
	for _, name := range declaredNames(t) {
		if !slices.Contains(implemented, name) {
			t.Errorf("%s is declared in checks.toml and not implemented", name)
		}
	}
	for i := 1; i < len(implemented); i++ {
		if implemented[i] == implemented[i-1] {
			t.Errorf("%s is implemented twice", implemented[i])
		}
	}
}

func TestRegisterRegistersEveryImplementedCheck(t *testing.T) {
	hygiene.Isolate(t)
	app := strictcli.NewApp("rlsbl", "0.0.0", "A test application", strictcli.WithChecksEmbed(Registry))
	if _, err := Register(app); err != nil {
		t.Fatal(err)
	}
}

func TestAScopeTokenPackageChecksDoesNotInterpretIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	shipped := Registry
	t.Cleanup(func() { Registry = shipped })
	Registry = []byte("app = \"rlsbl\"\n\n[checks.portal-check]\ndescription = \"A check.\"\nsubject = \"project\"\ntags = []\nseverity = \"error\"\nfast = true\npure = true\nneeds_network = false\ndepends_on = []\nscope = \"workspace:members-only\"\n")
	if _, err := declared(); err == nil || !strings.Contains(err.Error(), `"members-only"`) {
		t.Errorf("an unknown scope token was accepted: %v", err)
	}
}

func TestEveryScopeOfTheRegistryIsInterpreted(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := declared(); err != nil {
		t.Fatal(err)
	}
}

// workspaceRepo is the widget and gadget workspace, each member a go
// project at 1.0.0 with its releasable's version file.
func workspaceRepo(t *testing.T) *testsupport.Repo {
	t.Helper()
	return newRepo(t, workspaceWidgetGadget, map[string]string{
		"widget/go.mod":  "module github.com/acme/repo/widget\n\ngo 1.26\n",
		"widget/VERSION": "1.0.0\n",
		"gadget/go.mod":  "module github.com/acme/repo/gadget\n\ngo 1.26\n",
		"gadget/VERSION": "1.0.0\n",
		".strictmetadata/releases/widget/version": "1.0.0\n",
		".strictmetadata/releases/gadget/version": "1.0.0\n",
	})
}

func TestACheckForOneReleasableRefusesAtAWorkspaceRoot(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	got := runCheck(t, inputs(t, r.Dir), "version-consistency")
	if got.Unanswered == "" || !strings.Contains(got.Unanswered, "workspace root") || !strings.Contains(got.Unanswered, "gadget, widget") {
		t.Fatalf("want the workspace-root refusal naming the releasables, got %s", got)
	}
	// The fix the refusal names: run from a directory of a member.
	mustStatus(t, runCheck(t, inputs(t, r.Path("widget")), "version-consistency"), "pass")
}

func TestARepositoryCheckAnswersAtAWorkspaceRoot(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "prepush-gitignore-guard"), "pass")
}

func TestANamedReleasableAnswersAtAWorkspaceRoot(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	in := inputs(t, r.Dir)
	in.Releasable = "gadget"
	got := runCheck(t, in, "version-consistency")
	mustStatus(t, got, "pass")
	mustMention(t, got, `"gadget"`)
}

func TestNamingAnUndeclaredReleasableIsRefusedNamingTheDeclaredOnes(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	in := inputs(t, r.Dir)
	in.Releasable = "gizmo"
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly}, func(ctx *strictcli.Context) error {
		_, err := NewContext(ctx.Effects(), in)
		return err
	})
	if res.ExitCode == 0 || !strings.Contains(res.Stderr, `"gizmo"`) || !strings.Contains(res.Stderr, "gadget, widget") {
		t.Errorf("an undeclared releasable was accepted, or the refusal names no declared one: %s", res.Stderr)
	}
}

func TestNamingAnotherReleasableFromAMemberIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	in := inputs(t, r.Path("widget"))
	in.Releasable = "gadget"
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly}, func(ctx *strictcli.Context) error {
		_, err := NewContext(ctx.Effects(), in)
		return err
	})
	if res.ExitCode == 0 || !strings.Contains(res.Stderr, `"widget"`) {
		t.Errorf("a releasable other than the member's was accepted: %s", res.Stderr)
	}
}

func TestScopeTokensNarrowTheMembersAndSkip(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	parse := func(text string) *workspace.Workspace {
		d, err := declarations.Parse([]byte(text))
		if err != nil {
			t.Fatal(err)
		}
		w, err := workspace.New(root, d)
		if err != nil {
			t.Fatal(err)
		}
		return w
	}
	ws := parse(workspaceWidgetGadget)
	c := &Context{ws: ws, members: ws.Members(), unselected: []string{"gadget", "widget"}}
	narrowed := *c
	if skip := narrowed.applyToken(tokenNonDevOnly); skip != "" {
		t.Fatal(skip)
	}
	var names []string
	for _, m := range narrowed.members {
		names = append(names, m.Name)
	}
	if !slices.Equal(names, []string{"widget", "gadget"}) {
		t.Errorf("non_dev_only kept %v", names)
	}
	if narrowed.unselected == nil {
		t.Error("a narrowing token cleared the workspace-root marker")
	}
	if skip := narrowed.applyToken(tokenWorkspace); skip != "" || narrowed.unselected != nil {
		t.Errorf("workspace in a workspace: skip %q, marker %v", skip, narrowed.unselected)
	}
	standalone := parse(fmt.Sprintf(standalonePortal, "none"))
	s := &Context{ws: standalone, members: standalone.Members()}
	if skip := s.applyToken(tokenWorkspace); !strings.Contains(skip, "not a workspace") {
		t.Errorf("workspace in a standalone repository: %q", skip)
	}
	if skip := s.applyToken(tokenPush); !strings.Contains(skip, "not in a push") {
		t.Errorf("push outside a push: %q", skip)
	}
	s.in.PushLines = []string{}
	if skip := s.applyToken(tokenPush); skip != "" {
		t.Errorf("push inside a push: %q", skip)
	}
}

func TestUnreadableDeclarationsAreReportedByDeclarationsValidAndRefusedElsewhere(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	good := fmt.Sprintf(standalonePortal, "none")
	r.Write(".strictmetadata/releasables/releasables.toml", good+"surprise = true\n")
	got := runCheck(t, inputs(t, r.Dir), "declarations-valid")
	mustStatus(t, got, "fail")
	mustMention(t, got, "surprise")
	refused := runCheck(t, inputs(t, r.Dir), "version-consistency")
	if !strings.Contains(refused.Unanswered, "declarations-valid") {
		t.Errorf("a check over unreadable declarations did not refuse naming declarations-valid: %s", refused)
	}
	// The fix: correct the declarations.
	r.Write(".strictmetadata/releasables/releasables.toml", good)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "declarations-valid"), "pass")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "version-consistency"), "pass")
}

func TestTheContextAnswersForTheMemberItStandsIn(t *testing.T) {
	hygiene.Isolate(t)
	r := workspaceRepo(t)
	var member, releasable string
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly}, func(ctx *strictcli.Context) error {
		c, err := NewContext(ctx.Effects(), inputs(t, filepath.Join(r.Dir, "gadget")))
		if err != nil {
			return err
		}
		member = c.Member().Name
		rel, _ := c.Releasable()
		releasable = rel.Name
		return nil
	})
	if member != "gadget" || releasable != "gadget" {
		t.Errorf("the context stands in %q of %q", member, releasable)
	}
}
