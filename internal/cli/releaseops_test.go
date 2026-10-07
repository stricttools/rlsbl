package cli

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestTheOperationsOnPastReleasesAreClassified(t *testing.T) {
	hygiene.Isolate(t)
	group, ok := appWith(t, testsupport.NewFakeHTTP(t)).Groups()["release"]
	if !ok {
		t.Fatal("no release group is registered")
	}
	for name, consequential := range map[string]bool{
		"edit":      false,
		"retry":     true,
		"undo":      true,
		"abandon":   true,
		"deprecate": true,
		"yank":      true,
	} {
		cmd, ok := group.Commands[name]
		if !ok || cmd.Effect != strictcli.EffectMutating || cmd.Consequential != consequential {
			t.Errorf("release %s: registered %v: %+v", name, ok, cmd)
		}
	}
}

func TestReleaseRetryRequiresWatchOrNoWatch(t *testing.T) {
	hygiene.Isolate(t)
	releaseCommandsProject(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"release", "retry", "--approve-consequential"})
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, "watch") {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stderr)
	}
}

func TestEmptyArgumentsAreRefused(t *testing.T) {
	hygiene.Isolate(t)
	releaseCommandsProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	for _, argv := range [][]string{
		{"release", "edit", ""},
		{"release", "undo", "--version", "", "--approve-consequential"},
		{"release", "undo", "--target", "", "--approve-consequential"},
		{"release", "deprecate", "", "--approve-consequential"},
		{"release", "yank", "0.4.0", "--reason", "", "--approve-consequential"},
		{"release", "deprecate", "0.4.0", "--use", "", "--approve-consequential"},
	} {
		r := app.Test(argv)
		if r.ExitCode != 1 || !strings.Contains(r.Stderr, "must not be empty") {
			t.Errorf("%q: exit %d: %s", argv, r.ExitCode, r.Stderr)
		}
	}
}

func TestTheOperationsThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	repo.Git("remote", "add", "origin", "https://github.com/acme/portal.git")
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: []string{"auth", "status", "--hostname", "github.com"}},
		testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/acme/portal"}, Stdout: `{"full_name":"acme/portal","visibility":"public","private":false,"archived":false,"permissions":{"push":true}}`},
		testsupport.GHAnswer{Args: []string{"release", "view", "v0.4.0", "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}, Stdout: "v0.4.0\n"})
	app := appWith(t, testsupport.NewFakeHTTP(t))
	for _, c := range []struct {
		argv []string
		want string
	}{
		{[]string{"release", "edit", "0.9.0"}, "has no release archive"},
		{[]string{"release", "deprecate", "v0.4.0", "--approve-consequential"}, `carries a leading "v"`},
		{[]string{"release", "abandon", "--approve-consequential"}, "there is nothing to abandon"},
		{[]string{"release", "undo", "--target", "npm", "--approve-consequential"}, "--target selects nothing"},
		{[]string{"release", "retry", "--no-watch", "--approve-consequential"}, "holds no workflow with a workflow_dispatch trigger"},
	} {
		r := app.Test(c.argv)
		if r.ExitCode != 1 || !strings.Contains(r.Stderr, c.want) {
			t.Errorf("%q: exit %d:\n%s%s", c.argv, r.ExitCode, r.Stdout, r.Stderr)
		}
	}
}

func TestYankThroughTheApplicationRefusesAProprietaryReleasable(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	repo.Write(".strictmetadata/lifecycle-and-license/lifecycle-and-license.toml", "format_version = 1\n\n[[licenses]]\nsubject = \"portal\"\nlicense = \"proprietary\"\nfrom = 2026-01-01\nreason = \"the server's logic\"\n")
	repo.Commit("classify portal", ".strictmetadata/lifecycle-and-license/lifecycle-and-license.toml")
	fake := testsupport.NewFakeHTTP(t)
	r := appWith(t, fake).Test([]string{"release", "yank", "0.4.0", "--reason", "Broken", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "no registry write is made for a proprietary releasable") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if len(fake.Requests()) != 0 {
		t.Fatalf("a refused yank asked a registry: %q", fake.URLs())
	}
}
