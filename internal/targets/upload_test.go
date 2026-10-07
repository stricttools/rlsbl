package targets

import (
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestTheGoModuleZipIsTheTrackedFilesLessNestedModules(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	for _, f := range []string{"go.mod", "main.go", "todo/plan.md", "nested/go.mod", "nested/x.go", "vendor/dep/dep.go", "CLAUDE.md"} {
		repo.Write(f, "x\n")
	}
	repo.Commit("files", ".")
	repo.Write("untracked.go", "x\n")
	var listing UploadListing
	var found bool
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		target, _ := Get(Go)
		listing, found, err = ListUpload(nil, r, target, repo.Dir)
		return err
	})
	if !found || !slices.Equal(listing.Files, []string{"CLAUDE.md", "go.mod", "main.go", "todo/plan.md"}) {
		t.Fatalf("files = %q", listing.Files)
	}
	private := PrivatePathsIn(listing.Files)
	if len(private) != 2 {
		t.Fatalf("private = %+v", private)
	}
	if fix := listing.PrivatePathFix(private[0].Rel, private[0].Rule, private[0].Directory); !strings.Contains(fix, "move it to .claude/CLAUDE.md") {
		t.Errorf("CLAUDE.md fix: %s", fix)
	}
	if fix := listing.PrivatePathFix(private[1].Rel, private[1].Rule, private[1].Directory); !strings.Contains(fix, "put a stub go.mod in todo/ (`rlsbl scaffold` writes it)") {
		t.Errorf("todo/ fix: %s", fix)
	}
}

func TestASelfdocGeneratedClaudeFileIsMovedBySelfdoc(t *testing.T) {
	hygiene.Isolate(t)
	dir := project(t, map[string]string{"CLAUDE.md": SelfdocGeneratedHeader + "x -->\n# portal\n", "docs/CLAUDE.md": SelfdocGeneratedHeader + "y -->\n"})
	if fix := goPrivatePathFix(dir, "CLAUDE.md", ""); !strings.Contains(fix, "selfdoc layout migrate") || !strings.HasSuffix(fix, "run `rlsbl scaffold`, which writes the stub go.mod in .claude/") {
		t.Errorf("root: %s", fix)
	}
	if fix := goPrivatePathFix(dir, "docs/CLAUDE.md", ""); !strings.HasSuffix(fix, "put a stub go.mod in docs/.claude/") {
		t.Errorf("nested: %s", fix)
	}
	if fix := goPrivatePathFix(dir, "notes.local-only", ""); !strings.Contains(fix, "git rm --cached notes.local-only") {
		t.Errorf("a file: %s", fix)
	}
}

func TestAPythonUploadIsNotListedOffline(t *testing.T) {
	hygiene.Isolate(t)
	target, _ := Get(PyPI)
	if _, found, err := ListUpload(nil, git.Repo{}, target, t.TempDir()); err != nil || found {
		t.Fatalf("found %v, %v", found, err)
	}
}
