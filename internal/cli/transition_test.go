package cli

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestTheTransitionCommandsAreClassified(t *testing.T) {
	hygiene.Isolate(t)
	group, ok := appWith(t, testsupport.NewFakeHTTP(t)).Groups()["transition"]
	if !ok {
		t.Fatal("no transition group is registered")
	}
	for name, want := range map[string]struct {
		effect        string
		consequential bool
	}{
		"show":                {strictcli.EffectReadOnly, false},
		"lifecycle":           {strictcli.EffectMutating, true},
		"license":             {strictcli.EffectMutating, true},
		"identity":            {strictcli.EffectMutating, true},
		"unversioned-tag":     {strictcli.EffectMutating, true},
		"classify":            {strictcli.EffectMutating, true},
		"declassify":          {strictcli.EffectMutating, true},
		"init-minimal-record": {strictcli.EffectMutating, false},
	} {
		cmd, ok := group.Commands[name]
		if !ok || cmd.Effect != want.effect || cmd.Consequential != want.consequential {
			t.Errorf("transition %s: registered %v: %+v", name, ok, cmd)
		}
	}
}

func TestInitMinimalRecordAndShowThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("README.md", "readme\n", "start")
	hygiene.Chdir(t, repo.Dir)
	testsupport.FakeSafegit(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	if r := app.Test([]string{"transition", "init-minimal-record"}); r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	data, err := os.ReadFile(repo.Path(lifecycle.RecordFile))
	if err != nil || string(data) != "format_version = 1\n" {
		t.Fatalf("the record: %q, %v", data, err)
	}
	// No origin and no declared GitHub repository: the visibility verdict
	// is unknown, with the reason, and the command still reports.
	r := app.Test([]string{"transition", "show", "--json"})
	if r.ExitCode != 0 {
		t.Fatalf("show: exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	p := jsonPayload(t, r)
	if p["present"] != true || p["confidential"] != false {
		t.Fatalf("payload %v", p)
	}
	verdicts, _ := p["verdicts"].([]any)
	unknown := false
	for _, v := range verdicts {
		if m, ok := v.(map[string]any); ok && m["rule"] == "proprietary-requires-private" && m["verdict"] == "unknown" {
			unknown = true
		}
	}
	if !unknown {
		t.Fatalf("verdicts %v", verdicts)
	}
}

func TestDeclassifyReadsItsLicenses(t *testing.T) {
	hygiene.Isolate(t)
	releaseCommandsProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	for argv, want := range map[string]string{
		"portal --reason open":                           "is not <releasable>=<SPDX identifier>",
		"portal=MIT --license portal=0BSD --reason open": "names \"portal\" twice",
	} {
		args := append([]string{"transition", "declassify", "--approve-consequential", "--license"}, strings.Fields(argv)...)
		if r := app.Test(args); r.ExitCode != 1 || !strings.Contains(r.Stderr, want) {
			t.Errorf("%q: exit %d: %s", args, r.ExitCode, r.Stderr)
		}
	}
}

func TestIdentityNeedsFromWithUntil(t *testing.T) {
	hygiene.Isolate(t)
	releaseCommandsProject(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"transition", "identity", "--approve-consequential", "--subject", "gizmo", "--facet", "go-module-path",
		"--value", "example.com/gizmo", "--until", "2025-06-01", "--reason", "dead"})
	if r.ExitCode == 0 {
		t.Fatalf("--until without --from was accepted:\n%s", r.Stdout)
	}
}

// Each period and declaration command previews the record it would write
// and the commit it would make.
func TestTheTransitionDeclarationsPreviewTheirRecord(t *testing.T) {
	hygiene.Isolate(t)
	for argv, want := range map[string]string{
		"transition lifecycle --subject portal --status on-hold --reason paused":                                 "Would record portal as on-hold from ",
		"transition license --subject portal --license MIT --reason relicensed":                                  "Would record portal as licensed MIT from ",
		"transition identity --subject portal --facet package-name --registry npm --value portal --reason named": `Would record portal's package-name identity "portal" from `,
		"transition unversioned-tag --tag nightly --reason builds":                                               "Would record nightly as a tag that releases no version.",
	} {
		t.Run(argv, func(t *testing.T) {
			repo := releaseCommandsProject(t)
			r := appWith(t, testsupport.NewFakeHTTP(t)).Test(append(strings.Fields(argv), "--dry-run"))
			if r.ExitCode != 0 || !strings.Contains(r.Stdout, want) || !strings.Contains(r.Stdout, "Would commit .strictmetadata/lifecycle-and-license/lifecycle-and-license.toml") {
				t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
			}
			if status := repo.Git("status", "--porcelain"); status != "" {
				t.Fatalf("a dry run changed the tree:\n%s", status)
			}
		})
	}
}

// Classifying a releasable proprietary needs to know the repository's
// visibility, so a checkout naming no GitHub repository is refused.
func TestClassifyRefusesACheckoutNamingNoGitHubRepository(t *testing.T) {
	hygiene.Isolate(t)
	releaseCommandsProject(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"transition", "classify", "--subject", "portal", "--reason", "closed", "--dry-run"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "no GitHub repository is declared (github_repository)") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}
