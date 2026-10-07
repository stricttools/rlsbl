package monorepo

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// absorbDecls declares a workspace on GitHub as acme/portal whose dev-node
// root holds widget, a Go module at packages/widget versioned under widget.
const absorbDecls = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "widget"
tag_format = "{name}@v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "packages/widget"
name = "widget"
releasable = "widget"
`

// absorbFiles are the workspace's files.
var absorbFiles = map[string]string{
	"packages/widget/go.mod":                     "module github.com/acme/widget\n\ngo 1.26\n",
	"packages/widget/main.go":                    "package main\n\nfunc main() {}\n",
	declarations.ReleaseStateDir + "/.gitignore": "*\n!.gitignore\n",
}

// gizmoDecls declares a standalone Go project, the releasable gizmo, tagged
// v{version} and publishing nothing.
const gizmoDecls = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]

[[releasables]]
name = "gizmo"
tag_format = "v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
releasable = "gizmo"
`

// gizmoRecord is gizmo's record: MIT, and named gizmo, since the start of
// the year.
const gizmoRecord = `format_version = 1

[[licenses]]
subject = "gizmo"
license = "MIT"
from = 2026-01-01
reason = "published"

[[identities]]
subject = "gizmo"
facet = "releasable-name"
value = "gizmo"
registry = ""
tag_patterns = ["v*"]
from = 2026-01-01
reason = "its name since it began"
`

// gizmoSource is the repository absorbed: gizmo released 0.1.0 from its
// first commit s1, tagged v0.1.0 there, archived and logged; then s2, in its
// unreleased changelog. files replace or add to its first commit's files.
func gizmoSource(t *testing.T, files map[string]string) (src *testsupport.Repo, s1, s2 string) {
	t.Helper()
	src = testsupport.NewRepo(t)
	first := map[string]string{
		declarations.ReleasablesFile: gizmoDecls,
		"go.mod":                     "module github.com/acme/gizmo\n\ngo 1.26\n",
		"main.go":                    "package main\n\nfunc main() {}\n",
		"VERSION":                    "0.1.0\n",
		declarations.ReleasesRoot + "/manifest.toml":  "owner = \"rlsbl\"\n",
		declarations.ChangelogRoot + "/manifest.toml": "owner = \"rlsbl\"\n",
		lifecycle.ManifestFile:                        "owner = \"strictspec\"\n",
		lifecycle.RecordFile:                          gizmoRecord,
	}
	for k, v := range files {
		if v == "" {
			delete(first, k)
			continue
		}
		first[k] = v
	}
	var paths []string
	for rel, content := range first {
		src.Write(rel, content)
		paths = append(paths, rel)
	}
	s1 = src.Commit("gizmo", paths...)
	src.Git("tag", "v0.1.0", s1)
	if _, ok := first[declarations.ReleasablesFile]; ok {
		archive := declarations.ReleasesDir("gizmo") + "/v0.1.0.toml"
		src.Write(archive, archiveText(s1, map[string]string{".": src.Git("rev-parse", s1+"^{tree}")}))
		released := declarations.ChangelogDir("gizmo") + "/0.1.0.jsonl"
		src.Write(released, entryLine("the first feature", s1))
		src.Commit("gizmo 0.1.0's records", archive, released)
	}
	s2 = src.CommitFile("main.go", "package main\n\nfunc main() { println() }\n", "gizmo: a second feature")
	if _, ok := first[declarations.ReleasablesFile]; ok {
		unreleased := declarations.ChangelogDir("gizmo") + "/unreleased.jsonl"
		src.Write(unreleased, entryLine("the second feature", s2))
		src.Commit("gizmo's unreleased entry", unreleased)
	}
	return src, s1, s2
}

// absorbFixture is a workspace of decls and a gizmo source, with a fake
// safegit, a saferm that deletes, and a fake gh answering the scaffold.
func absorbFixture(t *testing.T, decls string, sourceFiles map[string]string) (repo, src *testsupport.Repo, s1, s2 string) {
	t.Helper()
	needFilterRepo(t)
	testsupport.FakeSafegit(t)
	deletingSaferm(t)
	testsupport.FakeGH(t, topicsAnswer)
	src, s1, s2 = gizmoSource(t, sourceFiles)
	repo = newWorkspace(t, decls, absorbFiles)
	// The scaffold of the absorbed member writes its LICENSE, whose
	// copyright holder is git's user.name.
	repo.Git("config", "user.name", "Ada Lovelace")
	return repo, src, s1, s2
}

// absorbing absorbs src at dest, creating the releasable gizmo tagged
// {name}@v{version} unless change says otherwise.
func absorbing(repo, src *testsupport.Repo, dest string, change func(*AbsorbRequest)) func(ctx *strictcli.Context, say func(string)) error {
	return func(ctx *strictcli.Context, say func(string)) error {
		ws, err := workspace.Load(repo.Dir)
		if err != nil {
			return err
		}
		gh, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		req := AbsorbRequest{Source: src.Dir, Dest: dest, TagFormat: "{name}@v{version}", Now: today, Version: "0.132.0", GitHub: gh, Say: say}
		if change != nil {
			change(&req)
		}
		return Absorb(ctx, ws, req)
	}
}

func TestAbsorbBringsARepositoryInAsAMemberWithItsRecords(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, s1, s2 := absorbFixture(t, absorbDecls, nil)
	srcHead := src.Head()
	text := mustConvert(t, false, absorbing(repo, src, "packages/gizmo", nil))

	// The history arrived under the member's path, merged with trailers.
	if !exists(repo, "packages/gizmo/main.go") || repo.Git("ls-files", "packages/gizmo/go.mod") == "" {
		t.Fatalf("the history did not arrive:\n%s", text)
	}
	merge := repo.Git("log", "--format=%H", "--fixed-strings", "--grep="+absorbMarker("gizmo", "packages/gizmo"))
	if merge == "" || !strings.Contains(repo.Git("log", "-1", "--format=%B", merge), AbsorbSourceTrailer+": "+s1) {
		t.Fatalf("no merge carrying the trailers:\n%s", repo.Git("log", "--format=%H %s"))
	}

	// The tags: under the releasable's scheme at the rewritten commit, the
	// source's own name beside it.
	released := repo.Git("rev-parse", "gizmo@v0.1.0^{commit}")
	if alias := repo.Git("rev-parse", "v0.1.0^{commit}"); alias != released || released == s1 {
		t.Fatalf("gizmo@v0.1.0 at %s, v0.1.0 at %s, the source's s1 %s", released, alias, s1)
	}
	if repo.Git("rev-parse", released+":packages/gizmo") != src.Git("rev-parse", s1+"^{tree}") {
		t.Error("the rewritten release commit does not carry the released tree under the member's path")
	}

	// The declarations and the records.
	ws := load(t, repo)
	m, ok := ws.Declarations.Member("gizmo")
	r, rok := ws.Declarations.Releasable("gizmo")
	if !ok || m.Path != "packages/gizmo" || m.Releasable != "gizmo" || !rok || r.TagFormat != "{name}@v{version}" || r.PublishMode != declarations.PublishNone {
		t.Fatalf("declarations: %+v", ws.Declarations)
	}
	if v := readText(t, repo, declarations.VersionFile("gizmo")); v != "0.1.0\n" {
		t.Errorf("the version file: %q", v)
	}
	a, err := releaserecord.ReadArchive(repo.Dir, declarations.ReleasesDir("gizmo"), version(t, "0.1.0"))
	if err != nil {
		t.Fatal(err)
	}
	if a.ReleaseCommit.Commit != released || a.ReleaseCommit.Trees["packages/gizmo"] == "" || len(a.ReleaseCommit.Trees) != 1 {
		t.Fatalf("the archive's release commit: %+v", a.ReleaseCommit)
	}
	logged, err := changelog.ReadVersion(repo.Dir, declarations.ChangelogDir("gizmo"), version(t, "0.1.0"))
	if err != nil {
		t.Fatal(err)
	}
	if len(logged.Lines) != 1 || logged.Lines[0].Entry.Commits[0] != released {
		t.Fatalf("the released changelog: %+v", logged.Lines)
	}
	unreleased, err := changelog.ReadUnreleased(repo.Dir, declarations.ChangelogDir("gizmo"))
	if err != nil {
		t.Fatal(err)
	}
	if len(unreleased.Lines) != 1 || unreleased.Lines[0].Entry.Commits[0] == s2 || repo.Git("cat-file", "-t", unreleased.Lines[0].Entry.Commits[0]) != "commit" {
		t.Fatalf("the unreleased changelog: %+v", unreleased.Lines)
	}
	for _, gone := range []string{"packages/gizmo/" + declarations.ChangelogDir("gizmo"), "packages/gizmo/" + declarations.ReleasesDir("gizmo")} {
		if exists(repo, gone) {
			t.Errorf("%s was moved, and is still there", gone)
		}
	}
	rec, err := lifecycle.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if l, ok := rec.LicenseOn("gizmo", today); !ok || l.License != "MIT" {
		t.Errorf("the license did not arrive: %+v", rec.Licenses())
	}
	closedURL := false
	for _, id := range rec.Identities() {
		if id.Subject == "gizmo" && id.Facet == lifecycle.FacetRepositoryURL && id.Value == src.Dir && !id.Open() {
			closedURL = true
		}
	}
	if !closedURL {
		t.Errorf("the source is not recorded as the member's closed repository: %+v", rec.Identities())
	}
	if got := strings.Join(eventNames(t, repo.Dir), " "); got != "conversion tag-map release-commit-remap boundary-alias" {
		t.Errorf("the transition record: %s", got)
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("the absorb left changes uncommitted:\n%s", status)
	}

	// The source and the scratch clone.
	if src.Head() != srcHead || src.Git("status", "--porcelain") != "" {
		t.Error("the source repository changed")
	}
	if _, err := os.Stat(filepath.Join(repo.Dir, ".git", "rlsbl", "absorb-gizmo")); !os.IsNotExist(err) {
		t.Errorf("the working clone was left: %v", err)
	}
}

func TestADryRunOfAnAbsorbPrintsThePlanAndWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	head := repo.Head()
	text := mustConvert(t, true, absorbing(repo, src, "packages/gizmo", nil))
	for _, want := range []string{
		"source: rewrite-history",
		"releasable: create-releasable",
		"tags: import-tags",
		"v0.1.0 -> gizmo@v0.1.0",
		"boundary alias: v0.1.0 is created beside gizmo@v0.1.0",
		"released changelogs: 0.1.0",
		"workspace: register-member",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("the plan lacks %q:\n%s", want, text)
		}
	}
	if repo.Head() != head || repo.Git("status", "--porcelain") != "" || exists(repo, "packages/gizmo") {
		t.Error("the dry run changed the workspace")
	}
}

func TestRunningAnAbsorbAgainChangesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	mustConvert(t, false, absorbing(repo, src, "packages/gizmo", nil))
	head := repo.Head()
	text := mustConvert(t, false, absorbing(repo, src, "packages/gizmo", nil))
	if repo.Head() != head {
		t.Fatalf("the second run committed:\n%s", repo.Git("log", "--format=%s", head+"..HEAD"))
	}
	if !strings.Contains(text, "recorded already") {
		t.Errorf("the second run recorded the absorb again:\n%s", text)
	}
}

func TestAnAbsorbStoppedAfterItsMergeIsCompletedByRunningItAgain(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	mustConvert(t, false, absorbing(repo, src, "packages/gizmo", nil))
	merge := repo.Git("log", "--format=%H", "--fixed-strings", "--grep="+absorbMarker("gizmo", "packages/gizmo"))
	repo.Git("reset", "-q", "--hard", merge)
	text := mustConvert(t, false, absorbing(repo, src, "packages/gizmo", nil))
	if !strings.Contains(text, "merged by") {
		t.Errorf("the run merged again:\n%s", text)
	}
	ws := load(t, repo)
	if _, ok := ws.Declarations.Member("gizmo"); !ok {
		t.Fatal("the run did not complete the declarations")
	}
	if readText(t, repo, declarations.VersionFile("gizmo")) != "0.1.0\n" || !exists(repo, declarations.ReleasesDir("gizmo")+"/v0.1.0.toml") {
		t.Fatal("the run did not complete the records")
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("changes left uncommitted:\n%s", status)
	}
}

// absorbRefusedThenCleared checks that a dry run of the absorb is refused
// with want, and passes once fix ran with the request changed by after.
func absorbRefusedThenCleared(t *testing.T, repo, src *testsupport.Repo, dest string, before func(*AbsorbRequest), want string, fix func(), after func(*AbsorbRequest)) {
	t.Helper()
	mustRefuse(t, true, absorbing(repo, src, dest, before), want)
	if fix != nil {
		fix()
	}
	mustConvert(t, true, absorbing(repo, src, dest, after))
}

func TestAbsorbRefusesADirtySource(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	src.Write("notes.txt", "draft\n")
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", nil, "uncommitted changes", func() {
		src.Commit("the notes", "notes.txt")
	}, nil)
}

func TestAbsorbRefusesADestinationOnDisk(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	repo.CommitFile("packages/gizmo/README.md", "taken\n", "the path is taken")
	mustRefuse(t, true, absorbing(repo, src, "packages/gizmo", nil), "exists on disk already")
	mustConvert(t, true, absorbing(repo, src, "libs/gizmo", nil))
}

func TestAbsorbRefusesAMemberNameTaken(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	absorbRefusedThenCleared(t, repo, src, "libs/widget", nil, `is named "widget" already`, nil, func(r *AbsorbRequest) { r.Name = "gizmo" })
}

func TestAbsorbRefusesCreatingAReleasableThatIsDeclared(t *testing.T) {
	hygiene.Isolate(t)
	decls := absorbDecls + "\n[[releasables]]\nname = \"gadget\"\ntag_format = \"{name}@v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \"apps/gadget\"\nname = \"gadgetapp\"\nreleasable = \"gadget\"\n"
	repo, src, _, _ := absorbFixture(t, decls, nil)
	repo.CommitFile("apps/gadget/go.mod", "module github.com/acme/gadget\n\ngo 1.26\n", "gadget")
	absorbRefusedThenCleared(t, repo, src, "libs/gadget", nil, "pass --releasable gadget to join it", nil, func(r *AbsorbRequest) {
		r.Releasable, r.TagFormat = "gadget", ""
	})
}

func TestAbsorbRefusesAMissingTagFormat(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", func(r *AbsorbRequest) { r.TagFormat = "" }, "--tag-format", nil, nil)
}

func TestAbsorbRefusesATagFormatWhenJoining(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", func(r *AbsorbRequest) { r.Releasable = "widget" }, "drop them", nil, func(r *AbsorbRequest) {
		r.Releasable, r.TagFormat = "widget", ""
	})
}

func TestAbsorbRefusesATagTheWorkspaceHolds(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	repo.Git("tag", "gizmo@v0.1.0", repo.Head())
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", nil, "exists in this repository already", func() {
		repo.Git("tag", "-d", "gizmo@v0.1.0")
	}, nil)
}

func TestAbsorbRefusesABoundaryAliasTheWorkspaceHolds(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	repo.Git("tag", "v0.1.0", repo.Head())
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", nil, "is kept beside", func() {
		repo.Git("tag", "-d", "v0.1.0")
	}, nil)
}

func TestAbsorbRefusesAVersionTheJoinedReleasableReleased(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	repo.CommitFile(declarations.ChangelogDir("widget")+"/0.1.0.jsonl", "", "widget released 0.1.0")
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", func(r *AbsorbRequest) { r.Releasable, r.TagFormat = "widget", "" }, "has released 0.1.0 already", nil, nil)
}

func TestAbsorbRefusesASourceInTheOldLayout(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, map[string]string{
		declarations.ReleasablesFile: "",
		lifecycle.RecordFile:         "",
		lifecycle.ManifestFile:       "",
		".rlsbl/config.json":         "{}\n",
	})
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", func(r *AbsorbRequest) { r.PublishMode = "none" }, "rlsbl migrate records", func() {
		src.Git("rm", "-q", "-r", ".rlsbl")
		src.CommitFile(declarations.ReleasablesFile, gizmoDecls, "the records in the new layout")
	}, nil)
}

func TestAbsorbRefusesAWorkspaceSource(t *testing.T) {
	hygiene.Isolate(t)
	workspaceSource := "format_version = 1\nrepository_layout = \"workspace\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"gizmo\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"gizmo\"\n"
	repo, src, _, _ := absorbFixture(t, absorbDecls, map[string]string{declarations.ReleasablesFile: workspaceSource})
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", nil, "is a workspace", func() {
		src.CommitFile(declarations.ReleasablesFile, gizmoDecls, "a standalone project")
	}, nil)
}

func TestAbsorbRefusesASourceWithoutAVersion(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, map[string]string{
		declarations.ReleasablesFile: "",
		lifecycle.RecordFile:         "",
		lifecycle.ManifestFile:       "",
		"VERSION":                    "",
	})
	src.Git("tag", "-d", "v0.1.0")
	bare := func(r *AbsorbRequest) { r.PublishMode = "none" }
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", bare, "cannot be decided", func() {
		src.CommitFile("VERSION", "0.3.0\n", "state the version")
	}, bare)
}

func TestAbsorbRefusesASourceRecordWithCodenames(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, map[string]string{
		lifecycle.RecordFile: "format_version = 1\ncodenames = [\"bluebird\"]\n",
	})
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", nil, "codenames", func() {
		src.CommitFile(lifecycle.RecordFile, gizmoRecord, "no codenames")
	}, nil)
}

func TestAbsorbRefusesTwoSourceTagsOfOneVersion(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, s1, _ := absorbFixture(t, absorbDecls, map[string]string{
		declarations.ReleasablesFile: "",
		lifecycle.RecordFile:         "",
		lifecycle.ManifestFile:       "",
	})
	src.Git("tag", "gizmo@v0.1.0", s1)
	bare := func(r *AbsorbRequest) { r.PublishMode = "none" }
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", bare, "both carry the version 0.1.0", func() {
		src.Git("tag", "-d", "gizmo@v0.1.0")
	}, bare)
}

func TestARunNamingAnotherReleasableIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	mustConvert(t, false, absorbing(repo, src, "packages/gizmo", nil))
	merge := repo.Git("log", "--format=%H", "--fixed-strings", "--grep="+absorbMarker("gizmo", "packages/gizmo"))
	repo.Git("reset", "-q", "--hard", merge)
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", func(r *AbsorbRequest) { r.Releasable, r.TagFormat = "widget", "" }, "drop --releasable", nil, nil)
}

func TestAnotherRepositoryAtAnAbsorbedPathIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	mustConvert(t, false, absorbing(repo, src, "packages/gizmo", nil))
	// Another repository at 0.2.0, so its tags hold no boundary alias.
	other, _, _ := gizmoSource(t, map[string]string{"main.go": "package main\n\n// another\nfunc main() {}\n", "VERSION": "0.2.0\n"})
	mustRefuse(t, true, absorbing(repo, other, "packages/gizmo", nil), "from another repository")
	mustConvert(t, true, absorbing(repo, other, "libs/gizmo2", func(r *AbsorbRequest) { r.Name = "gizmo2" }))
}
