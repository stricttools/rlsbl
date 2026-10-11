package historyrewrite_test

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/historyrewrite"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A rewrite that moved a retracted version's tag writes no Release for it:
// the retraction deleted the one it had.
func TestARewriteWritesNoReleaseForARetractedVersion(t *testing.T) {
	hygiene.Isolate(t)
	f := newReleased(t, true)
	tree := f.repo.Git("rev-parse", f.h.R+"^{tree}")
	f.repo.Write(archivePath, strings.Replace(archiveText(f.h.R, tree), "release_commit", "retracted_by = \"0.1.1\"\nrelease_commit", 1))
	commitAll(t, f.repo, map[string]string{}, "retract 0.1.0")
	fake := gh(t)
	var written int
	r := run(t, false, func(ctx *strictcli.Context) error {
		ws, err := workspace.Discover(f.repo.Dir)
		if err != nil {
			return err
		}
		repo, err := git.Open(ctx.Effects(), ws.Root)
		if err != nil {
			return err
		}
		record, err := lifecycle.Load(ws.Root)
		if err != nil {
			return err
		}
		client, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		scanner, err := publishrules.LoadScanner(repo, testsupport.TermList(t), scrubNow())
		if err != nil {
			return err
		}
		repair := historyrewrite.ReleaseRepair{Repo: repo, Workspace: ws, Record: record, GitHub: client, Repository: github.Repository{Owner: "acme", Name: "portal"}, Scanner: scanner, Say: ctx.Out}
		written, err = historyrewrite.RewriteReleases(repair, []historyrewrite.TagRewrite{{Refname: "refs/tags/v0.1.0", OldSHA: f.h.R, NewSHA: f.h.R}})
		return err
	})
	requireExit(t, r, 0)
	if written != 0 || !strings.Contains(r.Stdout, "retracted by 0.1.1") {
		t.Fatalf("wrote %d Release(s):\n%s", written, r.Stdout)
	}
	for _, c := range fake.Calls() {
		if len(c.Args) > 1 && c.Args[0] == "release" {
			t.Fatalf("a Release was touched: %v", c.Args)
		}
	}
}
