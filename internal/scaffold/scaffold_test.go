package scaffold

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A Go project is scaffolded into the new layout: its VERSION, its CI
// workflow, the scratch directories with their go.mod files, the private
// go.mod stub under .strictmetadata/ (never under .rlsbl/), the hooks, the
// scaffold state, and the merge bases. A second run changes nothing.
func TestAGoProjectIsScaffoldedIntoTheNewLayout(t *testing.T) {
	hygiene.Isolate(t)
	repo := newProject(t, standalone("none", ""), goModule)
	s := mustScaffold(t, repo.Dir, nil)

	if got := readFile(t, repo, "VERSION"); got != "0.0.0\n" {
		t.Errorf("VERSION = %q", got)
	}
	if !strings.Contains(readFile(t, repo, ".github/workflows/ci.yml"), "go test") {
		t.Errorf("the CI workflow does not test the module:\n%s", readFile(t, repo, ".github/workflows/ci.yml"))
	}
	if !strings.Contains(readFile(t, repo, ".strictmetadata/go.mod"), "module private.invalid/rlsbl-private") {
		t.Error("the private stub go.mod is not under .strictmetadata/")
	}
	if exists(t, repo, ".rlsbl") {
		t.Error("scaffold wrote the old layout's .rlsbl/")
	}
	for _, dir := range ScratchDirs {
		if ignore := readFile(t, repo, dir+"/.gitignore"); !strings.HasPrefix(ignore, "*\n!.gitignore") || !strings.Contains(ignore, "!go.mod") {
			t.Errorf("%s/.gitignore = %q", dir, ignore)
		}
		if !strings.Contains(readFile(t, repo, dir+"/go.mod"), "module scratch.invalid/rlsbl-scratch") {
			t.Errorf("%s has no scratch go.mod", dir)
		}
	}
	if readFile(t, repo, ".git/hooks/pre-push") != PrePushHook || readFile(t, repo, ".git/hooks/post-rewrite") != PostRewriteHook {
		t.Error("the git hooks were not installed")
	}
	state, found, err := ReadState(repo.Dir)
	if err != nil || !found || state.RlsblVersion != "0.132.0" {
		t.Fatalf("the scaffold state: %+v, %v, %v", state, found, err)
	}
	for _, p := range []string{"VERSION", ".github/workflows/ci.yml", ".strictmetadata/go.mod", "experiments/go.mod"} {
		if state.Files[p] != FileHash([]byte(readFile(t, repo, p))) {
			t.Errorf("the state records %s as %q", p, state.Files[p])
		}
		if readFile(t, repo, BasePath(p)) != readFile(t, repo, p) {
			t.Errorf("the merge base of %s is not what was written", p)
		}
	}
	if _, recorded := state.Files[".gitignore"]; recorded {
		t.Error("the root .gitignore, the project's own, is recorded as managed")
	}
	if !strings.Contains(s.text(), `No publish workflow: publish_mode "none"`) {
		t.Errorf("the report does not say why there is no publish workflow:\n%s", s.text())
	}
	if s.rowStatus(".github/workflows/ci.yml") != statusCreated {
		t.Errorf("ci.yml: %q\n%s", s.rowStatus(".github/workflows/ci.yml"), s.text())
	}

	again := mustScaffold(t, repo.Dir, nil)
	for _, p := range []string{"VERSION", ".github/workflows/ci.yml", ".gitignore", ".strictmetadata/go.mod", "experiments/.gitignore"} {
		if got := again.rowStatus(p); got != statusUnchanged {
			t.Errorf("a second run reports %s as %q", p, got)
		}
	}
}

// Under --dry-run scaffold writes nothing at all.
func TestADryRunWritesNothing(t *testing.T) {
	hygiene.Isolate(t)
	repo := newProject(t, standalone("none", ""), goModule)
	before := snapshot(t, repo.Dir)
	mustScaffold(t, repo.Dir, func(in *Inputs) { in.DryRun = true })
	sameFiles(t, before, snapshot(t, repo.Dir))
	if exists(t, repo, ".git/hooks/pre-push") {
		t.Error("a dry run installed the pre-push hook")
	}
}

// A workspace's root member is not scaffolded: its workflows are the ones
// monorepo sync generates.
func TestAWorkspacesRootMemberIsNotScaffolded(t *testing.T) {
	hygiene.Isolate(t)
	repo := newProject(t, workspaceWithWidget, map[string]string{"widget/go.mod": "module github.com/acme/widget\n\ngo 1.26\n"})
	before := snapshot(t, repo.Dir)
	s := mustScaffold(t, repo.Dir, nil)
	if !strings.Contains(s.text(), WorkspaceRootSkip) {
		t.Fatalf("the root was not skipped:\n%s", s.text())
	}
	sameFiles(t, before, snapshot(t, repo.Dir))
}

// A directory inside a member that is not the member's own is refused,
// naming the member's directory; running there clears it.
func TestADirectoryInsideAMemberIsRefusedNamingTheMember(t *testing.T) {
	hygiene.Isolate(t)
	files := map[string]string{"go.mod": goModule["go.mod"], "main.go": goModule["main.go"], "cmd/tool/main.go": "package main\n\nfunc main() {}\n"}
	repo := newProject(t, standalone("none", ""), files)
	s := runScaffold(t, repo.Path("cmd/tool"), nil)
	if s.result.ExitCode == 0 || !strings.Contains(s.text(), "run it in "+repo.Dir) {
		t.Fatalf("exit %d:\n%s", s.result.ExitCode, s.text())
	}
	mustScaffold(t, repo.Dir, nil)
}

// Scaffolding a workspace member regenerates the workspace's routers, since
// the member's workflows are their inputs.
func TestScaffoldingAWorkspaceMemberSyncsTheRouters(t *testing.T) {
	hygiene.Isolate(t)
	repo := newProject(t, workspaceWithWidget, map[string]string{
		"widget/go.mod":  "module github.com/acme/widget\n\ngo 1.26\n",
		"widget/main.go": "package main\n\nfunc main() {}\n",
	})
	s := mustScaffold(t, repo.Path("widget"), nil)
	if !exists(t, repo, "widget/.github/workflows/ci.yml") {
		t.Fatalf("the member's CI workflow was not written:\n%s", s.text())
	}
	router := readFile(t, repo, workflows.RouterPath)
	if !strings.HasPrefix(router, workflows.Header) || !strings.Contains(router, "widget") {
		t.Fatalf("the CI router does not route the member:\n%s", router)
	}
	if !strings.Contains(s.text(), "Wrote "+workflows.RouterPath) {
		t.Errorf("the report does not name the router:\n%s", s.text())
	}
	if exists(t, repo, "widget/.strictmetadata/go.mod") || exists(t, repo, declarations.MetadataDir+"/go.mod") {
		t.Error("a module below the root got a stub for .strictmetadata/, which lies outside it")
	}
}

// LICENSE is written only from the lifecycle-and-license record: the
// releasable's license in effect, when rlsbl carries its text. A missing
// git user.name, which names the copyright holder, is refused until set.
func TestTheLicenseComesFromTheRecordOnly(t *testing.T) {
	hygiene.Isolate(t)

	t.Run("no record", func(t *testing.T) {
		repo := newProject(t, standalone("none", ""), goModule)
		mustScaffold(t, repo.Dir, nil)
		if exists(t, repo, "LICENSE") {
			t.Error("a LICENSE was written without a record")
		}
	})

	t.Run("proprietary", func(t *testing.T) {
		repo := newProject(t, standalone("none", ""), goModule)
		writeRecord(repo, proprietaryRecord)
		repo.Git("config", "user.name", "Ada Lovelace")
		mustScaffold(t, repo.Dir, nil)
		if exists(t, repo, "LICENSE") {
			t.Error("a proprietary releasable got a LICENSE")
		}
	})

	t.Run("MIT", func(t *testing.T) {
		repo := newProject(t, standalone("none", ""), goModule)
		writeRecord(repo, mitRecord)
		s := runScaffold(t, repo.Dir, nil)
		if s.result.ExitCode == 0 || !strings.Contains(s.text(), "git config user.name") {
			t.Fatalf("a missing user.name: exit %d\n%s", s.result.ExitCode, s.text())
		}
		// The fix the refusal names.
		repo.Git("config", "user.name", "Ada Lovelace")
		mustScaffold(t, repo.Dir, nil)
		license := readFile(t, repo, "LICENSE")
		if !strings.HasPrefix(license, "MIT License\n") || !strings.Contains(license, "Copyright (c) 2026 Ada Lovelace") {
			t.Fatalf("LICENSE:\n%s", license)
		}
		state, _, err := ReadState(repo.Dir)
		if err != nil {
			t.Fatal(err)
		}
		if _, recorded := state.Files["LICENSE"]; recorded {
			t.Error("LICENSE, the project's once written, is recorded as managed")
		}
	})

	t.Run("an existing LICENSE is the project's", func(t *testing.T) {
		repo := newProject(t, standalone("none", ""), goModule)
		writeRecord(repo, mitRecord)
		repo.Git("config", "user.name", "Ada Lovelace")
		repo.Write("LICENSE", "my own words\n")
		mustScaffold(t, repo.Dir, nil)
		if readFile(t, repo, "LICENSE") != "my own words\n" {
			t.Error("an existing LICENSE was rewritten")
		}
	})
}

// A confidential repository's publish workflow carries no build
// attestation; a public one's does.
func TestAConfidentialRepositorysPublishWorkflowNamesNothingOfIt(t *testing.T) {
	hygiene.Isolate(t)

	confidential := newProject(t, standalone("ci", npmPipeline), npmPackage)
	writeRecord(confidential, proprietaryRecord)
	mustScaffold(t, confidential.Dir, nil)
	publish := readFile(t, confidential, workflows.PublishPath)
	if !strings.Contains(publish, "npm publish") || strings.Contains(publish, "--provenance") || strings.Contains(publish, "github.com/acme/portal") {
		t.Fatalf("the confidential repository's publish workflow:\n%s", publish)
	}

	public := newProject(t, standalone("ci", npmPipeline), npmPackage, publicAnswer)
	mustScaffold(t, public.Dir, nil)
	if publish := readFile(t, public, workflows.PublishPath); !strings.Contains(publish, "--provenance") || !strings.Contains(publish, workflows.WaitForCIJobKey+":") {
		t.Fatalf("the public repository's publish workflow:\n%s", publish)
	}
}

// --publish-mode none declares the mode, and the publish workflow scaffold
// managed is removed through saferm while it is unmodified. One edited
// since is refused, the declarations left as they were, until it is
// restored.
func TestAPublishWorkflowNoLongerRenderedIsRemovedWhileUnmodified(t *testing.T) {
	hygiene.Isolate(t)
	repo := newProject(t, standalone("ci", npmPipeline), npmPackage, publicAnswer)
	mustScaffold(t, repo.Dir, nil)
	written := readFile(t, repo, workflows.PublishPath)
	rm := testsupport.FakeSaferm(t)
	none := func(in *Inputs) { in.PublishMode = "none" }

	repo.Write(workflows.PublishPath, written+"# mine\n")
	s := runScaffold(t, repo.Dir, none)
	if s.result.ExitCode == 0 || !strings.Contains(s.text(), workflows.PublishPath) || !strings.Contains(s.text(), "saferm delete") {
		t.Fatalf("an edited orphan: exit %d\n%s", s.result.ExitCode, s.text())
	}
	if !strings.Contains(readFile(t, repo, declarations.ReleasablesFile), `publish_mode = "ci"`) {
		t.Fatal("a refused run wrote the declarations")
	}
	if len(rm.Calls()) != 0 {
		t.Fatalf("a refused run deleted: %q", rm.Calls())
	}

	// The fix the refusal names: restore what scaffold last wrote.
	repo.Write(workflows.PublishPath, written)
	s = mustScaffold(t, repo.Dir, none)
	if !strings.Contains(readFile(t, repo, declarations.ReleasablesFile), `publish_mode = "none"`) {
		t.Error("--publish-mode none was not declared")
	}
	calls := strings.Join(rm.Calls(), "\n")
	if !strings.Contains(calls, "-- "+workflows.PublishPath) || !strings.Contains(calls, "-- "+BasePath(workflows.PublishPath)) {
		t.Fatalf("saferm calls:\n%s", calls)
	}
	if !strings.HasPrefix(s.rowStatus(workflows.PublishPath), "removed (publish_mode") {
		t.Errorf("the report: %q\n%s", s.rowStatus(workflows.PublishPath), s.text())
	}
	state, _, err := ReadState(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, recorded := state.Files[workflows.PublishPath]; recorded {
		t.Error("the removed publish workflow is still recorded")
	}
}

// --target adds a target the member neither declares nor has detected to
// its declared targets, beside the ones it has.
func TestTheTargetFlagDeclaresATarget(t *testing.T) {
	hygiene.Isolate(t)
	files := map[string]string{"package.json": "{\n  \"name\": \"portal\",\n  \"version\": \"0.0.0\",\n  \"engines\": {\"node\": \">=22\"}\n}\n"}
	repo := newProject(t, standalone("none", ""), files)
	s := mustScaffold(t, repo.Dir, func(in *Inputs) { in.Target = "go" })
	d, err := declarations.Parse([]byte(readFile(t, repo, declarations.ReleasablesFile)))
	if err != nil {
		t.Fatal(err)
	}
	root, _ := d.MemberAt(declarations.RootPath)
	if len(root.Targets) != 2 {
		t.Fatalf("declared targets: %+v\n%s", root.Targets, s.text())
	}
	if !exists(t, repo, ".github/workflows/ci-go.yml") || !exists(t, repo, ".github/workflows/ci-npm.yml") {
		t.Errorf("each target of two gets its own CI workflow:\n%s", s.text())
	}
	if s.rowStatus(declarations.ReleasablesFile) != statusUpdated {
		t.Errorf("the declarations row: %q", s.rowStatus(declarations.ReleasablesFile))
	}
}

// A repository without release declarations is refused, showing the
// declarations a standalone project writes; writing them clears it.
func TestARepositoryWithoutDeclarationsIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	for rel, content := range goModule {
		repo.Write(rel, content)
	}
	testsupport.FakeGH(t, topicsAnswer)
	s := runScaffold(t, repo.Dir, nil)
	if s.result.ExitCode == 0 || !strings.Contains(s.text(), `repository_layout = "standalone"`) {
		t.Fatalf("exit %d:\n%s", s.result.ExitCode, s.text())
	}
	repo.Write(declarations.ReleasablesFile, standalone("none", ""))
	mustScaffold(t, repo.Dir, nil)
}

// A merge base directory missing while the state records managed files is
// refused before anything is written; creating it, as the refusal says,
// clears it, and each base is then seeded or rebuilt.
func TestAMissingMergeBaseDirectoryIsRefusedUntilCreated(t *testing.T) {
	hygiene.Isolate(t)
	repo := newProject(t, standalone("none", ""), goModule)
	mustScaffold(t, repo.Dir, nil)
	if err := os.RemoveAll(repo.Path(declarations.ScaffoldBasesDir)); err != nil {
		t.Fatal(err)
	}
	before := snapshot(t, repo.Dir)
	s := runScaffold(t, repo.Dir, nil)
	if s.result.ExitCode == 0 || !strings.Contains(s.text(), "mkdir -p") {
		t.Fatalf("exit %d:\n%s", s.result.ExitCode, s.text())
	}
	sameFiles(t, before, snapshot(t, repo.Dir))
	if err := os.MkdirAll(repo.Path(declarations.ScaffoldBasesDir), 0o755); err != nil {
		t.Fatal(err)
	}
	s = mustScaffold(t, repo.Dir, nil)
	if got := s.rowStatus(".github/workflows/ci.yml"); got != statusSeeded {
		t.Errorf("ci.yml: %q", got)
	}
	if !exists(t, repo, BasePath(".github/workflows/ci.yml")) {
		t.Error("the base was not seeded")
	}
}

// A scaffold commits what it wrote, with the message a missing merge base
// is rebuilt from and the Autogenerated trailer.
func TestAScaffoldCommitsWhatItWrote(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := newProject(t, standalone("none", ""), goModule)
	repo.Commit("the module", "go.mod", "main.go", declarations.ReleasablesFile)
	s := mustScaffold(t, repo.Dir, func(in *Inputs) { in.AutoCommit = true })
	if repo.Git("log", "-1", "--format=%s") != commitMessage || repo.Git("log", "-1", "--format=%(trailers:key=Autogenerated,valueonly)") != "true" {
		t.Fatalf("the last commit:\n%s\n%s", repo.Git("log", "-1"), s.text())
	}
	tracked := repo.Git("ls-files")
	for _, p := range []string{"VERSION", ".github/workflows/ci.yml", ".strictmetadata/go.mod", declarations.ScaffoldStateFile, BasePath("VERSION")} {
		if !strings.Contains("\n"+tracked+"\n", "\n"+p+"\n") {
			t.Errorf("%s was not committed", p)
		}
	}
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("the scaffold left uncommitted changes:\n%s", status)
	}
}

// A missing merge base is rebuilt from the file's last scaffold commit, so
// a local edit made since is merged with the template rather than refused
// or overwritten.
func TestAMissingBaseIsRebuiltFromTheLastScaffoldCommit(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := newProject(t, standalone("none", ""), goModule)
	repo.Commit("the module", "go.mod", "main.go", declarations.ReleasablesFile)
	mustScaffold(t, repo.Dir, func(in *Inputs) { in.AutoCommit = true })
	ci := ".github/workflows/ci.yml"
	if err := os.Remove(repo.Path(BasePath(ci))); err != nil {
		t.Fatal(err)
	}
	repo.Write(ci, readFile(t, repo, ci)+"# kept\n")
	s := mustScaffold(t, repo.Dir, nil)
	if got := s.rowStatus(ci); got != statusHealed {
		t.Fatalf("ci.yml: %q\n%s", got, s.text())
	}
	if !strings.Contains(s.text(), "merge base rebuilt from the last scaffold commit") {
		t.Errorf("the healing was not reported:\n%s", s.text())
	}
	if !strings.HasSuffix(readFile(t, repo, ci), "# kept\n") {
		t.Error("the local edit was lost")
	}
}

// A workspace's publish router renders the root member's publish jobs from
// scaffold's templates; a member publishing nothing has no publish
// workflow to render.
func TestTheRootMembersPublishWorkflowIsRenderedForTheRouter(t *testing.T) {
	hygiene.Isolate(t)
	text := `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "ci"
publish_ci_check_pattern = '^(test)( \(.*\))?$'

[[members]]
path = "."
name = "root"
releasable = "portal"
` + npmPipeline + `
[[releasables]]
name = "widget"
tag_format = "widget/v{version}"
publish_mode = "none"

[[members]]
path = "widget"
name = "widget"
releasable = "widget"
`
	repo := newProject(t, text, map[string]string{
		"package.json":        npmPackage["package.json"],
		"package-lock.json":   npmPackage["package-lock.json"],
		"widget/package.json": "{\n  \"name\": \"widget\",\n  \"version\": \"0.1.0\",\n  \"engines\": {\"node\": \">=22\"}\n}\n",
	}, publicAnswer)
	ws, err := workspace.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	root, _ := ws.Declarations.MemberAt(declarations.RootPath)
	widget, _ := ws.Declarations.MemberAt("widget")
	var rendered string
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		gh, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		if rendered, err = MemberPublishWorkflow(ctx.Effects(), ws, root, gh, today); err != nil {
			return err
		}
		_, err = MemberPublishWorkflow(ctx.Effects(), ws, widget, gh, today)
		return err
	})
	if !strings.Contains(rendered, "npm publish") || !strings.Contains(rendered, workflows.WaitForCIJobKey+":") {
		t.Fatalf("the root member's publish workflow:\n%s", rendered)
	}
	if r.ExitCode == 0 || !strings.Contains(r.Stderr, `the member "widget" has no publish workflow`) {
		t.Fatalf("a member publishing nothing: exit %d\n%s", r.ExitCode, r.Stderr)
	}
}

// Tracked files in a scratch directory are refused, since the ignore-all
// .gitignore would hide every file added beside them; moving them out, as
// the refusal shows, clears it.
func TestTrackedFilesInAScratchDirectoryAreRefusedUntilMoved(t *testing.T) {
	hygiene.Isolate(t)
	repo := newProject(t, standalone("none", ""), goModule)
	repo.CommitFile("experiments/plot.png", "png\n", "a plot")
	s := runScaffold(t, repo.Dir, nil)
	if s.result.ExitCode == 0 || !strings.Contains(s.text(), "git mv experiments/plot.png assets/plot.png") {
		t.Fatalf("exit %d:\n%s", s.result.ExitCode, s.text())
	}
	if err := os.MkdirAll(repo.Path("assets"), 0o755); err != nil {
		t.Fatal(err)
	}
	repo.Git("mv", "experiments/plot.png", "assets/plot.png")
	repo.Git("commit", "-q", "-m", "move the plot")
	mustScaffold(t, repo.Dir, nil)
}

// The scaffold files rlsbl commits into a scratch directory are not
// refused as tracked files.
func TestScaffoldsOwnScratchFilesAreNotRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo := newProject(t, standalone("none", ""), goModule)
	repo.Write("experiments/.gitignore", "*\n!.gitignore\n!go.mod\n")
	repo.Write("experiments/go.mod", "module scratch.invalid/rlsbl-scratch\n")
	repo.Commit("scratch", "experiments/.gitignore", "experiments/go.mod")
	r := testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(e *strictcli.Effects) error {
		g, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		return refuseTrackedScratchFiles(g, declarations.RootPath)
	})
	if r.ExitCode != 0 {
		t.Fatalf("stderr:\n%s", r.Stderr)
	}
}
