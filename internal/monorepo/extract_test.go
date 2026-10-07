package monorepo

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// extractDecls declares a workspace whose dev-node root holds widget at
// packages/widget and gadget at apps/gadget, each versioned under a
// releasable of its own tagged {name}@v{version}, publishing nothing.
const extractDecls = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

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
path = "packages/widget"
name = "widget"
releasable = "widget"

[[members]]
path = "apps/gadget"
name = "gadget"
releasable = "gadget"
`

// extractFiles are the files of the extract fixture's first commit.
func extractFiles() map[string]string {
	return map[string]string{
		"packages/widget/package.json":                "{\n  \"name\": \"widget\",\n  \"version\": \"0.1.0\"\n}\n",
		"apps/gadget/package.json":                    "{\n  \"name\": \"gadget\",\n  \"version\": \"0.1.0\"\n}\n",
		declarations.ReleaseStateDir + "/.gitignore":  "*\n!.gitignore\n",
		declarations.VersionFile("widget"):            "0.1.0\n",
		declarations.VersionFile("gadget"):            "0.1.0\n",
		declarations.ReleasesRoot + "/manifest.toml":  "owner = \"rlsbl\"\n",
		declarations.ChangelogRoot + "/manifest.toml": "owner = \"rlsbl\"\n",
		lifecycle.ManifestFile:                        "owner = \"strictspec\"\n",
		lifecycle.RecordFile:                          widgetRecord,
	}
}

// extractFixture is a workspace of decls (with more files) whose widget
// released 0.1.0 from w1, tagged widget@v0.1.0 there (gadget@v0.1.0 too),
// archived and logged; then widget's w2 and a gadget commit, both in
// widget's unreleased changelog. It installs a fake safegit and a saferm
// that deletes.
func extractFixture(t *testing.T, decls string, more map[string]string) (repo *testsupport.Repo, w1, w2 string) {
	t.Helper()
	needFilterRepo(t)
	testsupport.FakeSafegit(t)
	deletingSaferm(t)
	files := extractFiles()
	for k, v := range more {
		files[k] = v
	}
	repo = newWorkspace(t, decls, files)
	w1 = repo.CommitFile("packages/widget/index.js", "one\n", "widget: the first feature")
	repo.Git("tag", "widget@v0.1.0", w1)
	repo.Git("tag", "gadget@v0.1.0", w1)
	tree := repo.Git("rev-parse", w1+":packages/widget")
	archive := declarations.ReleasesDir("widget") + "/v0.1.0.toml"
	repo.Write(archive, archiveText(w1, map[string]string{"packages/widget": tree}))
	released := declarations.ChangelogDir("widget") + "/0.1.0.jsonl"
	repo.Write(released, entryLine("the first feature", w1))
	repo.Commit("widget 0.1.0's records", archive, released)
	w2 = repo.CommitFile("packages/widget/index.js", "two\n", "widget: the second feature")
	g := repo.CommitFile("apps/gadget/index.js", "gadget\n", "gadget: a feature")
	unreleased := declarations.ChangelogDir("widget") + "/unreleased.jsonl"
	repo.Write(unreleased, entryLine("the second feature", w2)+entryLine("a commit of a member that stays", g))
	repo.Commit("widget's unreleased entries", unreleased)
	return repo, w1, w2
}

// extracting extracts name to target on today, with a router regeneration
// that records the releasables it was given (and fails when failSync).
func extracting(repo *testsupport.Repo, name, target string, synced *[]string, failSync bool) func(ctx *strictcli.Context, say func(string)) error {
	return func(ctx *strictcli.Context, say func(string)) error {
		ws, err := workspace.Load(repo.Dir)
		if err != nil {
			return err
		}
		return Extract(ctx, ws, ExtractRequest{
			Releasable: name,
			Target:     target,
			Now:        today,
			Say:        say,
			Sync: func(remaining *workspace.Workspace) (workflows.SyncResult, error) {
				if failSync {
					return workflows.SyncResult{}, errors.New("the router regeneration broke")
				}
				if synced != nil {
					for _, r := range remaining.Releasables() {
						*synced = append(*synced, r.Name)
					}
				}
				return workflows.SyncResult{}, nil
			},
		})
	}
}

// gitIn runs git in dir and returns its trimmed stdout, failing the test on
// a non-zero exit.
func gitIn(t *testing.T, dir string, args ...string) string {
	t.Helper()
	stdout, stderr, code := testsupport.RunGit(t, dir, args...)
	if code != 0 {
		t.Fatalf("git %s in %s exited %d: %s", strings.Join(args, " "), dir, code, stderr)
	}
	return strings.TrimSpace(stdout)
}

// hasTag reports whether the repository at dir has the tag.
func hasTag(t *testing.T, dir, tag string) bool {
	t.Helper()
	_, _, code := testsupport.RunGit(t, dir, "rev-parse", "--verify", "--quiet", "refs/tags/"+tag)
	return code == 0
}

// eventNames are the transition record's events in order.
func eventNames(t *testing.T, root string) []string {
	t.Helper()
	events, err := releaserecord.ReadEvents(root)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, ev := range events {
		names = append(names, string(ev.EventName()))
	}
	return names
}

func version(t *testing.T, text string) semver.Version {
	t.Helper()
	v, err := semver.Parse(text)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func TestExtractMovesALoneMemberIntoAStandaloneRepository(t *testing.T) {
	hygiene.Isolate(t)
	repo, w1, w2 := extractFixture(t, extractDecls, nil)
	tree := repo.Git("rev-parse", w1+":packages/widget")
	target := filepath.Join(t.TempDir(), "widget")
	var synced []string
	text := mustConvert(t, false, extracting(repo, "widget", target, &synced, false))

	// The history: the member hoisted to the root, its trees unchanged.
	files := gitIn(t, target, "ls-files")
	tracked := strings.Split(files, "\n")
	if !slices.Contains(tracked, "index.js") || !slices.Contains(tracked, "package.json") || strings.Contains(files, "packages/") || strings.Contains(files, "apps/") {
		t.Fatalf("the new repository tracks:\n%s", files)
	}
	d, err := declarations.Load(target)
	if err != nil {
		t.Fatal(err)
	}
	if d.Layout != declarations.LayoutStandalone || len(d.Releasables) != 1 || d.Releasables[0].Name != "widget" || d.Releasables[0].TagFormat != "v{version}" || d.RootMember().Releasable != "widget" {
		t.Fatalf("the new repository's declarations: %+v", d)
	}

	// The tags: renamed, the current version's old name kept beside, and
	// another releasable's deleted.
	released := gitIn(t, target, "rev-parse", "v0.1.0^{commit}")
	if alias := gitIn(t, target, "rev-parse", "widget@v0.1.0^{commit}"); alias != released || released == w1 {
		t.Fatalf("v0.1.0 at %s, widget@v0.1.0 at %s, the source's release commit %s", released, alias, w1)
	}
	if hasTag(t, target, "gadget@v0.1.0") {
		t.Error("another releasable's tag was carried")
	}

	// The records: the release commit moved, its tree rekeyed to the root.
	a, err := releaserecord.ReadArchive(target, declarations.ReleasesDir("widget"), version(t, "0.1.0"))
	if err != nil {
		t.Fatal(err)
	}
	if a.ReleaseCommit.Commit != released || a.ReleaseCommit.Trees["."] != tree || len(a.ReleaseCommit.Trees) != 1 {
		t.Fatalf("the archive's release commit: %+v", a.ReleaseCommit)
	}
	if _, err := os.Stat(filepath.Join(target, filepath.FromSlash(declarations.VersionFile("widget")))); !os.IsNotExist(err) {
		t.Errorf("a standalone repository got a version file: %v", err)
	}
	unreleased, err := changelog.ReadUnreleased(target, declarations.ChangelogDir("widget"))
	if err != nil {
		t.Fatal(err)
	}
	if len(unreleased.Lines) != 1 || unreleased.Lines[0].Entry.Commits[0] == w2 || gitIn(t, target, "cat-file", "-t", unreleased.Lines[0].Entry.Commits[0]) != "commit" {
		t.Fatalf("the unreleased changelog: %+v", unreleased.Lines)
	}
	if !strings.Contains(text, "1 entries dropped") {
		t.Errorf("the dropped entry was not named:\n%s", text)
	}
	if got := strings.Join(eventNames(t, target), " "); got != "conversion tag-map release-commit-remap boundary-alias" {
		t.Errorf("the new repository's transition record: %s", got)
	}
	rec, err := lifecycle.Load(target)
	if err != nil {
		t.Fatal(err)
	}
	if l, ok := rec.LicenseOn("widget", today); !ok || l.License != "MIT" {
		t.Errorf("the license did not move: %+v", rec.Licenses())
	}
	movedNamespace := false
	for _, id := range rec.Identities() {
		if id.Facet == lifecycle.FacetReleasableName && id.Open() && slices.Equal(id.TagPatterns, []string{"v*"}) {
			movedNamespace = true
		}
	}
	if !movedNamespace {
		t.Errorf("the releasable-name identity did not take the new tag namespace: %+v", rec.Identities())
	}
	if status := gitIn(t, target, "status", "--porcelain"); status != "" {
		t.Errorf("the new repository has uncommitted changes:\n%s", status)
	}

	// The source: the member and the releasable gone, in one commit.
	ws := load(t, repo)
	if _, ok := ws.Declarations.Releasable("widget"); ok || exists(repo, "packages/widget") || exists(repo, declarations.ChangelogDir("widget")) || exists(repo, declarations.ReleasesDir("widget")) {
		t.Fatal("the releasable, its member, or its records are still here")
	}
	gadget, _ := ws.Declarations.Member("gadget")
	if !slices.Equal(gadget.InternalDepFloors, []string{"widget"}) {
		t.Errorf("gadget's internal_dep_floors: %v", gadget.InternalDepFloors)
	}
	reg, err := options.Shipped()
	if err != nil {
		t.Fatal(err)
	}
	loaded, err := options.Load(reg, repo.Dir, ws.Declarations)
	if err != nil {
		t.Fatal(err)
	}
	if off, err := loaded.IsOff(options.DepFloors, "apps/gadget"); err != nil || off {
		t.Errorf("rlsbl:dep-floors for gadget: off %v, %v", off, err)
	}
	events, err := releaserecord.ReadEvents(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	departed, ok := events[len(events)-1].(*releaserecord.DepartedGlobsEvent)
	if !ok || !slices.Equal(departed.Globs, []string{"widget@v*"}) || departed.Destination.Repo != target {
		t.Errorf("the departure: %+v", events)
	}
	here, err := lifecycle.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, l := range here.Licenses() {
		if l.Subject == "widget" && (l.Open() || !l.Until.Equal(today)) {
			t.Errorf("widget's license here: %+v", l)
		}
	}
	if subject := repo.Git("log", "-1", "--format=%s"); subject != ExtractCommitMessage("widget") {
		t.Errorf("the last commit is %q", subject)
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("the extract left changes uncommitted:\n%s", status)
	}
	if !slices.Equal(synced, []string{"gadget"}) {
		t.Errorf("the routers were regenerated for %v", synced)
	}
}

func TestADryRunOfAnExtractPrintsThePlanAndWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo, _, _ := extractFixture(t, extractDecls, nil)
	head := repo.Head()
	target := filepath.Join(t.TempDir(), "widget")
	text := mustConvert(t, true, extracting(repo, "widget", target, nil, false))
	for _, want := range []string{
		"releasable: extract-to-standalone",
		"tags: translate-tags",
		"widget@v0.1.0 -> v0.1.0",
		"boundary alias: widget@v0.1.0 is kept beside v0.1.0",
		"deleted, another releasable's: gadget@v0.1.0",
		"source: remove-members",
		"next-steps: operator-actions",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("the plan lacks %q:\n%s", want, text)
		}
	}
	if _, err := os.Lstat(target); !os.IsNotExist(err) {
		t.Errorf("the dry run created %s", target)
	}
	if repo.Head() != head || repo.Git("status", "--porcelain") != "" {
		t.Error("the dry run changed this repository")
	}
}

// twoMemberWidget declares widget with a second member, wcli.
var twoMemberWidget = extractDecls + `
[[members]]
path = "packages/wcli"
name = "wcli"
releasable = "widget"
depends_on = ["widget"]
`

func TestExtractingAReleasableOfSeveralMembersMakesAWorkspace(t *testing.T) {
	hygiene.Isolate(t)
	repo, w1, _ := extractFixture(t, twoMemberWidget, map[string]string{
		"packages/wcli/package.json": "{\n  \"name\": \"wcli\",\n  \"version\": \"0.1.0\"\n}\n",
	})
	tree := repo.Git("rev-parse", w1+":packages/widget")
	target := filepath.Join(t.TempDir(), "widget")
	mustConvert(t, false, extracting(repo, "widget", target, nil, false))
	d, err := declarations.Load(target)
	if err != nil {
		t.Fatal(err)
	}
	if d.Layout != declarations.LayoutWorkspace || d.Releasables[0].TagFormat != "{name}@v{version}" || !d.RootMember().DevNode() {
		t.Fatalf("the new repository's declarations: %+v", d)
	}
	wcli, ok := d.Member("wcli")
	if !ok || wcli.Path != "packages/wcli" || !slices.Equal(wcli.DependsOn, []string{"widget"}) {
		t.Fatalf("wcli: %+v", wcli)
	}
	if !hasTag(t, target, "widget@v0.1.0") || hasTag(t, target, "v0.1.0") {
		t.Error("the tags changed spelling though the format did not")
	}
	if data, err := os.ReadFile(filepath.Join(target, filepath.FromSlash(declarations.VersionFile("widget")))); err != nil || string(data) != "0.1.0\n" {
		t.Errorf("the version file: %q, %v", data, err)
	}
	a, err := releaserecord.ReadArchive(target, declarations.ReleasesDir("widget"), version(t, "0.1.0"))
	if err != nil {
		t.Fatal(err)
	}
	if a.ReleaseCommit.Trees["packages/widget"] != tree || a.ReleaseCommit.Commit == w1 {
		t.Fatalf("the archive's release commit: %+v", a.ReleaseCommit)
	}
}

func TestATreeTheRewriteChangedStopsTheExtractBeforeThisRepositoryChanges(t *testing.T) {
	hygiene.Isolate(t)
	repo, w1, _ := extractFixture(t, extractDecls, nil)
	archive := declarations.ReleasesDir("widget") + "/v0.1.0.toml"
	repo.CommitFile(archive, archiveText(w1, map[string]string{"packages/widget": strings.Repeat("e", 40)}), "a wrong tree")
	head := repo.Head()
	target := filepath.Join(t.TempDir(), "widget")
	mustRefuse(t, false, extracting(repo, "widget", target, nil, false), "does not carry over", strings.Repeat("e", 40))
	if repo.Head() != head || !exists(repo, "packages/widget") {
		t.Fatal("this repository changed")
	}
}

func TestAFailedSourceStepPutsThisRepositoryBack(t *testing.T) {
	hygiene.Isolate(t)
	repo, _, _ := extractFixture(t, extractDecls, nil)
	head := repo.Head()
	target := filepath.Join(t.TempDir(), "widget")
	mustRefuse(t, false, extracting(repo, "widget", target, nil, true), "the router regeneration broke", "back as its last commit holds it")
	if repo.Head() != head || !exists(repo, "packages/widget/index.js") || !exists(repo, declarations.ReleasesDir("widget")+"/v0.1.0.toml") {
		t.Fatal("this repository was not put back")
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("changes left behind:\n%s", status)
	}
	if _, ok := load(t, repo).Declarations.Releasable("widget"); !ok {
		t.Error("the declarations were not put back")
	}
}

// refusedThenCleared checks that a dry run of extracting widget is refused
// with want, and passes once fix ran.
func refusedThenCleared(t *testing.T, repo *testsupport.Repo, target, want string, fix func()) {
	t.Helper()
	mustRefuse(t, true, extracting(repo, "widget", target, nil, false), want)
	fix()
	mustConvert(t, true, extracting(repo, "widget", target, nil, false))
}

func TestExtractRefusesAMemberThatStaysDependingOnOneThatLeaves(t *testing.T) {
	hygiene.Isolate(t)
	decls := strings.Replace(extractDecls, "name = \"gadget\"\nreleasable = \"gadget\"\n", "name = \"gadget\"\nreleasable = \"gadget\"\ndepends_on = [\"widget\"]\n", 1)
	repo, _, _ := extractFixture(t, decls, nil)
	target := filepath.Join(t.TempDir(), "widget")
	refusedThenCleared(t, repo, target, `remove "widget" from the depends_on of the member "gadget"`, func() {
		repo.CommitFile(declarations.ReleasablesFile, extractDecls, "gadget no longer depends on widget")
	})
}

func TestExtractRefusesANestedMemberThatStays(t *testing.T) {
	hygiene.Isolate(t)
	nested := extractDecls + "\n[[members]]\npath = \"packages/widget/plugin\"\nname = \"plugin\"\nreleasable = \"gadget\"\n"
	repo, _, _ := extractFixture(t, nested, map[string]string{
		"packages/widget/plugin/package.json": "{\n  \"name\": \"plugin\",\n  \"version\": \"0.1.0\"\n}\n",
	})
	target := filepath.Join(t.TempDir(), "widget")
	refusedThenCleared(t, repo, target, "lies inside the departing member", func() {
		repo.CommitFile(declarations.ReleasablesFile, strings.Replace(nested, "name = \"plugin\"\nreleasable = \"gadget\"", "name = \"plugin\"\nreleasable = \"widget\"", 1), "plugin leaves with widget")
	})
}

func TestExtractRefusesAReleasableHoldingTheRootMember(t *testing.T) {
	hygiene.Isolate(t)
	rooted := strings.Replace(extractDecls, "name = \"root\"\ndev_only = true\nreleasable = false", "name = \"root\"\nreleasable = \"widget\"", 1)
	repo, _, _ := extractFixture(t, rooted, nil)
	target := filepath.Join(t.TempDir(), "widget")
	refusedThenCleared(t, repo, target, "holds the root member", func() {
		repo.CommitFile(declarations.ReleasablesFile, extractDecls, "the root is a dev node")
	})
}

func TestExtractRefusesATargetThatExists(t *testing.T) {
	hygiene.Isolate(t)
	repo, _, _ := extractFixture(t, extractDecls, nil)
	taken := t.TempDir()
	mustRefuse(t, true, extracting(repo, "widget", taken, nil, false), "exists already")
	mustConvert(t, true, extracting(repo, "widget", filepath.Join(taken, "widget"), nil, false))
}

func TestExtractRefusesUncommittedChanges(t *testing.T) {
	hygiene.Isolate(t)
	repo, _, _ := extractFixture(t, extractDecls, nil)
	target := filepath.Join(t.TempDir(), "widget")
	repo.Write("apps/gadget/notes.txt", "draft\n")
	refusedThenCleared(t, repo, target, "uncommitted changes", func() {
		repo.Commit("the notes", "apps/gadget/notes.txt")
	})
}

func TestExtractRefusesAWaitingBatchRelease(t *testing.T) {
	hygiene.Isolate(t)
	repo, _, _ := extractFixture(t, extractDecls, nil)
	target := filepath.Join(t.TempDir(), "widget")
	repo.CommitFile(releaserecord.BatchReleaseFilePath, "format_version = 2\n", "a batch release")
	refusedThenCleared(t, repo, target, "batch release file", func() {
		repo.Git("rm", "-q", releaserecord.BatchReleaseFilePath)
		repo.Git("commit", "-q", "-m", "no batch release")
	})
}

func TestExtractRefusesOptionsEntriesScopedToADepartingMember(t *testing.T) {
	hygiene.Isolate(t)
	repo, _, _ := extractFixture(t, extractDecls, nil)
	target := filepath.Join(t.TempDir(), "widget")
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(e *strictcli.Effects) error {
		reg, err := options.Shipped()
		if err != nil {
			return err
		}
		_, err = options.Set(e, reg, repo.Dir, load(t, repo).Declarations, options.SetRequest{
			ID: options.Prefix + options.DepFloors, Current: "error", Ideal: "error", Reason: "widget polices its floors", Scope: "packages/widget",
		})
		return err
	})
	repo.Commit("widget's dep-floors", options.Dir)
	refusedThenCleared(t, repo, target, "scoped to a departing member", func() {
		repo.Git("rm", "-r", "-q", options.Dir)
		repo.Git("commit", "-q", "-m", "drop the entry")
	})
}

func TestExtractRefusesARenamedTagCollidingWithAnother(t *testing.T) {
	hygiene.Isolate(t)
	repo, _, _ := extractFixture(t, extractDecls, nil)
	target := filepath.Join(t.TempDir(), "widget")
	repo.Git("tag", "v0.1.0", repo.Head())
	refusedThenCleared(t, repo, target, "exists already at another commit", func() {
		repo.Git("tag", "-d", "v0.1.0")
	})
}

func TestExtractRefusesAPendingIdentityOfADepartingSubject(t *testing.T) {
	hygiene.Isolate(t)
	pending := widgetRecord + "\n[[identities]]\nsubject = \"widget\"\nfacet = \"package-name\"\nvalue = \"widget-client\"\nregistry = \"npm\"\ntag_patterns = [\"widget@v*\"]\neffective_version = \"0.2.0\"\nreason = \"renamed on npm\"\n"
	repo, _, _ := extractFixture(t, extractDecls, map[string]string{lifecycle.RecordFile: pending})
	target := filepath.Join(t.TempDir(), "widget")
	refusedThenCleared(t, repo, target, "pending identity", func() {
		repo.CommitFile(lifecycle.RecordFile, widgetRecord, "the rename is dropped")
	})
}

func TestExtractRefusesAMemberWithNothingTracked(t *testing.T) {
	hygiene.Isolate(t)
	ghost := strings.Replace(extractDecls, "path = \"packages/widget\"\nname = \"widget\"\nreleasable = \"widget\"\n", "path = \"packages/widget\"\nname = \"widget\"\nreleasable = \"widget\"\n\n[[members]]\npath = \"packages/ghost\"\nname = \"ghost\"\nreleasable = \"widget\"\n", 1)
	repo, _, _ := extractFixture(t, ghost, nil)
	target := filepath.Join(t.TempDir(), "widget")
	refusedThenCleared(t, repo, target, "has nothing tracked", func() {
		repo.CommitFile("packages/ghost/package.json", "{\n  \"name\": \"ghost\",\n  \"version\": \"0.1.0\"\n}\n", "ghost's package")
	})
}

func TestExtractRefusesAMemberHoldingASubmodule(t *testing.T) {
	hygiene.Isolate(t)
	repo, w1, _ := extractFixture(t, extractDecls, nil)
	target := filepath.Join(t.TempDir(), "widget")
	repo.Git("update-index", "--add", "--cacheinfo", "160000,"+w1+",packages/widget/vendored")
	repo.Git("commit", "-q", "-m", "a submodule")
	refusedThenCleared(t, repo, target, "holds a submodule", func() {
		repo.Git("rm", "--cached", "-q", "packages/widget/vendored")
		repo.Git("commit", "-q", "-m", "no submodule")
	})
}

func TestExtractRefusesAnUndeclaredReleasableNamingTheDeclaredOnes(t *testing.T) {
	hygiene.Isolate(t)
	repo, _, _ := extractFixture(t, extractDecls, nil)
	target := filepath.Join(t.TempDir(), "widget")
	mustRefuse(t, true, extracting(repo, "widgets", target, nil, false), "the declared releasables are widget, gadget")
}
