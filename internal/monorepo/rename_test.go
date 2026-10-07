package monorepo

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// renameDecls declares a workspace whose widget releasable, tagged
// {name}@v{version}, holds the member at packages/widget.
const renameDecls = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

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

// widgetRecord is the record of widget: active, MIT, and named widget since
// the start of the year.
const widgetRecord = `format_version = 1

[[lifecycle]]
subject = "widget"
status = "active"
from = 2026-01-01
reason = "released"

[[licenses]]
subject = "widget"
license = "MIT"
from = 2026-01-01
reason = "published"

[[identities]]
subject = "widget"
facet = "releasable-name"
value = "widget"
registry = ""
tag_patterns = ["widget@v*"]
from = 2026-01-01
reason = "its name since it began"
`

// renameFixture is a workspace of decls whose widget released 0.1.0 from
// its first commit, tagged widget@v0.1.0 there and archived after it, with
// the record and an origin remote.
func renameFixture(t *testing.T, decls, record string) (repo *testsupport.Repo, released string) {
	t.Helper()
	repo = newWorkspace(t, decls, map[string]string{
		"packages/widget/go.mod":                                  "module github.com/acme/widget\n\ngo 1.26\n",
		declarations.ReleaseStateDir + "/.gitignore":              "*\n!.gitignore\n",
		declarations.VersionFile("widget"):                        "0.1.0\n",
		declarations.ChangelogDir("widget") + "/unreleased.jsonl": "",
		lifecycle.ManifestFile:                                    "owner = \"strictspec\"\n",
		declarations.ReleasesRoot + "/manifest.toml":              "owner = \"rlsbl\"\n",
		declarations.ChangelogRoot + "/manifest.toml":             "owner = \"rlsbl\"\n",
		lifecycle.RecordFile:                                      record,
	})
	released = repo.Head()
	repo.Git("tag", "widget@v0.1.0", released)
	archive := declarations.ReleasesDir("widget") + "/v0.1.0.toml"
	repo.Write(archive, "format_version = 2\nbump = \"minor\"\ninclude = [\"go\"]\nexclude = []\ndescription = \"the first release\"\nrelease_commit = \""+released+"\"\n\n[released_trees]\n\"packages/widget\" = \""+strings.Repeat("e", 40)+"\"\n")
	repo.Commit("archive widget 0.1.0", archive)
	repo.AddBareRemote("origin")
	return repo, released
}

// renaming renames old to new on today with a router regeneration that
// records the declarations it was given.
func renaming(repo *testsupport.Repo, old, new string, dryRun bool, out *Renamed, synced *[]string) func(e *strictcli.Effects, say func(string)) error {
	return func(e *strictcli.Effects, say func(string)) error {
		ws, err := workspace.Load(repo.Dir)
		if err != nil {
			return err
		}
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		indexPath, err := index.DefaultPath()
		if err != nil {
			return err
		}
		got, err := RenameReleasable(e, r, ws, RenameRequest{
			Old: old, New: new, DryRun: dryRun, Now: today, Say: say, IndexPath: indexPath,
			Sync: func(renamed *workspace.Workspace) (workflows.SyncResult, error) {
				if synced != nil {
					for _, rel := range renamed.Releasables() {
						*synced = append(*synced, rel.Name)
					}
				}
				return workflows.SyncResult{}, nil
			},
		})
		if out != nil {
			*out = got
		}
		return err
	}
}

func identityOf(rec *lifecycle.Record, subject string) (lifecycle.Identity, bool) {
	for _, id := range rec.Identities() {
		if id.Subject == subject && id.Facet == lifecycle.FacetReleasableName {
			return id, true
		}
	}
	return lifecycle.Identity{}, false
}

func TestRenameMovesTheStateAndTheSubjectsAndAliasesTheCurrentVersion(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo, released := renameFixture(t, renameDecls, widgetRecord)
	indexPath, err := index.DefaultPath()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(indexPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(indexPath, []byte("format_version = 1\n\n[[repositories]]\nsubjects = [\"widget\"]\nnames = [\"widget\"]\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var out Renamed
	var synced []string
	mustRun(t, strictcli.EffectMutating, false, renaming(repo, "widget", "portal", false, &out, &synced))

	// The index entry the old name keyed is keyed by the new one.
	idx, err := index.Load(indexPath)
	if err != nil {
		t.Fatal(err)
	}
	if entries := idx.Entries(); len(entries) != 1 || !slices.Equal(entries[0].Subjects, []string{"portal"}) || !slices.Equal(entries[0].Names, []string{"widget"}) {
		t.Fatalf("index entries %+v", entries)
	}

	ws := load(t, repo)
	if _, ok := ws.Declarations.Releasable("portal"); !ok || ws.Declarations.Members[1].Releasable != "portal" {
		t.Fatalf("declarations: %+v", ws.Declarations)
	}
	if !slices.Equal(synced, []string{"portal"}) {
		t.Errorf("the routers were regenerated from %v", synced)
	}
	if exists(repo, declarations.ReleasesDir("widget")) || exists(repo, declarations.ChangelogDir("widget")) || !exists(repo, declarations.ChangelogDir("portal")+"/unreleased.jsonl") {
		t.Error("the state did not move")
	}
	archive := readText(t, repo, declarations.ReleasesDir("portal")+"/v0.1.0.toml")
	if !strings.Contains(archive, `shipped_as = "widget@v0.1.0"`) || !slices.Equal(out.ShippedAs, []string{"0.1.0"}) {
		t.Errorf("shipped_as %v:\n%s", out.ShippedAs, archive)
	}

	rec, err := lifecycle.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	old, _ := identityOf(rec, "widget")
	renamed, ok := identityOf(rec, "portal")
	if old.Open() || !old.Until.Equal(today) || !ok || !renamed.Open() || renamed.Value != "portal" || !slices.Equal(renamed.TagPatterns, []string{"portal@v*"}) || !renamed.From.Equal(today) {
		t.Errorf("identities %+v", rec.Identities())
	}
	if l, ok := rec.LicenseOn("portal", today); !ok || l.License != "MIT" {
		t.Errorf("the license did not move: %+v", rec.Licenses())
	}
	if l, ok := rec.LifecycleOn("portal", today); !ok || l.Status != lifecycle.StatusActive {
		t.Errorf("the lifecycle did not move: %+v", rec.Lifecycle())
	}
	if err := rec.Validate(today, []string{"portal", "root", "widget"}); err != nil {
		t.Errorf("the record is refused: %v", err)
	}

	if out.AliasTag != "portal@v0.1.0" || out.AliasStatus != AliasCreated {
		t.Errorf("alias %q: %s", out.AliasTag, out.AliasStatus)
	}
	if got := repo.Git("rev-parse", "portal@v0.1.0^{commit}"); got != released {
		t.Errorf("the alias names %s, not %s", got, released)
	}
	if remote := repo.Git("ls-remote", "--tags", "origin", "portal@v0.1.0"); !strings.Contains(remote, released) {
		t.Errorf("the alias was not pushed: %q", remote)
	}
	if record := readText(t, repo, declarations.TransitionsFile); !strings.Contains(record, `"alias_tag":"portal@v0.1.0"`) || !strings.Contains(record, `"aliased_tag":"widget@v0.1.0"`) {
		t.Errorf("the alias is not recorded:\n%s", record)
	}

	subjects := strings.Split(repo.Git("log", "-3", "--format=%s"), "\n")
	if !slices.Equal(subjects, []string{aliasCommitMessage("portal"), shippedAsCommitMessage("widget", "portal"), RenameCommitMessage("widget", "portal")}) {
		t.Errorf("commits %q", subjects)
	}
	if trailer := repo.Git("log", "-1", "--skip=2", "--format=%(trailers:key=Autogenerated,valueonly)"); trailer != "" {
		t.Errorf("the rename commit carries the Autogenerated trailer")
	}
	if out.RenameCommit != repo.Git("rev-parse", "HEAD~2") {
		t.Errorf("the rename commit is %s", out.RenameCommit)
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("the rename left changes:\n%s", status)
	}
}

// Running the rename again after it finished completes nothing and makes
// no commit.
func TestARenameRunAgainChangesNothing(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo, _ := renameFixture(t, renameDecls, widgetRecord)
	mustRun(t, strictcli.EffectMutating, false, renaming(repo, "widget", "portal", false, nil, nil))
	head := repo.Head()
	var out Renamed
	mustRun(t, strictcli.EffectMutating, false, renaming(repo, "widget", "portal", false, &out, nil))
	if repo.Head() != head || !out.Resumed || out.AliasStatus != AliasAlreadyDone || len(out.ShippedAs) != 0 {
		t.Fatalf("head moved %v, %+v", repo.Head() != head, out)
	}
}

// A push that fails leaves the local rename committed, and running the
// rename again once the remote is reachable finishes it.
func TestAnInterruptedRenameIsCompletedByRunningItAgain(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo, released := renameFixture(t, renameDecls, widgetRecord)
	repo.Git("remote", "remove", "origin")
	mustFail(t, strictcli.EffectMutating, false, renaming(repo, "widget", "portal", false, nil, nil), "origin")
	if _, ok := load(t, repo).Declarations.Releasable("portal"); !ok {
		t.Fatal("the local rename was not kept")
	}
	repo.AddBareRemote("origin")
	var out Renamed
	mustRun(t, strictcli.EffectMutating, false, renaming(repo, "widget", "portal", false, &out, nil))
	if !out.Resumed || out.AliasStatus != AliasCreated {
		t.Fatalf("%+v", out)
	}
	if remote := repo.Git("ls-remote", "--tags", "origin", "portal@v0.1.0"); !strings.Contains(remote, released) {
		t.Errorf("the alias was not pushed: %q", remote)
	}
}

// A tag format without {name} keeps every tag's spelling: no shipped_as, no
// alias, no push, and the identities keep their tag namespace.
func TestANameOnlyRenameLeavesTheTagsAlone(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	decls := strings.Replace(renameDecls, `tag_format = "{name}@v{version}"`, `tag_format = "packages/widget/v{version}"`, 1)
	record := strings.Replace(widgetRecord, `tag_patterns = ["widget@v*"]`, `tag_patterns = ["packages/widget/v*"]`, 1)
	repo, _ := renameFixture(t, decls, record)
	var out Renamed
	mustRun(t, strictcli.EffectMutating, false, renaming(repo, "widget", "portal", false, &out, nil))
	if out.NameInFormat || out.AliasTag != "" || len(out.ShippedAs) != 0 {
		t.Fatalf("%+v", out)
	}
	if strings.Contains(readText(t, repo, declarations.ReleasesDir("portal")+"/v0.1.0.toml"), "shipped_as") {
		t.Error("a name-only rename recorded shipped_as")
	}
	rec, err := lifecycle.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if id, ok := identityOf(rec, "portal"); !ok || !slices.Equal(id.TagPatterns, []string{"packages/widget/v*"}) {
		t.Errorf("identities %+v", rec.Identities())
	}
	if subject := repo.Git("log", "-1", "--format=%s"); subject != RenameCommitMessage("widget", "portal") {
		t.Errorf("the last commit is %q", subject)
	}
	if lines := RenameClosing(load(t, repo), "widget", "portal", out); strings.Contains(strings.Join(lines, "\n"), "Alias tag") {
		t.Errorf("the closing names an alias:\n%s", strings.Join(lines, "\n"))
	}
}

func TestADryRunRenameWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo, _ := renameFixture(t, renameDecls, widgetRecord)
	head := repo.Head()
	text := mustRun(t, strictcli.EffectMutating, true, renaming(repo, "widget", "portal", true, nil, nil))
	if repo.Head() != head || repo.Git("status", "--porcelain") != "" || !strings.Contains(text, `Would rename the releasable "widget" to "portal"`) || !strings.Contains(text, "push it to origin") {
		t.Fatalf("the dry run wrote, or said:\n%s", text)
	}
}

// Each refusal is made before anything is written, and doing what it names
// clears it (checked by a dry run, which runs every refusal).
func TestRenameRefusals(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo, _ := renameFixture(t, renameDecls, widgetRecord)
	head := repo.Head()
	dry := func(old, new string) func(e *strictcli.Effects, say func(string)) error {
		return renaming(repo, old, new, true, nil, nil)
	}

	mustFail(t, strictcli.EffectMutating, true, dry("widget", "widget"), "both")
	mustFail(t, strictcli.EffectMutating, true, dry("gadget", "portal"), `no releasable is named "gadget"`, "widget")
	mustFail(t, strictcli.EffectMutating, true, dry("widget", "root"), "the name of the member")
	mustFail(t, strictcli.EffectMutating, true, dry("widget", ".portal"), "starts with a dot")

	repo.Write("notes.txt", "uncommitted\n")
	mustFail(t, strictcli.EffectMutating, true, dry("widget", "portal"), "uncommitted changes", "notes.txt")
	repo.Commit("the notes", "notes.txt")
	mustRun(t, strictcli.EffectMutating, true, dry("widget", "portal"))

	inProgress := repo.Path(declarations.RunStateDir("widget") + "/in-progress.toml")
	testsupport.WriteFile(t, inProgress, "format_version = 1\n")
	mustFail(t, strictcli.EffectMutating, true, dry("widget", "portal"), "a release is in progress for widget", "rlsbl release resume")
	if err := os.Remove(inProgress); err != nil {
		t.Fatal(err)
	}
	mustRun(t, strictcli.EffectMutating, true, dry("widget", "portal"))

	batch := releaserecord.BatchReleaseFilePath
	repo.Write(batch, "format_version = 2\n\n[releasables.widget]\nbump = \"patch\"\ninclude = [\"go\"]\nexclude = []\ndescription = \"a fix\"\n")
	repo.Commit("the batch release file", batch)
	mustFail(t, strictcli.EffectMutating, true, dry("widget", "portal"), "[releasables.widget]", "delete that table")
	repo.Git("rm", "-q", batch)
	repo.Git("commit", "-q", "-m", "no batch release")
	mustRun(t, strictcli.EffectMutating, true, dry("widget", "portal"))

	withoutIdentity := widgetRecord[:strings.Index(widgetRecord, "[[identities]]")]
	repo.Write(lifecycle.RecordFile, withoutIdentity)
	repo.Commit("a record without the identity", lifecycle.RecordFile)
	mustFail(t, strictcli.EffectMutating, true, dry("widget", "portal"), "no open releasable-name identity of \"widget\"", `tag_patterns = ["widget@v*"]`)
	repo.Write(lifecycle.RecordFile, withoutIdentity+"\n[[identities]]\nsubject = \"widget\"\nfacet = \"releasable-name\"\nvalue = \"widget\"\nregistry = \"\"\ntag_patterns = [\"widget@v*\"]\nfrom = 2026-01-01\nreason = \"recorded by hand\"\n")
	repo.Commit("the identity", lifecycle.RecordFile)
	mustRun(t, strictcli.EffectMutating, true, dry("widget", "portal"))

	withPending := readText(t, repo, lifecycle.RecordFile)
	repo.Write(lifecycle.RecordFile, withPending+"\n[[identities]]\nsubject = \"widget\"\nfacet = \"package-name\"\nvalue = \"widget-client\"\nregistry = \"npm\"\ntag_patterns = [\"widget@v*\"]\neffective_version = \"0.2.0\"\nreason = \"renamed\"\n")
	repo.Commit("a pending identity", lifecycle.RecordFile)
	mustFail(t, strictcli.EffectMutating, true, dry("widget", "portal"), "a pending package-name identity", "delete the pending entry")
	repo.Write(lifecycle.RecordFile, withPending)
	repo.Commit("no pending identity", lifecycle.RecordFile)
	mustRun(t, strictcli.EffectMutating, true, dry("widget", "portal"))

	if _, ok := load(t, repo).Declarations.Releasable("widget"); !ok || repo.Git("rev-list", "--count", head+"..HEAD") != "7" {
		t.Errorf("a refused or previewed rename changed the repository")
	}
}

func TestRenameRefusesANameAnotherReleasableHolds(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	decls := renameDecls + "\n[[members]]\npath = \"apps/gadget\"\nname = \"gadget\"\nreleasable = \"gadget\"\n"
	decls = strings.Replace(decls, "[[members]]\npath = \".\"", "[[releasables]]\nname = \"gadget\"\ntag_format = \"{name}@v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"", 1)
	repo, _ := renameFixture(t, decls, widgetRecord)
	mustFail(t, strictcli.EffectMutating, true, renaming(repo, "widget", "gadget", true, nil, nil), `a releasable named "gadget" is declared already`)
}

// The closing names the changelog entry the rename commit needs when
// coverage asks one, changing into one of the releasable's members.
func TestTheClosingNamesTheEntryTheRenameCommitNeeds(t *testing.T) {
	hygiene.Isolate(t)
	repo, _ := renameFixture(t, renameDecls, widgetRecord)
	ws := load(t, repo)
	commit := strings.Repeat("a", 40)
	lines := strings.Join(RenameClosing(ws, "portal", "widget", Renamed{RenameCommit: commit, NeedsEntry: true, NameInFormat: true, AliasTag: "widget@v0.1.0", AliasStatus: AliasCreated}), "\n")
	if !strings.Contains(lines, "(cd packages/widget && rlsbl changelog add --commits aaaaaaaaaaaa --type breaking --description 'Renamed the releasable portal to widget: releases from now on are tagged under the name widget.')") {
		t.Errorf("closing:\n%s", lines)
	}
	lines = strings.Join(RenameClosing(ws, "portal", "widget", Renamed{RenameCommit: commit}), "\n")
	if strings.Contains(lines, "changelog add") {
		t.Errorf("a rename commit needing no entry is given one:\n%s", lines)
	}
}
