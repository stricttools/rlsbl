package historyrewrite_test

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

const declarationsPath = ".strictmetadata/releasables/releasables.toml"

// portalDeclarations declares the standalone npm project portal, which
// publishes nothing (so no companion tags), on the GitHub repository
// acme/portal.
const portalDeclarations = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
releasable = "portal"
`

// manifests are the ownership manifests of the record directories, which a
// migrated repository carries.
var manifests = []string{
	".strictmetadata/releases/manifest.toml",
	".strictmetadata/changelog/manifest.toml",
	".strictmetadata/transitions/manifest.toml",
}

const (
	archivePath        = ".strictmetadata/releases/portal/v0.1.0.toml"
	changelogFilePath  = ".strictmetadata/changelog/portal/0.1.0.jsonl"
	unreleasedLogPath  = ".strictmetadata/changelog/portal/unreleased.jsonl"
	scrubResultPath    = ".strictmetadata/.release-state/scrub-result.json"
	transitionsPath    = ".strictmetadata/transitions/transitions.jsonl"
	historyRewritesDir = ".strictmetadata/history-rewrites"
)

func packageJSON(version string) string {
	return fmt.Sprintf("{\n  \"name\": \"portal\",\n  \"version\": %q\n}\n", version)
}

// entryLine is one released, user-facing changelog entry naming commits.
func entryLine(n int, commits ...string) string {
	return changelog.Serialize(changelog.Entry{ID: fmt.Sprintf("%048x", n), Commits: commits, UserFacing: true, Type: "fix", Description: "Fixed the notes"}) + "\n"
}

// archiveText is a recorded archive of 0.1.0.
func archiveText(commit, tree string) string {
	return "format_version = 2\nbump = \"minor\"\ninclude = [\"npm\"]\nexclude = []\ndescription = \"The first release\"\nrelease_commit = \"" + commit + "\"\n\n[released_trees]\n\".\" = \"" + tree + "\"\n"
}

// commitAll writes files into the repository and commits every change.
func commitAll(t *testing.T, repo *testsupport.Repo, files map[string]string, message string) string {
	t.Helper()
	for rel, content := range files {
		repo.Write(rel, content)
	}
	repo.Git("add", "-A")
	repo.Git("commit", "-q", "-m", message)
	return repo.Head()
}

// history is one release of portal, as four commits: the project (A), a
// change (B), the release commit of 0.1.0 (R, tagged v0.1.0, carrying its
// released changelog file, whose entry names B), and the archive with the
// generated CHANGELOG.md (S). notes is notes.txt's content, which a scrub
// rewrites.
type history struct {
	A, B, R, S string
}

// buildHistory commits the history onto the checked-out branch, which must
// be empty (a new repository, or an orphan branch with nothing staged).
func buildHistory(t *testing.T, repo *testsupport.Repo, notes string) history {
	t.Helper()
	var h history
	base := map[string]string{declarationsPath: portalDeclarations, "package.json": packageJSON("0.1.0"), "notes.txt": notes + "\n"}
	for _, m := range manifests {
		base[m] = "owner = \"rlsbl\"\n"
	}
	h.A = commitAll(t, repo, base, "the project")
	h.B = commitAll(t, repo, map[string]string{"notes.txt": notes + "\nmore\n"}, "more notes")
	h.R = commitAll(t, repo, map[string]string{changelogFilePath: entryLine(1, h.B)}, "v0.1.0")
	tree := repo.Git("rev-parse", h.R+"^{tree}")
	repo.Write(archivePath, archiveText(h.R, tree))
	document, err := changelog.Document(repo.Dir, "portal", nil)
	if err != nil {
		t.Fatal(err)
	}
	h.S = commitAll(t, repo, map[string]string{"CHANGELOG.md": document}, "archive 0.1.0")
	return h
}

// scrubFixture is a repository whose history holds a secret, released and
// pushed to a bare origin, with a rewritten history prepared beside it the
// fake safegit's scrub moves the branch and the tag onto: the secret
// replaced, the changelog's commit id remapped, and the archive left naming
// the old release commit, as safegit leaves it.
type scrubFixture struct {
	repo *testsupport.Repo
	bare string
	old  history
	new  history
}

func newScrubFixture(t *testing.T) *scrubFixture {
	t.Helper()
	repo := testsupport.NewRepo(t)
	old := buildHistory(t, repo, "token SECRET")
	repo.Git("tag", "v0.1.0", old.R)
	f := &scrubFixture{repo: repo, old: old}
	f.bare = repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main", "v0.1.0")
	// The rewritten history, built on an orphan branch and left unreachable.
	repo.Git("checkout", "-q", "--orphan", "rewritten")
	repo.Git("rm", "-q", "-r", "-f", ".")
	var n history
	base := map[string]string{declarationsPath: portalDeclarations, "package.json": packageJSON("0.1.0"), "notes.txt": "token REDACTED\n"}
	for _, m := range manifests {
		base[m] = "owner = \"rlsbl\"\n"
	}
	n.A = commitAll(t, repo, base, "the project")
	n.B = commitAll(t, repo, map[string]string{"notes.txt": "token REDACTED\nmore\n"}, "more notes")
	n.R = commitAll(t, repo, map[string]string{changelogFilePath: entryLine(1, n.B)}, "v0.1.0")
	tree := repo.Git("rev-parse", old.R+"^{tree}")
	repo.Write(archivePath, archiveText(old.R, tree))
	document, err := changelog.Document(repo.Dir, "portal", nil)
	if err != nil {
		t.Fatal(err)
	}
	n.S = commitAll(t, repo, map[string]string{"CHANGELOG.md": document}, "archive 0.1.0")
	repo.Git("checkout", "-q", "-f", "main")
	repo.Git("branch", "-q", "-D", "rewritten")
	f.new = n
	return f
}

// rewrites is the fixture's commit map.
func (f *scrubFixture) rewrites() map[string]string {
	return map[string]string{f.old.A: f.new.A, f.old.B: f.new.B, f.old.R: f.new.R, f.old.S: f.new.S}
}

// envelope is safegit's --json document for a scrub with payload.
func envelope(t *testing.T, payload any) string {
	t.Helper()
	data, err := json.Marshal(map[string]any{
		"interface_version": 3, "app": "safegit", "app_version": "0.31.0", "command": "scrub match", "exit_code": 0,
		"payload": payload, "output": nil, "dry_run": false, "writes": nil, "preview": nil, "preview_error": nil, "diagnostics": []any{},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// rewritePayload is the payload of the fixture's rewrite.
func (f *scrubFixture) rewritePayload(cleanupOK bool) map[string]any {
	return map[string]any{
		"version": 1, "dry_run": false, "pattern": "SECRET",
		"rewrites":          f.rewrites(),
		"tags":              []map[string]any{{"refname": "refs/tags/v0.1.0", "old_sha": f.old.R, "new_sha": f.new.R, "annotated": false}},
		"commits_rewritten": 4,
		"old_head":          f.old.S,
		"new_head":          f.new.S,
		"cleanup_ok":        cleanupOK,
	}
}

// rewriteScript is the shell the fake safegit's scrub runs to perform the
// fixture's rewrite: the branch and the tag moved, the working tree synced.
func (f *scrubFixture) rewriteScript() string {
	return fmt.Sprintf("git update-ref refs/heads/main %s\ngit reset -q --hard %s\ngit tag -f v0.1.0 %s >/dev/null\n", f.new.S, f.new.S, f.new.R)
}

// Safegit is a fake safegit for one test: `--version` prints the version
// given, `commit` commits with git as safegit would, and a scrub runs the
// test's script and prints the test's stdout with its exit status. Every
// invocation is recorded.
type Safegit struct {
	t   *testing.T
	dir string
}

const fakeSafegit = `#!/bin/sh
here="${0%/*}"
printf '%s\n' "$*" >> "$here/calls.txt"
if [ "$1" = --version ]; then
	cat "$here/version.txt"
	exit 0
fi
if [ "$1" = --approve-consequential ] && [ "$2" = scrub ]; then
	if [ -f "$here/scrub.sh" ]; then
		sh "$here/scrub.sh" >&2 || exit 1
		rm -f "$here/scrub.sh"
	fi
	cat "$here/stdout.txt"
	exit "$(cat "$here/exit.txt")"
fi
if [ "$1" != commit ]; then
	echo "fake safegit: unexpected $*" >&2
	exit 97
fi
shift
trailer=""
message=""
while [ $# -gt 0 ]; do
	case "$1" in
	--trailer) trailer="$2"; shift 2 ;;
	-m) message="$2"; shift 2 ;;
	--) shift; break ;;
	*) echo "fake safegit: unexpected argument $1" >&2; exit 97 ;;
	esac
done
git add -A -- "$@" || exit 1
if [ -n "$trailer" ]; then
	exec git commit -q --trailer "$trailer" -m "$message"
fi
exec git commit -q -m "$message"
`

// newSafegit puts the fake first on PATH for the rest of the test.
func newSafegit(t *testing.T, version string) *Safegit {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "safegit"), []byte(fakeSafegit), 0o755); err != nil {
		t.Fatal(err)
	}
	s := &Safegit{t: t, dir: dir}
	s.write("version.txt", "safegit "+version+"\n")
	s.Answer("", "", 0)
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return s
}

func (s *Safegit) write(name, content string) {
	s.t.Helper()
	if err := os.WriteFile(filepath.Join(s.dir, name), []byte(content), 0o644); err != nil {
		s.t.Fatal(err)
	}
}

// Answer sets what the next scrubs do: script (empty for none) runs once,
// then stdout is printed and exit returned.
func (s *Safegit) Answer(script, stdout string, exit int) {
	s.t.Helper()
	if script != "" {
		s.write("scrub.sh", script)
	}
	s.write("stdout.txt", stdout)
	s.write("exit.txt", fmt.Sprintf("%d\n", exit))
}

// Calls are the invocations, space-joined, in order.
func (s *Safegit) Calls() []string {
	s.t.Helper()
	data, err := os.ReadFile(filepath.Join(s.dir, "calls.txt"))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		s.t.Fatal(err)
	}
	return strings.Split(strings.TrimRight(string(data), "\n"), "\n")
}

// Scrubs are the scrub invocations.
func (s *Safegit) Scrubs() []string {
	var out []string
	for _, c := range s.Calls() {
		if strings.HasPrefix(c, "--approve-consequential scrub") {
			out = append(out, c)
		}
	}
	return out
}

// The gh argvs the history rewrites issue for acme/portal.
var (
	ghVersion  = []string{"--version"}
	ghAuth     = []string{"auth", "status", "--hostname", "github.com"}
	ghList     = []string{"release", "list", "--repo", "acme/portal", "--limit", "1000", "--json", "tagName", "--jq", ".[].tagName"}
	ghExists   = []string{"release", "view", "v0.1.0", "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}
	ghBody     = []string{"release", "view", "v0.1.0", "--repo", "acme/portal", "--json", "body", "--jq", ".body"}
	ghLatest   = []string{"release", "view", "--repo", "acme/portal", "--json", "tagName", "--jq", ".tagName"}
	ghRewrite  = []string{"release", "edit", "v0.1.0", "--repo", "acme/portal", "--notes-file", "-", "--title", "v0.1.0", "--prerelease=false"}
	ghEdit     = []string{"release", "edit", "v0.1.0", "--repo", "acme/portal", "--notes-file", "-"}
	ghCreate   = []string{"release", "create", "v0.1.0", "--repo", "acme/portal", "--title", "v0.1.0", "--notes-file", "-", "--verify-tag"}
	ghLoggedIn = []testsupport.GHAnswer{{Args: ghVersion, Stdout: "gh version 2.0.0"}, {Args: ghAuth}}
)

// gh installs the fake gh with the logged-in answers and the answers given.
func gh(t *testing.T, answers ...testsupport.GHAnswer) *testsupport.GH {
	t.Helper()
	return testsupport.FakeGH(t, append(append([]testsupport.GHAnswer(nil), ghLoggedIn...), answers...)...)
}

// run runs fn as a mutating command carrying rlsbl's observe allowlist.
func run(t *testing.T, dryRun bool, fn func(ctx *strictcli.Context) error) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, fn)
}

// requireExit fails the test unless the result exited with code.
func requireExit(t *testing.T, r strictcli.Result, code int) {
	t.Helper()
	if r.ExitCode != code {
		t.Fatalf("exit %d, want %d\nstdout:\n%s\nstderr:\n%s", r.ExitCode, code, r.Stdout, r.Stderr)
	}
}

// requireStderr fails the test unless stderr holds every fragment.
func requireStderr(t *testing.T, r strictcli.Result, fragments ...string) {
	t.Helper()
	for _, f := range fragments {
		if !strings.Contains(r.Stderr, f) {
			t.Fatalf("stderr lacks %q:\n%s", f, r.Stderr)
		}
	}
}

// readFile reads rel in the repository.
func readFile(t *testing.T, repo *testsupport.Repo, rel string) string {
	t.Helper()
	data, err := os.ReadFile(repo.Path(rel))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func exists(repo *testsupport.Repo, rel string) bool {
	_, err := os.Lstat(repo.Path(rel))
	return err == nil
}

// writeJournal writes safegit's rewrite journal with one complete rewrite
// of commitMap.
func writeJournal(t *testing.T, repo *testsupport.Repo, id string, commitMap map[string]string, complete bool) {
	t.Helper()
	start, err := json.Marshal(map[string]any{"phase": "start", "id": id, "op": "scrub match", "reason": "a secret", "created_at": "2026-01-01T00:00:00Z", "old_head": "", "commit_map": commitMap, "pre_rewrite_remotes": map[string]string{}})
	if err != nil {
		t.Fatal(err)
	}
	lines := string(start) + "\n"
	if complete {
		lines += `{"phase":"complete","id":"` + id + `","created_at":"2026-01-01T00:00:01Z","new_head":"","cleanup_ok":true,"cleanup_errors":[]}` + "\n"
	}
	path := filepath.Join(repo.Git("rev-parse", "--path-format=absolute", "--git-common-dir"), "safegit", "rewrite-maps.jsonl")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err := f.WriteString(lines); err != nil {
		t.Fatal(err)
	}
}
