package cli

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestAdoptTagsIsMutatingAndConsequential(t *testing.T) {
	hygiene.Isolate(t)
	cmd, ok := appWith(t, testsupport.NewFakeHTTP(t)).Groups()["upstream"].Commands["adopt-tags"]
	if !ok || cmd.Effect != strictcli.EffectMutating || !cmd.Consequential {
		t.Fatalf("registered %v: %+v", ok, cmd)
	}
}

func TestAdoptTagsThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	up := testsupport.NewRepo(t)
	up.CommitFile("a.txt", "a\n", "upstream: a")
	up.Git("tag", "v0.1.0")
	fork := up.Clone()
	fork.Git("config", "url.file://"+up.Dir+".insteadOf", "https://github.com/up/proj")
	bare := fork.AddBareRemote("mirror")
	fork.Git("remote", "remove", "origin")
	fork.Git("remote", "add", "origin", "file://"+bare)
	fork.Git("push", "-q", "origin", "HEAD:refs/heads/main", "refs/tags/v0.1.0")
	fork.Write(".strictmetadata/upstream/upstream.toml", "format_version = 1\nhost = \"github.com\"\nowner = \"up\"\nrepo = \"proj\"\nbranch = \"main\"\n")
	hygiene.Chdir(t, fork.Path("."))
	app := appWith(t, testsupport.NewFakeHTTP(t))

	r := app.Test([]string{"upstream", "adopt-tags", "--dry-run"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "v0.1.0: inherited") || !strings.Contains(r.Stdout, "Dry run: 1 inherited tag(s) would move") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	r = app.Test([]string{"upstream", "adopt-tags", "--approve-consequential"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	kept := "refs/tags-of/github.com/up/proj/v0.1.0"
	if _, ok := testsupport.Refs(t, filepath.Clean(bare))[kept]; !ok {
		t.Fatalf("origin holds no %s", kept)
	}
	if _, ok := testsupport.Refs(t, fork.Dir)["refs/tags/v0.1.0"]; ok {
		t.Fatal("the inherited tag is still in refs/tags")
	}
}
