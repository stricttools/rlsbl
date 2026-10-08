package releaseops_test

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// TestMain runs the tests through testsupport.RunTests, which builds the
// fake gh they use.
func TestMain(m *testing.M) {
	os.Exit(testsupport.RunTests(m))
}

const declarationsPath = ".strictmetadata/releasables/releasables.toml"

// slug is the GitHub repository every fixture declares.
const slug = "acme/portal"

// standaloneDeclarations declares the standalone project portal, whose
// Releases live in acme/portal.
const standaloneDeclarations = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "ci"

[[members]]
path = "."
name = "root"
releasable = "portal"
`

// workspaceDeclarations declares a workspace of the releasables widget and
// gadget, one member each, under a dev-node root.
const workspaceDeclarations = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "widget"
tag_format = "{name}@v{version}"
publish_mode = "none"

[[releasables]]
name = "gadget"
tag_format = "{name}@v{version}"
publish_mode = "none"

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
path = "gadget"
name = "gadget"
releasable = "gadget"
`

func packageJSON(name, version string) string {
	return fmt.Sprintf("{\n  \"name\": %q,\n  \"version\": %q\n}\n", name, version)
}

func pyproject(name, version string) string {
	return fmt.Sprintf("[project]\nname = %q\nversion = %q\n", name, version)
}

// entryNumber makes each fixture entry's id distinct.
var entryNumber int

// entryLine is a serialized user-facing changelog entry naming commits.
func entryLine(description string, commits ...string) string {
	entryNumber++
	id := fmt.Sprintf("%048x", entryNumber)
	return changelog.Serialize(changelog.Entry{ID: id, Commits: commits, UserFacing: true, Type: "feature", Description: description}) + "\n"
}

// project is a fixture repository with a bare origin, its releases made
// the way a release makes them.
type project struct {
	*testsupport.Repo
	t    *testing.T
	bare string
	// workspace is whether the declarations declare one.
	workspace bool
	// indexPath is the confidential-name index the fixture's commands
	// read, outside the repository.
	indexPath string
}

// newPortal is the standalone project portal at 0.1.0, carrying the
// manifests named (package.json, pyproject.toml, go.mod with VERSION),
// pushed to its origin, nothing released.
func newPortal(t *testing.T, manifests ...string) *project {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(declarationsPath, standaloneDeclarations)
	paths := []string{declarationsPath}
	for _, m := range manifests {
		switch m {
		case "package.json":
			repo.Write(m, packageJSON("portal", "0.1.0"))
		case "pyproject.toml":
			repo.Write(m, pyproject("portal", "0.1.0"))
		case "go.mod":
			repo.Write(m, "module github.com/acme/portal\n\ngo 1.26\n")
			repo.Write("VERSION", "0.1.0\n")
			paths = append(paths, "VERSION")
		default:
			t.Fatalf("unknown manifest %q", m)
		}
		paths = append(paths, m)
	}
	repo.Commit("the project", paths...)
	p := &project{Repo: repo, t: t, indexPath: filepath.Join(t.TempDir(), "confidential-names.toml")}
	p.bare = repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	return p
}

// newWorkspace is the workspace of widget and gadget at 0.1.0, pushed to its
// origin, nothing released.
func newWorkspace(t *testing.T) *project {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(declarationsPath, workspaceDeclarations)
	paths := []string{declarationsPath}
	for _, name := range []string{"widget", "gadget"} {
		repo.Write(name+"/package.json", packageJSON(name, "0.1.0"))
		repo.Write(".strictmetadata/releases/"+name+"/version", "0.1.0\n")
		paths = append(paths, name+"/package.json", ".strictmetadata/releases/"+name+"/version")
	}
	repo.Commit("the workspace", paths...)
	p := &project{Repo: repo, t: t, workspace: true, indexPath: filepath.Join(t.TempDir(), "confidential-names.toml")}
	p.bare = repo.AddBareRemote("origin")
	repo.Git("push", "-q", "origin", "main")
	return p
}

// tagOf is the tag a release of releasable at v carries.
func (p *project) tagOf(releasable, v string) string {
	if p.workspace {
		return releasable + "@v" + v
	}
	return "v" + v
}

// writeVersionFiles writes v into every version file of releasable that
// exists, and returns the paths written.
func (p *project) writeVersionFiles(releasable, v string) []string {
	var paths []string
	if p.workspace {
		p.Write(releasable+"/package.json", packageJSON(releasable, v))
		p.Write(".strictmetadata/releases/"+releasable+"/version", v+"\n")
		return []string{releasable + "/package.json", ".strictmetadata/releases/" + releasable + "/version"}
	}
	for _, f := range []string{"package.json", "pyproject.toml", "VERSION"} {
		if _, err := os.Stat(p.Path(f)); err != nil {
			continue
		}
		switch f {
		case "package.json":
			p.Write(f, packageJSON("portal", v))
		case "pyproject.toml":
			p.Write(f, pyproject("portal", v))
		case "VERSION":
			p.Write(f, v+"\n")
		}
		paths = append(paths, f)
	}
	return paths
}

// bump commits releasable's version-bump commit of v, with the subject the
// release gives it, and returns it.
func (p *project) bump(releasable, v string) string {
	subject := p.tagOf(releasable, v)
	if p.workspace {
		subject = releasable + ": release v" + v
	}
	return p.Commit(subject+"\n\nAutogenerated: true", p.writeVersionFiles(releasable, v)...)
}

// finalize records the release of v at releaseCommit, tagged there: the
// archive and the released changelog file, committed above it with the
// Autogenerated trailer the way the release finalizes, and everything pushed.
func (p *project) finalize(releasable, v, releaseCommit string) {
	p.t.Helper()
	tag := p.tagOf(releasable, v)
	p.Git("tag", tag, releaseCommit)
	tree := "."
	if p.workspace {
		tree = releasable
	}
	archive := ".strictmetadata/releases/" + releasable + "/v" + v + ".toml"
	p.Write(archive, fmt.Sprintf("format_version = 2\nbump = \"minor\"\ninclude = []\nexclude = []\ndescription = \"release %s\"\nrelease_commit = \"%s\"\n\n[released_trees]\n%q = \"%s\"\n", v, releaseCommit, tree, strings.Repeat("e", 40)))
	dir := changelog.Dir(releasable)
	p.Write(dir+"/"+v+".jsonl", entryLine("shipped in "+v, releaseCommit))
	p.Write(dir+"/unreleased.jsonl", "")
	p.Commit("Finalize "+v+"\n\nAutogenerated: true", archive, dir+"/"+v+".jsonl", dir+"/unreleased.jsonl")
	p.Git("push", "-q", "origin", "main", tag)
}

// release makes the release of v of releasable and returns its release
// commit, the version-bump commit.
func (p *project) release(releasable, v string) string {
	p.t.Helper()
	commit := p.bump(releasable, v)
	p.finalize(releasable, v, commit)
	return commit
}

// read is the content of the repository-relative rel.
func (p *project) read(rel string) string {
	p.t.Helper()
	data, err := os.ReadFile(p.Path(rel))
	if err != nil {
		p.t.Fatal(err)
	}
	return string(data)
}

// exists reports whether rel exists.
func (p *project) exists(rel string) bool {
	_, err := os.Lstat(p.Path(rel))
	return err == nil
}

// originRefs are the refs of the bare origin.
func (p *project) originRefs() map[string]string {
	return testsupport.Refs(p.t, filepath.Clean(p.bare))
}

// The gh argvs the client issues against acme/portal.
var authStatus = testsupport.GHAnswer{Args: []string{"auth", "status", "--hostname", "github.com"}}

func viewArgs(tag string) []string {
	return []string{"release", "view", tag, "--repo", slug, "--json", "tagName", "--jq", ".tagName"}
}

func releaseExists(tag string) testsupport.GHAnswer {
	return testsupport.GHAnswer{Args: viewArgs(tag), Stdout: tag + "\n"}
}

func releaseMissing(tag string) testsupport.GHAnswer {
	return testsupport.GHAnswer{Args: viewArgs(tag), Stderr: "release not found\n", Exit: 1}
}

func rewriteArgs(tag string, prerelease bool) []string {
	flag := "--prerelease=false"
	if prerelease {
		flag = "--prerelease"
	}
	return []string{"release", "edit", tag, "--repo", slug, "--notes-file", "-", "--title", tag, flag}
}

func deleteArgs(tag string) []string {
	return []string{"release", "delete", tag, "--repo", slug, "--yes"}
}

// called reports whether the fake gh received argv.
func called(calls []testsupport.GHCall, argv []string) (testsupport.GHCall, bool) {
	for _, c := range calls {
		if slices.Equal(c.Args, argv) {
			return c, true
		}
	}
	return testsupport.GHCall{}, false
}

// get is a canned GET answer.
func get(url string, status int, body string) testsupport.HTTPAnswer {
	return testsupport.HTTPAnswer{Method: "GET", URL: url, Status: status, Body: body}
}

// npmDocument is the npm package document of portal listing versions.
func npmDocument(versions ...string) testsupport.HTTPAnswer {
	var listed []string
	for _, v := range versions {
		listed = append(listed, fmt.Sprintf("%q:{}", v))
	}
	latest := "0.0.0"
	if len(versions) > 0 {
		latest = versions[len(versions)-1]
	}
	return get("https://registry.npmjs.org/portal", 200, fmt.Sprintf(`{"name":"portal","dist-tags":{"latest":%q},"versions":{%s}}`, latest, strings.Join(listed, ",")))
}

// run runs fn as a mutating command carrying rlsbl's observe allowlist,
// its HTTP requests going to fake (nil: a fake answering nothing).
func run(t *testing.T, fake *testsupport.FakeHTTP, dryRun bool, fn func(ctx *strictcli.Context) error) strictcli.Result {
	t.Helper()
	if fake == nil {
		fake = testsupport.NewFakeHTTP(t)
	}
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes(), HTTPClient: fake.Client()}, fn)
}

// requireExit fails the test unless r exited with code, printing its
// output.
func requireExit(t *testing.T, r strictcli.Result, code int) {
	t.Helper()
	if r.ExitCode != code {
		t.Fatalf("exit %d, want %d\nstdout:\n%s\nstderr:\n%s", r.ExitCode, code, r.Stdout, r.Stderr)
	}
}

func requireContains(t *testing.T, text string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(text, want) {
			t.Fatalf("%q is not in:\n%s", want, text)
		}
	}
}

// fakeNpm puts an npm first on PATH that records each argv and succeeds,
// and returns what it recorded so far.
func fakeNpm(t *testing.T) func() []string {
	t.Helper()
	dir := t.TempDir()
	script := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"${0%/*}/npm-calls.txt\"\n"
	if err := os.WriteFile(filepath.Join(dir, "npm"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return func() []string {
		data, err := os.ReadFile(filepath.Join(dir, "npm-calls.txt"))
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			t.Fatal(err)
		}
		return strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	}
}

// subjects are the subjects of the last n commits, newest first.
func (p *project) subjects(n int) []string {
	return strings.Split(p.Git("log", "-n", fmt.Sprint(n), "--format=%s"), "\n")
}
