package workspace

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestTheDirectoryDecidesTheMemberAndTheReleasable(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	w := newWorkspace(t, root, nested)
	for _, c := range []struct {
		dir, member, releasable string
	}{
		{".", "root", ""},
		{"docs", "root", ""},
		{"draw", "draw", "draw"},
		{"draw/cmd/internal", "cmd", "cmd"},
		{"kernel", "kernel", "kernel"},
	} {
		abs := filepath.Join(root, filepath.FromSlash(c.dir))
		m, err := w.MemberAtDirectory(abs)
		if err != nil || m.Name != c.member {
			t.Errorf("MemberAtDirectory(%s) = %q, %v", c.dir, m.Name, err)
		}
		r, found, err := w.ReleasableForDirectory(abs)
		if err != nil || found != (c.releasable != "") || r.Name != c.releasable {
			t.Errorf("ReleasableForDirectory(%s) = %q, %t, %v", c.dir, r.Name, found, err)
		}
	}
	if _, err := w.MemberAtDirectory(filepath.Dir(root)); err == nil {
		t.Error("a directory outside the repository found a member")
	}
	if m, ok := w.MemberForDirectory("docs", false); ok {
		t.Errorf("the root member answered for docs without being asked to: %s", m.Name)
	}
}

func TestNestedMemberPaths(t *testing.T) {
	hygiene.Isolate(t)
	w := newWorkspace(t, t.TempDir(), nested)
	for _, c := range []struct {
		member string
		want   string
	}{{"root", "draw draw/cmd kernel"}, {"draw", "draw/cmd"}, {"cmd", ""}} {
		m, _ := w.Declarations.Member(c.member)
		if got := strings.Join(w.NestedMemberPaths(m), " "); got != c.want {
			t.Errorf("NestedMemberPaths(%s) = %q, want %q", c.member, got, c.want)
		}
	}
}

func TestAMemberVersionedUnderNoneHasNoReleasableAndPublishesNothing(t *testing.T) {
	hygiene.Isolate(t)
	w := newWorkspace(t, t.TempDir(), nested)
	if _, ok := w.ReleasableOf(w.RootMember()); ok {
		t.Error("a dev-node root has a releasable")
	}
	if mode := w.PublishModeOf(w.RootMember()); mode != declarations.PublishNone {
		t.Errorf("PublishModeOf(root) = %s", mode)
	}
}

func writeVersion(t *testing.T, w *Workspace, releasable, version string) strictcli.Result {
	t.Helper()
	v, err := semver.Parse(version)
	if err != nil {
		t.Fatal(err)
	}
	return testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(e *strictcli.Effects) error {
		return w.WriteReleasableVersion(e, releasable, v)
	})
}

func TestAReleasableVersionFileRoundTrips(t *testing.T) {
	hygiene.Isolate(t)
	w := newWorkspace(t, t.TempDir(), nested)
	if _, err := w.ReadReleasableVersion("draw"); err == nil || !strings.Contains(err.Error(), declarations.VersionFile("draw")) {
		t.Fatalf("ReadReleasableVersion of a missing file = %v", err)
	}
	writeVersion(t, w, "draw", "0.4.1")
	v, err := w.ReadReleasableVersion("draw")
	if err != nil || v.String() != "0.4.1" {
		t.Fatalf("ReadReleasableVersion = %s, %v", v, err)
	}
}

func TestAMalformedVersionFileIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	w := newWorkspace(t, root, nested)
	for _, text := range []string{"", "\n", "v1.0.0\n", "1.0\n"} {
		writeFixture(t, root, declarations.VersionFile("kernel"), text)
		if _, err := w.ReadReleasableVersion("kernel"); err == nil {
			t.Errorf("the version file %q was accepted", text)
		}
	}
}

func TestDiscoverLoadsTheEnclosingRepository(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(declarations.ReleasablesFile, standalone)
	repo.Write("src/main.go", "package main\n")
	w, err := Discover(repo.Path("src"))
	if err != nil {
		t.Fatal(err)
	}
	if w.IsWorkspace() || w.Releasables()[0].Name != "gadget" {
		t.Fatalf("Discover read %#v", w.Declarations)
	}
}

// attributionHistory is a repository with one commit to each of: a root
// file, the draw member, its nested cmd member, draw's changelog state, and
// both kernel and draw at once.
func attributionHistory(t *testing.T) (repo *testsupport.Repo, root, draw, cmd, state, both string) {
	t.Helper()
	repo = testsupport.NewRepo(t)
	root = repo.CommitFile("README.md", "x\n", "root file")
	draw = repo.CommitFile("draw/main.go", "package main\n", "draw file")
	cmd = repo.CommitFile("draw/cmd/main.go", "package main\n", "cmd file")
	state = repo.CommitFile(".strictmetadata/changelog/draw/unreleased.jsonl", "{}\n", "draw changelog")
	repo.Write("kernel/a.c", "x\n")
	repo.Write("draw/b.go", "x\n")
	both = repo.Commit("both", "kernel/a.c", "draw/b.go")
	return repo, root, draw, cmd, state, both
}

func TestCommitsAreFilteredToTheScopeTheyTouch(t *testing.T) {
	hygiene.Isolate(t)
	repo, root, draw, cmd, state, both := attributionHistory(t)
	w := newWorkspace(t, repo.Dir, nested)
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		kept, err := FilterCommits(r, []string{root, draw, cmd, state, both}, w.ScopeOfReleasable("draw"))
		if err != nil {
			return err
		}
		if got, want := strings.Join(kept, " "), strings.Join([]string{draw, state, both}, " "); got != want {
			t.Errorf("FilterCommits kept %s, want %s", got, want)
		}
		owners, err := w.CommitOwnerNames(r, both)
		if err != nil {
			return err
		}
		if len(owners) != 2 || !owners["kernel"] || !owners["draw"] {
			t.Errorf("CommitOwnerNames = %v", owners)
		}
		return nil
	})
}
