package migration

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// TestMain hands the process to the fake gh when the test binary was
// started as gh, and runs the tests otherwise.
func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

// testNow is the migration date of every test.
var testNow = time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)

// shippedPreChecks is a pre-checks.sh a Python scaffold shipped, which the
// migration deletes.
const shippedPreChecks = `#!/usr/bin/env bash
set -euo pipefail
# This hook runs BEFORE built-in pre-release checks (tests, lint).
# Use it for setup tasks: starting services, setting env vars, etc.
# Built-in checks run after this hook. Custom validation goes in pre-release.sh.
`

// oldKey is the first format's event key of a transition record line, spelled
// as the migration spells it.
const oldKey = oldEventKey

// deletingSafermScript stands in for saferm: it records its argv and
// deletes its last argument, so the migration's removals happen.
const deletingSafermScript = `#!/bin/sh
printf '%s\n' "$*" >> "${0%/*}/saferm-calls.txt"
for last; do :; done
rm -rf -- "$last"
`

// fixture is a repository holding an old layout, with a fake gh answering
// its visibility, a fake safegit, and a saferm that deletes.
type fixture struct {
	t         *testing.T
	repo      *testsupport.Repo
	name      string
	licenses  map[string]string
	indexPath string
	safermDir string
}

// newFixture creates the repository, its origin remote naming
// github.com/owner/<name>, and the fakes; visibility is "public" or
// "private".
func newFixture(t *testing.T, name, visibility string) *fixture {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Git("remote", "add", "origin", "git@github.com:owner/"+name+".git")
	testsupport.FakeGH(t, testsupport.GHAnswer{
		Args:   []string{"api", "--method", "GET", "repos/owner/" + name},
		Stdout: fmt.Sprintf(`{"full_name": "owner/%s", "visibility": %q, "private": %t}`, name, visibility, visibility != "public"),
	})
	testsupport.FakeSafegit(t)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "saferm"), []byte(deletingSafermScript), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return &fixture{t: t, repo: repo, name: name, indexPath: filepath.Join(t.TempDir(), "confidential-names.toml"), safermDir: dir}
}

func (f *fixture) write(rel, content string) {
	f.t.Helper()
	f.repo.Write(rel, content)
}

func (f *fixture) read(rel string) string {
	f.t.Helper()
	data, err := os.ReadFile(f.repo.Path(rel))
	if err != nil {
		f.t.Fatal(err)
	}
	return string(data)
}

func (f *fixture) exists(rel string) bool {
	_, err := os.Lstat(f.repo.Path(rel))
	return err == nil
}

// remove deletes a fixture file or directory (the fixture's own setup, not
// the code under test).
func (f *fixture) remove(rel string) {
	f.t.Helper()
	if err := os.RemoveAll(f.repo.Path(rel)); err != nil {
		f.t.Fatal(err)
	}
}

// edit replaces old with new in rel, failing when old is not there once.
func (f *fixture) edit(rel, old, new string) {
	f.t.Helper()
	text := f.read(rel)
	if strings.Count(text, old) != 1 {
		f.t.Fatalf("%s holds %q %d times, not once:\n%s", rel, old, strings.Count(text, old), text)
	}
	f.write(rel, strings.Replace(text, old, new, 1))
}

// commit commits everything in the work tree.
func (f *fixture) commit(message string) string {
	f.t.Helper()
	f.repo.Git("add", "-A")
	f.repo.Git("commit", "-q", "--allow-empty", "-m", message)
	return f.repo.Head()
}

func (f *fixture) request() Request {
	return Request{Root: f.repo.Dir, Licenses: f.licenses, LicensesFile: "licenses.toml", Now: testNow, RlsblVersion: "0.132.0", IndexPath: f.indexPath}
}

func commandOptions(dryRun bool) testsupport.CommandOptions {
	return testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}
}

// plan builds the plan under a dry run, as the command does before it
// writes anything.
func (f *fixture) plan() (*Plan, error) {
	f.t.Helper()
	var plan *Plan
	var buildErr error
	testsupport.RunCommand(f.t, commandOptions(true), func(ctx *strictcli.Context) error {
		plan, buildErr = Build(ctx.Effects(), f.request())
		return nil
	})
	return plan, buildErr
}

// mustPlan is the plan, failing the test on a refusal.
func (f *fixture) mustPlan() *Plan {
	f.t.Helper()
	plan, err := f.plan()
	if err != nil {
		f.t.Fatalf("the migration refused:\n%v", err)
	}
	return plan
}

// refused is the refusal, failing the test when the plan is not refused
// with a problem holding want.
func (f *fixture) refused(want string) string {
	f.t.Helper()
	_, err := f.plan()
	if err == nil {
		f.t.Fatalf("the migration was not refused; want a refusal naming %q", want)
	}
	if !strings.Contains(err.Error(), want) {
		f.t.Fatalf("the refusal does not name %q:\n%v", want, err)
	}
	return err.Error()
}

// migrate runs the command for real and fails the test unless it exits 0.
func (f *fixture) migrate() strictcli.Result {
	f.t.Helper()
	r := testsupport.RunCommand(f.t, commandOptions(false), func(ctx *strictcli.Context) error { return Run(ctx, f.request()) })
	if r.ExitCode != 0 {
		f.t.Fatalf("migrate exited %d\nstdout:\n%s\nstderr:\n%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	return r
}

// planned is the content the plan writes to rel.
func planned(t *testing.T, p *Plan, rel string) string {
	t.Helper()
	for _, w := range p.writes {
		if w.path == rel {
			if w.writer != nil {
				return ""
			}
			return string(w.data)
		}
	}
	var paths []string
	for _, w := range p.writes {
		paths = append(paths, w.path)
	}
	sort.Strings(paths)
	t.Fatalf("the plan writes no %s; it writes:\n  %s", rel, strings.Join(paths, "\n  "))
	return ""
}

// writes reports whether the plan writes rel.
func writes(p *Plan, rel string) bool {
	for _, w := range p.writes {
		if w.path == rel {
			return true
		}
	}
	return false
}

// hasNote reports a note of the plan holding want.
func hasNote(p *Plan, want string) bool {
	for _, n := range p.notes {
		if strings.Contains(n, want) {
			return true
		}
	}
	return false
}

func contains(t *testing.T, text, want string) {
	t.Helper()
	if !strings.Contains(text, want) {
		t.Fatalf("missing %q in:\n%s", want, text)
	}
}

func lacks(t *testing.T, text, unwanted string) {
	t.Helper()
	if strings.Contains(text, unwanted) {
		t.Fatalf("unexpected %q in:\n%s", unwanted, text)
	}
}

// A commit id and tree id the fixtures' records name.
const (
	someCommit = "1111111111111111111111111111111111111111"
	otherSHA   = "2222222222222222222222222222222222222222"
	someHash   = "3333333333333333333333333333333333333333333333333333333333333333"
	someID     = "18dc47e50f0bbc65d295f29408ec48a78b9f83f1c6eb8384"
)

// oldArchive is a first-format archive released from commit.
func oldArchive(commit, tree, member string) string {
	return `# strictspec document version gate (do not remove)
format_version = 1
# Version bump type: patch, minor, major, hotfix, or prerelease
bump = "minor"
description = "The first release"
# preid = ""
include = ["npm"]
exclude = []
candidate_sha = "` + commit + `"

[tree_hashes]
"` + member + `" = "` + tree + `"
`
}

// standalone is a standalone npm project, portal, with a released 0.1.0, an
// unreleased entry without an id, a scaffold state with a merge base, a
// shipped hook script, and a watch log.
func standalone(t *testing.T) *fixture {
	t.Helper()
	f := newFixture(t, "portal", "public")
	f.write("package.json", `{"name": "portal", "version": "0.1.0", "license": "MIT"}`+"\n")
	f.write(".rlsbl/config.json", `{
  "publish_mode": "ci",
  "push_timeout": 120,
  "changelog_format_version_enforced": true,
  "batch_limits": {"exclusions": []},
  "targets": ["npm"],
  "pipelines": {"npm": {"type": "npm", "target": "npm", "local": false, "provenance": true}}
}
`)
	f.write("CHANGELOG.md", "# Changelog\n")
	f.write(".github/workflows/ci.yml", "name: ci\n")
	release := f.commit("the first release")
	tree := f.repo.Git("rev-parse", "HEAD^{tree}")
	f.write(".rlsbl/releases/v0.1.0.toml", oldArchive(release, tree, "."))
	f.write(".rlsbl/changes/0.1.0.jsonl", `{"format_version":1,"id":"`+someID+`","commits":["`+release+`"],"user_facing":true,"description":"The first feature","type":"feature"}`+"\n")
	f.write(".rlsbl/changes/0.1.0.md", "## 0.1.0\n")
	f.write(".rlsbl/changes/.validated", release+"\n")
	f.write(".rlsbl/changes/unreleased.jsonl", `{"format_version":1,"commits":["`+someCommit+`"],"user_facing":false}`+"\n")
	f.write(".rlsbl/version", "0.130.0\n")
	f.write(".rlsbl/managed-files.json", `{"version": 1, "files": {".github/workflows/ci.yml": "`+someHash+`", "CHANGELOG.md": "`+someHash+`", ".rlsbl/hooks/pre-checks.sh": "`+someHash+`"}}`+"\n")
	f.write(".rlsbl/bases/.github/workflows/ci.yml", "name: ci\n")
	f.write(".rlsbl/bases/.rlsbl/hooks/pre-checks.sh", shippedPreChecks)
	f.write(".rlsbl/hooks/pre-checks.sh", shippedPreChecks)
	f.write(".rlsbl/undo-audit.json", "[]\n")
	f.commit("the old layout")
	f.write(".rlsbl/watch-abc.log.local-only", "a watch log\n")
	f.write(".gitignore", "*.local-only\n")
	f.commit("ignore local files")
	return f
}

// workspaceFixture is a workspace whose root member is a dev node, with two
// releasables: widget (npm) and gadget (pypi, depending on widget).
func workspaceFixture(t *testing.T) *fixture {
	t.Helper()
	f := newFixture(t, "portal", "public")
	f.write(".rlsbl-monorepo/workspace.toml", `[[releasables]]
name = "widget"

[[releasables]]
name = "gadget"

[[projects]]
path = "."
name = "root"
dev_only = true
releasable = false

[[projects]]
path = "widget"
name = "widget"
releasable = "widget"

[[projects]]
path = "gadget"
name = "gadget"
releasable = "gadget"
depends_on = ["widget"]

[layers]
order = ["widget", "gadget"]
`)
	f.write(".rlsbl-monorepo/releasables/widget/config.json", `{"publish_mode": "ci", "batch_limits": {"exclusions": []}}`+"\n")
	f.write(".rlsbl-monorepo/releasables/widget/version", "0.3.0\n")
	f.write(".rlsbl-monorepo/releasables/widget/CHANGELOG.md", "# Changelog\n")
	f.write(".rlsbl-monorepo/releasables/widget/changes/unreleased.jsonl", `{"format_version":1,"id":"`+someID+`","commits":["`+someCommit+`"],"user_facing":true,"description":"A widget feature","type":"feature","packages":["widget"]}`+"\n")
	f.write(".rlsbl-monorepo/releasables/gadget/config.json", `{"publish_mode": "ci"}`+"\n")
	f.write(".rlsbl-monorepo/releasables/gadget/version", "0.1.0\n")
	f.write(".rlsbl-monorepo/snapshot.json", "{}\n")
	f.write(".rlsbl-monorepo/publish-cache.json", "{}\n")
	f.write("widget/.rlsbl/config.json", `{"targets": ["npm"], "publish_mode": "ci", "pipelines": {"npm": {"type": "npm", "target": "npm", "local": false}}}`+"\n")
	f.write("widget/package.json", `{"name": "widget", "version": "0.3.0", "license": "MIT"}`+"\n")
	f.write("gadget/.rlsbl/config.json", `{"targets": ["pypi"], "publish_mode": "ci", "pipelines": {"pypi": {"type": "pypi", "target": "pypi", "local": false}}}`+"\n")
	f.write("gadget/pyproject.toml", "[project]\nname = \"gadget\"\nversion = \"0.1.0\"\nlicense = \"MIT\"\n")
	f.write("CHANGELOG.md", "# Changelog\n")
	f.commit("the old layout")
	return f
}
