package lifecycleops_test

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/lifecycleops"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// twoPeriods licenses portal proprietary twice, the second time to this
// day, with a codename and a distinctive term.
const twoPeriods = `format_version = 1
codenames = ["moonbeam"]
distinctive_terms = ["quasarflux"]

[[licenses]]
subject = "portal"
license = "MIT"
from = 2026-01-01
until = 2026-02-01
reason = "first release"

[[licenses]]
subject = "portal"
license = "proprietary"
from = 2026-02-01
until = 2026-03-01
reason = "a private pilot"

[[licenses]]
subject = "portal"
license = "MIT"
from = 2026-03-01
until = 2026-05-01
reason = "the pilot ended"

[[licenses]]
subject = "portal"
license = "proprietary"
from = 2026-05-01
reason = "the server work"
` + portalName

const (
	archiveRel    = ".strictmetadata/releases/portal/v0.1.0.toml"
	releasedRel   = ".strictmetadata/changelog/portal/0.1.0.jsonl"
	unreleasedRel = ".strictmetadata/changelog/portal/unreleased.jsonl"
)

func entry(n int, commits ...string) string {
	return changelog.Serialize(changelog.Entry{ID: fmt.Sprintf("%048x", n), Commits: commits, UserFacing: true, Type: changelog.TypeFix, Description: "Fixed it"}) + "\n"
}

// squashDocument is safegit's --json document of one squash.
func squashDocument(t *testing.T, payload map[string]any) string {
	t.Helper()
	data, err := json.Marshal(map[string]any{"interface_version": 3, "app": "safegit", "command": "scrub squash", "exit_code": 0, "payload": payload})
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestDeclassifySquashesEachProprietaryPeriodAndGoesPublic(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	// The history: A public; B, C (released as 0.1.0), and D in the first
	// proprietary period; E public, archiving 0.1.0; F and G in the second
	// period, which runs to this day.
	a := commitOn(t, repo, "2026-01-10", map[string]string{
		declarations.ReleasablesFile: portalDeclarations,
		lifecycle.RecordFile:         twoPeriods,
		lifecycle.ManifestFile:       "owner = \"strictspec\"\n",
		"notes.txt":                  "public\n",
	}, "the project")
	b := commitOn(t, repo, "2026-02-05", map[string]string{"notes.txt": "moonbeam one\n"}, "moonbeam work")
	c := commitOn(t, repo, "2026-02-10", map[string]string{"notes.txt": "moonbeam two\n"}, "moonbeam release")
	d := commitOn(t, repo, "2026-02-20", map[string]string{"notes.txt": "moonbeam three\n"}, "moonbeam more")
	repo.Git("tag", "v0.1.0", c)
	archive := "format_version = 2\nbump = \"minor\"\ninclude = []\nexclude = []\ndescription = \"The pilot\"\nrelease_commit = \"" + c + "\"\n\n[released_trees]\n\".\" = \"" + treeOf(repo, c) + "\"\n"
	e := commitOn(t, repo, "2026-03-10", map[string]string{archiveRel: archive, releasedRel: entry(1, b, c), "notes.txt": "public again\n"}, "archive 0.1.0")
	f := commitOn(t, repo, "2026-05-02", map[string]string{"notes.txt": "quasarflux one\n"}, "quasarflux work")
	g := commitOn(t, repo, "2026-05-03", map[string]string{unreleasedRel: entry(2, f)}, "changelog")
	bare := repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main", "v0.1.0")

	fx := &fixture{repo: repo, index: filepath.Join(t.TempDir(), "confidential-names.toml")}
	if err := os.WriteFile(fx.index, []byte("format_version = 1\n\n[[repositories]]\nsubjects = [\"portal\"]\nnames = [\"moonbeam\", \"portal\"]\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	// What safegit's two squashes write, newest period first.
	s2 := commitTree(t, repo, treeOf(repo, g), []string{e}, g, lifecycleops.SquashMessage)
	s1 := commitTree(t, repo, treeOf(repo, d), []string{a}, d, lifecycleops.SquashMessage)
	e2 := commitTree(t, repo, treeOf(repo, e), []string{s1}, e, "archive 0.1.0")
	s22 := commitTree(t, repo, treeOf(repo, g), []string{e2}, g, lifecycleops.SquashMessage)
	sg := newFakeSafegit(t)
	sg.Squash(fmt.Sprintf("git update-ref refs/heads/main %s && git reset -q --hard %s\n", s2, s2), squashDocument(t, map[string]any{
		"version": 1, "dry_run": false, "first": f, "last": g, "squashed": []string{f, g}, "message": lifecycleops.SquashMessage, "old_head": g,
		"squash_commit": s2, "rewrites": map[string]string{f: s2, g: s2}, "tags": []any{}, "commits_rewritten": 1, "new_head": s2, "cleanup_ok": true,
	}), 0)
	sg.Squash(fmt.Sprintf("git update-ref refs/heads/main %s && git reset -q --hard %s && git tag -f v0.1.0 %s >/dev/null\n", s22, s22, s1), squashDocument(t, map[string]any{
		"version": 1, "dry_run": false, "first": b, "last": d, "squashed": []string{b, c, d}, "message": lifecycleops.SquashMessage, "old_head": s2,
		"squash_commit": s1, "rewrites": map[string]string{b: s1, c: s1, d: s1, e: e2, s2: s22},
		"tags":              []map[string]any{{"refname": "refs/tags/v0.1.0", "old_sha": c, "new_sha": s1, "annotated": false}},
		"commits_rewritten": 3, "new_head": s22, "cleanup_ok": true,
	}), 0)
	marker, err := github.CISHAMarker(c)
	if err != nil {
		t.Fatal(err)
	}
	visibility := []string{"repo", "edit", "acme/portal", "--visibility", "public", "--accept-visibility-change-consequences"}
	rewrite := []string{"release", "edit", "v0.1.0", "--repo", "acme/portal", "--notes-file", "-", "--title", "v0.1.0", "--prerelease=false"}
	gh := testsupport.FakeGH(t,
		ghAuth,
		testsupport.GHAnswer{Args: []string{"--version"}, Stdout: "gh version 2.0.0\n"},
		testsupport.GHAnswer{Args: []string{"release", "view", "v0.1.0", "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}, Stdout: "v0.1.0\n"},
		testsupport.GHAnswer{Args: []string{"release", "view", "v0.1.0", "--repo", "acme/portal", "--json", "body", "--jq", ".body"}, Stdout: "The pilot\n\n" + marker + "\n"},
		testsupport.GHAnswer{Args: rewrite},
		testsupport.GHAnswer{Args: visibility},
	)

	r := fx.run(t, false, func(inv lifecycleops.Invocation, dir string) error {
		return inv.Declassify(lifecycleops.DeclassifyRequest{Dir: dir, Licenses: map[string]string{"portal": "MIT"}, Reason: "the server goes open source"})
	})
	requireExit(t, r, 0)

	// Two periods squashed into two commits, newest first, with the fixed
	// message, which names no confidential term.
	squashes := sg.Squashes()
	if len(squashes) != 2 || !strings.Contains(squashes[0], "--first "+f+" --last "+g) || !strings.Contains(squashes[1], "--first "+b+" --last "+d) {
		t.Fatalf("squashes %q", squashes)
	}
	if found := index.ScanTerms(lifecycleops.SquashMessage, []string{"moonbeam", "quasarflux", "portal", "origin"}); len(found) != 0 {
		t.Fatalf("the squash message names %+v", found)
	}
	// The public history before and after each period is kept: A, the
	// first squash, E rewritten onto it, the second squash, and the
	// declassification's commit on top.
	head := repo.Head()
	for _, link := range []struct{ child, parent string }{{head, s22}, {s22, e2}, {e2, s1}, {s1, a}} {
		if got := repo.Git("rev-parse", link.child+"^"); got != link.parent {
			t.Fatalf("the parent of %s is %s, want %s", link.child, got, link.parent)
		}
	}
	if treeOf(repo, e2) != treeOf(repo, e) {
		t.Fatal("the public commit after the first period lost its tree")
	}
	// The tag inside the first period moved to its squash commit, on
	// origin too, and its archive is unrecoverable.
	remote := testsupport.Refs(t, bare)
	if remote["refs/tags/v0.1.0"] != s1 || remote["refs/heads/main"] != head {
		t.Fatalf("origin holds %v", remote)
	}
	archived := readFile(t, repo, archiveRel)
	if strings.Contains(archived, "release_commit") || strings.Contains(archived, "released_trees") || !strings.Contains(archived, "unrecoverable = true") {
		t.Fatalf("the archive reads:\n%s", archived)
	}
	if got := readFile(t, repo, releasedRel); got != entry(1, s1) {
		t.Errorf("the released changelog reads %q", got)
	}
	if got := readFile(t, repo, unreleasedRel); got != entry(2, s22) {
		t.Errorf("the unreleased changelog reads %q", got)
	}
	// The record is public: the proprietary license closed today, MIT
	// open, no codenames or terms; the index entry is gone.
	rec, err := lifecycle.Parse([]byte(readFile(t, repo, lifecycle.RecordFile)))
	if err != nil {
		t.Fatal(err)
	}
	if rec.Confidential(today) || len(rec.Codenames()) != 0 || len(rec.DistinctiveTerms()) != 0 {
		t.Fatalf("the record:\n%s", readFile(t, repo, lifecycle.RecordFile))
	}
	if l, ok := rec.LicenseOn("portal", today); !ok || l.License != "MIT" || !l.Open() {
		t.Fatalf("portal's license is %+v", l)
	}
	if strings.Contains(fx.indexText(t), "[[repositories]]") {
		t.Fatalf("the index keeps the repository:\n%s", fx.indexText(t))
	}
	// The run is archived and committed, and nothing is left in progress.
	if got := readFile(t, repo, ".strictmetadata/history-rewrites/20260601T120000Z.toml"); !strings.Contains(got, `operation = "declassify"`) || !strings.Contains(got, `mode = "squash"`) {
		t.Fatalf("the rewrite archive:\n%s", got)
	}
	// It records both squash commits as the history now holds them, which
	// the changelog batch checks leave out.
	if squashes, err := releaserecord.DeclassifySquashCommits(repo.Dir); err != nil || len(squashes) != 2 || !squashes[s1] || !squashes[s22] {
		t.Fatalf("the archive records the squash commits %v (%v), want %s and %s", squashes, err, s1, s22)
	}
	if s := repo.Git("status", "--porcelain", "--", ".strictmetadata/changelog", ".strictmetadata/releases", ".strictmetadata/lifecycle-and-license", ".strictmetadata/history-rewrites"); s != "" {
		t.Fatalf("records left uncommitted:\n%s", s)
	}
	if _, err := os.Stat(repo.Path(runstate.DeclassifyResultPath)); !os.IsNotExist(err) {
		t.Fatalf("the declassify result is left behind (%v)", err)
	}
	// The Release of the moved tag was rewritten, naming the squash commit,
	// and the repository was made public.
	var rewrote, madePublic bool
	for _, call := range gh.Calls() {
		switch strings.Join(call.Args, " ") {
		case strings.Join(rewrite, " "):
			rewrote = strings.Contains(call.Stdin, "<!-- rlsbl-ci-sha: "+s1+" -->")
		case strings.Join(visibility, " "):
			madePublic = true
		}
	}
	if !rewrote || !madePublic {
		t.Fatalf("Release rewritten %v, made public %v: %+v", rewrote, madePublic, gh.Calls())
	}
}

func TestDeclassifyRefusesLicensesThatDoNotNameEveryProprietaryReleasable(t *testing.T) {
	hygiene.Isolate(t)
	f := newFixture(t, "format_version = 1\n\n[[licenses]]\nsubject = \"portal\"\nlicense = \"proprietary\"\nfrom = 2026-01-01\nreason = \"server\"\n"+portalName)
	f.repo.Git("remote", "add", "origin", "https://github.com/acme/portal.git")
	declassify := func(licenses map[string]string) (int, string) {
		r := f.run(t, false, func(inv lifecycleops.Invocation, dir string) error {
			return inv.Declassify(lifecycleops.DeclassifyRequest{Dir: dir, Licenses: licenses, Reason: "open"})
		})
		return r.ExitCode, r.Stderr
	}
	for _, c := range []struct {
		licenses map[string]string
		want     string
	}{
		{map[string]string{}, "--license portal=<SPDX identifier>"},
		{map[string]string{"portal": "MIT", "gizmo": "MIT"}, `"gizmo", whose license on 2026-06-01 is not proprietary`},
		{map[string]string{"portal": "proprietary"}, "does not name the public license"},
	} {
		if code, stderr := declassify(c.licenses); code != 1 || !strings.Contains(stderr, c.want) {
			t.Errorf("%v: exit %d: %s", c.licenses, code, stderr)
		}
	}
	public := newFixture(t, mitSince)
	r := public.run(t, false, func(inv lifecycleops.Invocation, dir string) error {
		return inv.Declassify(lifecycleops.DeclassifyRequest{Dir: dir, Licenses: map[string]string{"portal": "MIT"}, Reason: "open"})
	})
	requireExit(t, r, 1)
	requireContains(t, r.Stderr, "public already")
}

func TestADryRunDeclassificationPrintsTheSquashesAndWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	commitOn(t, repo, "2026-04-10", map[string]string{declarations.ReleasablesFile: portalDeclarations, lifecycle.RecordFile: "format_version = 1\n\n[[licenses]]\nsubject = \"portal\"\nlicense = \"proprietary\"\nfrom = 2026-05-01\nreason = \"server\"\n" + portalName, lifecycle.ManifestFile: "owner = \"strictspec\"\n"}, "the project")
	first := commitOn(t, repo, "2026-05-02", map[string]string{"notes.txt": "one\n"}, "one")
	commitOn(t, repo, "2026-05-03", map[string]string{"notes.txt": "two\n"}, "two")
	repo.AddBareRemote("origin")
	head := repo.Head()
	sg := newFakeSafegit(t)
	testsupport.FakeGH(t, ghAuth)
	fx := &fixture{repo: repo, index: filepath.Join(t.TempDir(), "index.toml")}
	r := fx.run(t, true, func(inv lifecycleops.Invocation, dir string) error {
		return inv.Declassify(lifecycleops.DeclassifyRequest{Dir: dir, Licenses: map[string]string{"portal": "MIT"}, Reason: "open"})
	})
	requireExit(t, r, 0)
	requireContains(t, r.Stdout, "2 commit(s), "+first+" through "+head, "Nothing was written")
	if repo.Head() != head || len(sg.Squashes()) != 0 {
		t.Fatal("a dry run rewrote the history")
	}
	if _, err := os.Stat(repo.Path(runstate.DeclassifyResultPath)); !os.IsNotExist(err) {
		t.Fatal("a dry run saved a declassify result")
	}
}

func readFile(t *testing.T, repo *testsupport.Repo, rel string) string {
	t.Helper()
	data, err := os.ReadFile(repo.Path(rel))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestDeclassifyRefusalsClearOnceFixed(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	record := "format_version = 1\ncodenames = [\"moonbeam\"]\n\n[[licenses]]\nsubject = \"portal\"\nlicense = \"proprietary\"\nfrom = 2026-05-01\nreason = \"server\"\n" + portalName
	commitOn(t, repo, "2026-05-02", map[string]string{declarations.ReleasablesFile: portalDeclarations, lifecycle.RecordFile: record, lifecycle.ManifestFile: "owner = \"strictspec\"\n"}, "the project")
	repo.AddBareRemote("origin")
	newFakeSafegit(t)
	testsupport.FakeGH(t, ghAuth)
	fx := &fixture{repo: repo, index: filepath.Join(t.TempDir(), "index.toml")}
	preview := func(reason string) (int, string) {
		r := fx.run(t, true, func(inv lifecycleops.Invocation, dir string) error {
			return inv.Declassify(lifecycleops.DeclassifyRequest{Dir: dir, Licenses: map[string]string{"portal": "MIT"}, Reason: reason})
		})
		return r.ExitCode, r.Stderr
	}

	code, stderr := preview("moonbeam goes open source")
	if code != 1 || !strings.Contains(stderr, `--reason names "moonbeam"`) || !strings.Contains(stderr, "Word the reason without it") {
		t.Fatalf("a reason naming a codename: exit %d: %s", code, stderr)
	}
	if code, stderr := preview("the server goes open source"); code != 0 {
		t.Fatalf("the reworded reason: exit %d: %s", code, stderr)
	}

	repo.Git("checkout", "-q", "-b", "side")
	code, stderr = preview("the server goes open source")
	if code != 1 || !strings.Contains(stderr, "side is not a release branch") {
		t.Fatalf("off a release branch: exit %d: %s", code, stderr)
	}
	repo.Git("checkout", "-q", "main")
	if code, stderr := preview("the server goes open source"); code != 0 {
		t.Fatalf("back on main: exit %d: %s", code, stderr)
	}
}

// declassifyPreview is a repository with a proprietary portal on main, an
// origin, a safegit new enough, and gh logged in, whose declassification
// dry run passes; preview runs it.
func declassifyPreview(t *testing.T, record string) (*testsupport.Repo, func() (int, string)) {
	t.Helper()
	repo := testsupport.NewRepo(t)
	commitOn(t, repo, "2026-05-02", map[string]string{declarations.ReleasablesFile: portalDeclarations, lifecycle.RecordFile: record, lifecycle.ManifestFile: "owner = \"strictspec\"\n"}, "the project")
	repo.AddBareRemote("origin")
	newFakeSafegit(t)
	testsupport.FakeGH(t, ghAuth)
	fx := &fixture{repo: repo, index: filepath.Join(t.TempDir(), "index.toml")}
	return repo, func() (int, string) {
		r := fx.run(t, true, func(inv lifecycleops.Invocation, dir string) error {
			return inv.Declassify(lifecycleops.DeclassifyRequest{Dir: dir, Licenses: map[string]string{"portal": "MIT"}, Reason: "the server goes open source"})
		})
		return r.ExitCode, r.Stderr
	}
}

const proprietaryPortal = "format_version = 1\n\n[[licenses]]\nsubject = \"portal\"\nlicense = \"proprietary\"\nfrom = 2026-05-01\nreason = \"server\"\n" + portalName

// fakeSafegitVersion puts a safegit reporting version first on PATH.
func fakeSafegitVersion(t *testing.T, version string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "safegit"), []byte("#!/bin/sh\necho \"safegit "+version+"\"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func TestDeclassifyRefusalsNamingAFixClearOnceItIsMade(t *testing.T) {
	hygiene.Isolate(t)
	var savedOrigin string
	cases := []struct {
		name  string
		cause func(t *testing.T, repo *testsupport.Repo)
		want  string
		fix   func(t *testing.T, repo *testsupport.Repo)
	}{
		{
			name:  "a release stopped mid-flight",
			cause: func(t *testing.T, repo *testsupport.Repo) { repo.Write(runstate.InProgressPath("portal"), "stopped\n") },
			want:  "rlsbl release abandon",
			// What the abandon or the finished release leaves: no state.
			fix: func(t *testing.T, repo *testsupport.Repo) {
				removeFixtureFile(t, repo, runstate.InProgressPath("portal"))
			},
		},
		{
			name:  "a scrub in progress",
			cause: func(t *testing.T, repo *testsupport.Repo) { repo.Write(runstate.ScrubResultPath, "{}\n") },
			want:  "running the same `rlsbl release scrub` again",
			// What the finished scrub leaves: no result file.
			fix: func(t *testing.T, repo *testsupport.Repo) { removeFixtureFile(t, repo, runstate.ScrubResultPath) },
		},
		{
			name: "no origin remote",
			cause: func(t *testing.T, repo *testsupport.Repo) {
				savedOrigin = repo.Git("remote", "get-url", "origin")
				repo.Git("remote", "remove", "origin")
			},
			want: "git remote add origin <url>",
			fix: func(t *testing.T, repo *testsupport.Repo) {
				repo.Git("remote", "add", "origin", savedOrigin)
			},
		},
		{
			name:  "an old safegit",
			cause: func(t *testing.T, repo *testsupport.Repo) { fakeSafegitVersion(t, "0.1.0") },
			want:  "install that safegit",
			fix:   func(t *testing.T, repo *testsupport.Repo) { newFakeSafegit(t) },
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hygiene.Isolate(t)
			repo, preview := declassifyPreview(t, proprietaryPortal)
			if code, stderr := preview(); code != 0 {
				t.Fatalf("before: exit %d: %s", code, stderr)
			}
			c.cause(t, repo)
			if code, stderr := preview(); code != 1 || !strings.Contains(stderr, c.want) {
				t.Fatalf("not refused naming %q: exit %d: %s", c.want, code, stderr)
			}
			c.fix(t, repo)
			if code, stderr := preview(); code != 0 {
				t.Fatalf("after the fix: exit %d: %s", code, stderr)
			}
		})
	}
}

func TestDeclassifyRefusesACodenameTheSquashMessageNamesUntilItIsRemoved(t *testing.T) {
	hygiene.Isolate(t)
	repo, preview := declassifyPreview(t, "format_version = 1\ncodenames = [\"squash\"]\n\n[[licenses]]\nsubject = \"portal\"\nlicense = \"proprietary\"\nfrom = 2026-05-01\nreason = \"server\"\n"+portalName)
	if code, stderr := preview(); code != 1 || !strings.Contains(stderr, `Remove "squash" from the record's codenames`) {
		t.Fatalf("a codename the squash message names: exit %d: %s", code, stderr)
	}
	repo.Write(lifecycle.RecordFile, proprietaryPortal)
	if code, stderr := preview(); code != 0 {
		t.Fatalf("with the codename removed: exit %d: %s", code, stderr)
	}
}

func removeFixtureFile(t *testing.T, repo *testsupport.Repo, rel string) {
	t.Helper()
	if err := os.Remove(repo.Path(rel)); err != nil {
		t.Fatal(err)
	}
}
