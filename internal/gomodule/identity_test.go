package gomodule

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestEverySpellingOfARemoteIsTheSameIdentity(t *testing.T) {
	hygiene.Isolate(t)
	for _, url := range []string{
		"git@github.com:owner/repo.git",
		"git@github.com:owner/repo",
		"https://github.com/owner/repo.git",
		"https://github.com/owner/repo",
		"ssh://git@github.com/owner/repo.git",
		"ssh://git@github.com:22/owner/repo",
	} {
		id, ok := ParseRemote(url)
		if !ok || id.Host != "github.com" || id.Path != "owner/repo" || !id.HostIsADomain() {
			t.Errorf("%s: %+v, %v", url, id, ok)
		}
	}
	id, ok := ParseRemote("git@gp:owner/repo.git")
	if !ok || id.HostIsADomain() {
		t.Errorf("an SSH alias reads as a domain: %+v", id)
	}
	if id, _ := ParseRemote("https://gitlab.com/group/sub/repo.git"); id.Path != "group/sub/repo" {
		t.Errorf("a nested group path was cut: %q", id.Path)
	}
	for _, url := range []string{"", "   ", "/local/path", "not a remote"} {
		if _, ok := ParseRemote(url); ok {
			t.Errorf("%q parsed", url)
		}
	}
}

func TestTheExpectedPathAppendsTheSubdirectory(t *testing.T) {
	hygiene.Isolate(t)
	id, _ := ParseRemote("git@github.com:owner/repo.git")
	if full, tail := ExpectedModulePath(id, "."); full != "github.com/owner/repo" || tail != "owner/repo" {
		t.Errorf("root: %q %q", full, tail)
	}
	if full, _ := ExpectedModulePath(id, "services/api"); full != "github.com/owner/repo/services/api" {
		t.Errorf("subdirectory: %q", full)
	}
	for in, want := range map[string]string{
		"github.com/o/r/v2":  "github.com/o/r",
		"github.com/o/r/v10": "github.com/o/r",
		"github.com/o/r/v1":  "github.com/o/r/v1",
		"github.com/o/r":     "github.com/o/r",
	} {
		if got := StripMajorSuffix(in); got != want {
			t.Errorf("StripMajorSuffix(%q) = %q", in, got)
		}
	}
}

// identityRepo is a repository root with go.mod files at the given
// repository-relative directories, each declaring its module path.
func identityRepo(t *testing.T, modules map[string]string) (string, []string) {
	t.Helper()
	root := t.TempDir()
	var dirs []string
	for rel, module := range modules {
		dir := filepath.Join(root, filepath.FromSlash(rel))
		content := "go 1.21\n"
		if module != "" {
			content = "module " + module + "\n\n" + content
		}
		testsupport.WriteFile(t, filepath.Join(dir, "go.mod"), content)
		dirs = append(dirs, dir)
	}
	return root, dirs
}

func TestAMovedModuleIsAProblemNamingTheRewriteAndTheRewriteClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	root, dirs := identityRepo(t, map[string]string{".": "github.com/oldowner/repo/v2"})
	v, err := EvaluateIdentity(root, dirs, "git@github.com:owner/repo.git")
	if err != nil {
		t.Fatal(err)
	}
	if len(v.Problems) != 1 || !strings.Contains(v.Problems[0], "rlsbl rewrite go-module-path --from-module github.com/oldowner/repo/v2 --to-module github.com/owner/repo/v2") {
		t.Fatalf("problems = %q", v.Problems)
	}
	// Performing the named rewrite on go.mod clears the problem.
	testsupport.WriteFile(t, filepath.Join(root, "go.mod"), "module github.com/owner/repo/v2\n\ngo 1.21\n")
	if v, err := EvaluateIdentity(root, dirs, "git@github.com:owner/repo.git"); err != nil || !v.OK() || len(v.Notes) != 1 {
		t.Fatalf("after the rewrite: %+v, %v", v, err)
	}
}

func TestAMemberSubdirectoryIsPartOfTheExpectedPath(t *testing.T) {
	hygiene.Isolate(t)
	root, dirs := identityRepo(t, map[string]string{"widget": "github.com/owner/repo"})
	v, err := EvaluateIdentity(root, dirs, "https://github.com/owner/repo.git")
	if err != nil || len(v.Problems) != 1 || !strings.HasPrefix(v.Problems[0], "widget/go.mod: ") || !strings.Contains(v.Problems[0], "--to-module github.com/owner/repo/widget`") {
		t.Fatalf("%+v, %v", v, err)
	}
}

func TestTheMajorSubdirectoryLayoutIsNotDoubled(t *testing.T) {
	hygiene.Isolate(t)
	root, dirs := identityRepo(t, map[string]string{"v2": "github.com/owner/repo/v2"})
	if v, err := EvaluateIdentity(root, dirs, "git@github.com:owner/repo.git"); err != nil || !v.OK() {
		t.Fatalf("%+v, %v", v, err)
	}
	root, dirs = identityRepo(t, map[string]string{"v2": "github.com/owner/repo/v2/v2"})
	if v, err := EvaluateIdentity(root, dirs, "git@github.com:owner/repo.git"); err != nil || v.OK() {
		t.Fatalf("a doubled suffix passed: %+v, %v", v, err)
	}
}

func TestAnSSHAliasComparesOnlyTheTailAndSaysSo(t *testing.T) {
	hygiene.Isolate(t)
	root, dirs := identityRepo(t, map[string]string{".": "git.example.org/owner/repo"})
	v, err := EvaluateIdentity(root, dirs, "git@gp:owner/repo.git")
	if err != nil || !v.OK() || !strings.Contains(v.Notes[0], "NOT verified") {
		t.Fatalf("%+v, %v", v, err)
	}
	root, dirs = identityRepo(t, map[string]string{".": "git.example.org/owner/other"})
	v, err = EvaluateIdentity(root, dirs, "git@gp:owner/repo.git")
	if err != nil || v.OK() || !strings.Contains(v.Problems[0], "--to-module git.example.org/owner/repo`") {
		t.Fatalf("%+v, %v", v, err)
	}
}

func TestNoOriginSkipsInsteadOfGuessing(t *testing.T) {
	hygiene.Isolate(t)
	root, dirs := identityRepo(t, map[string]string{".": "github.com/owner/repo"})
	for _, remote := range []string{"", "nonsense"} {
		v, err := EvaluateIdentity(root, dirs, remote)
		if err != nil || v.SkipReason == "" || !v.OK() {
			t.Errorf("remote %q: %+v, %v", remote, v, err)
		}
	}
	if v, _ := EvaluateIdentity(root, nil, "git@github.com:owner/repo.git"); v.SkipReason != "no Go module in this project" {
		t.Errorf("no modules: %+v", v)
	}
	if err := os.Remove(filepath.Join(root, "go.mod")); err != nil {
		t.Fatal(err)
	}
	if v, _ := EvaluateIdentity(root, dirs, "git@github.com:owner/repo.git"); v.SkipReason != "no go.mod found in any Go target" {
		t.Errorf("no go.mod: %+v", v)
	}
}

func TestAGoModWithoutAModuleLineIsAProblem(t *testing.T) {
	hygiene.Isolate(t)
	root, dirs := identityRepo(t, map[string]string{".": ""})
	v, err := EvaluateIdentity(root, dirs, "git@github.com:owner/repo.git")
	if err != nil || len(v.Problems) != 1 || !strings.Contains(v.Problems[0], "declares no module path") {
		t.Fatalf("%+v, %v", v, err)
	}
}
