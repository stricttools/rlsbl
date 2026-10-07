package release_test

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// fakeProgram puts an executable shell script of that name first on PATH.
func fakeProgram(t *testing.T, name, script string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, name), []byte("#!/bin/sh\n"+script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func owedSyncs(t *testing.T, repo *testsupport.Repo, releasable string) ([]release.LockfileSync, error) {
	t.Helper()
	var syncs []release.LockfileSync
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		ws, err := workspace.Load(repo.Dir)
		if err != nil {
			return err
		}
		syncs, err = release.OwedLockfileSyncs(r, ws, releasable)
		return err
	})
	return syncs, err
}

func syncSummary(syncs []release.LockfileSync) string {
	var out []string
	for _, s := range syncs {
		out = append(out, s.Dir+":"+strings.Join(s.Argv, " "))
	}
	return strings.Join(out, ",")
}

func TestAnIgnoredLockfileIsNotOwedAndAPresentOneIs(t *testing.T) {
	hygiene.Isolate(t)
	fakeProgram(t, "npm", "exit 0\n")
	repo := standalone(t, "ci")
	repo.CommitFile("package-lock.json", "{}\n", "lock")
	syncs, err := owedSyncs(t, repo, "portal")
	mustNotFail(t, err)
	if got := syncSummary(syncs); got != ".:npm install --package-lock-only --ignore-scripts" {
		t.Errorf("owed: %s", got)
	}
	repo.Git("rm", "-q", "--cached", "package-lock.json")
	repo.CommitFile(".gitignore", "package-lock.json\n", "ignore the lock")
	syncs, err = owedSyncs(t, repo, "portal")
	mustNotFail(t, err)
	if len(syncs) != 0 {
		t.Errorf("an ignored lockfile is owed: %v", syncs)
	}
}

func TestALockfileWhoseToolIsMissingRefusesUntilTheToolIsOnPath(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "ci")
	repo.CommitFile("package-lock.json", "{}\n", "lock")
	testsupport.PathOnly(t, "git")
	_, err := owedSyncs(t, repo, "portal")
	var missing *release.LockfileToolMissingError
	if !errors.As(err, &missing) || !strings.Contains(err.Error(), "`npm` is not on PATH") || !strings.Contains(err.Error(), "package-lock.json") {
		t.Fatalf("a missing npm was not refused: %v", err)
	}
	// The fix the refusal names: put npm on PATH.
	fakeProgram(t, "npm", "exit 0\n")
	_, err = owedSyncs(t, repo, "portal")
	mustNotFail(t, err)
}

func TestAWorkspaceReLocksItsRootAndADevNodeLockingABumpedMember(t *testing.T) {
	hygiene.Isolate(t)
	fakeProgram(t, "uv", "exit 0\n")
	repo := testsupport.NewRepo(t)
	repo.Write(declarationsPath, `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "widget"
tag_format = "{name}@v{version}"
publish_mode = "ci"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "widget"
name = "widget"
releasable = "widget"

[[members]]
path = "tools"
name = "tools"
dev_only = true
releasable = false

[[members]]
path = "other"
name = "other"
dev_only = true
releasable = false
`)
	repo.Write("widget/pyproject.toml", "[project]\nname = \"widget\"\nversion = \"0.1.0\"\n")
	repo.Write("widget/uv.lock", "version = 1\n")
	repo.Write("uv.lock", "version = 1\n")
	repo.Write("tools/uv.lock", "version = 1\n\n[[package]]\nname = \"widget\"\nsource = { editable = \"../widget\" }\n")
	repo.Write("other/uv.lock", "version = 1\n\n[[package]]\nname = \"elsewhere\"\nsource = { directory = \"../elsewhere\" }\n")
	repo.Write("go.work.sum", "\n")
	repo.Commit("the workspace", declarationsPath, "widget/pyproject.toml", "widget/uv.lock", "uv.lock", "tools/uv.lock", "other/uv.lock", "go.work.sum")
	syncs, err := owedSyncs(t, repo, "widget")
	mustNotFail(t, err)
	if got := syncSummary(syncs); got != "widget:uv lock,.:uv lock,tools:uv lock" {
		t.Errorf("owed: %s", got)
	}
}

func TestALockfileFailureNamesTheCause(t *testing.T) {
	hygiene.Isolate(t)
	s := release.LockfileSync{Dir: "widget", Lockfile: "widget/package-lock.json", Argv: []string{"npm", "install", "--package-lock-only", "--ignore-scripts"}}
	cause := errors.New("exit 1")
	unpublished := release.LockfileSyncFailure(s, "/live/widget", "npm error notarget No matching version found for gadget@0.3.0.\n", cause, "run `rlsbl release resume`")
	if !strings.Contains(unpublished, "gadget 0.3.0 is required but the registry does not have it") || !strings.Contains(unpublished, "release it first") {
		t.Errorf("unpublished: %s", unpublished)
	}
	network := release.LockfileSyncFailure(s, "/live/widget", "npm error code ENOTFOUND\n", cause, "run `rlsbl release resume`")
	if !strings.Contains(network, "could not be reached") {
		t.Errorf("network: %s", network)
	}
	other := release.LockfileSyncFailure(s, "/live/widget", "npm error something else\n", cause, "run `rlsbl release resume`")
	if !strings.Contains(other, "Fix the error npm printed, then run `rlsbl release resume`") || !strings.Contains(other, "/live/widget") {
		t.Errorf("other: %s", other)
	}
}

func refuseUntidy(t *testing.T, root string, syncs []release.LockfileSync) error {
	t.Helper()
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		return release.RefuseUntidyGoModules(e, root, "/live", syncs, nil)
	})
	return err
}

func TestAnUntidyGoModuleIsRefusedUntilTidied(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	state := filepath.Join(root, "tidy")
	fakeProgram(t, "go", fmt.Sprintf("if [ -e %s ]; then exit 0; fi\necho '--- go.sum'\necho '+example.com/dep v1.0.0 h1:x'\nexit 1\n", state))
	syncs := []release.LockfileSync{{Dir: ".", Lockfile: "go.sum", Argv: []string{"go", "mod", "tidy"}, Timeout: 60e9}}
	err := refuseUntidy(t, root, syncs)
	var untidy *release.UntidyGoModuleError
	if !errors.As(err, &untidy) || !strings.Contains(err.Error(), "is not tidy") || !strings.Contains(err.Error(), "+example.com/dep") {
		t.Fatalf("an untidy module was not refused showing the diff: %v", err)
	}
	// The fix the refusal names: tidy and commit, after which the diff is
	// empty.
	testsupport.WriteFile(t, state, "")
	mustNotFail(t, refuseUntidy(t, root, syncs))
}

func TestAGoWorkspaceSyncRaisingARequirementIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, "go.work"), "go 1.26\n\nuse ./a\n")
	testsupport.WriteFile(t, filepath.Join(root, "a", "go.mod"), "module example.com/a\n\ngo 1.26\n\nrequire example.com/dep v1.0.0\n")
	fakeProgram(t, "go", `echo '{"Path":"example.com/a","Main":true}'
echo '{"Path":"example.com/dep","Version":"v1.1.0"}'
`)
	syncs := []release.LockfileSync{{Dir: ".", Lockfile: "go.work.sum", Argv: []string{"go", "work", "sync"}, Timeout: 60e9}}
	err := refuseUntidy(t, root, syncs)
	if err == nil || !strings.Contains(err.Error(), "example.com/dep v1.0.0 -> v1.1.0") {
		t.Fatalf("a requirement go work sync would raise was not named: %v", err)
	}
	// The fix the refusal names: run go work sync and commit, which raises
	// the requirement.
	testsupport.WriteFile(t, filepath.Join(root, "a", "go.mod"), "module example.com/a\n\ngo 1.26\n\nrequire example.com/dep v1.1.0\n")
	mustNotFail(t, refuseUntidy(t, root, syncs))
}
