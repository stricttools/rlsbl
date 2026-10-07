package migration

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// configCase is one row of the config-key audit, converted from a
// standalone project's .rlsbl/config.json.
type configCase struct {
	name string
	// config replaces .rlsbl/config.json.
	config string
	// setup, when set, adds files beside it.
	setup func(f *fixture)
	// declarations are substrings the converted releasables.toml holds.
	declarations []string
	// notes are substrings of the plan's notes.
	notes []string
	// files map a planned path to a substring of its content.
	files map[string]string
}

const npmPipeline = `"pipelines": {"npm": {"type": "npm", "target": "npm", "local": false}}`

func TestEveryConfigKeyConverts(t *testing.T) {
	hygiene.Isolate(t)
	goModule := func(f *fixture) {
		f.licenses = map[string]string{"portal": "MIT"}
		f.write("go.mod", "module github.com/owner/portal\n\ngo 1.26.3\n")
		f.write("cmd/portal/main.go", "package main\n\nfunc main() {}\n")
	}
	cases := []configCase{
		{name: "publish_mode", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}}`, declarations: []string{`publish_mode = "none"`}},
		{name: "targets with a path", config: `{"publish_mode": "none", "targets": [{"name": "npm", "path": "web/"}], "pipelines": {}}`,
			setup:        func(f *fixture) { f.write("web/package.json", `{"name": "portal", "license": "MIT"}`) },
			declarations: []string{`targets = [{ name = "npm", path = "web" }]`}},
		{name: "a pipeline's name, type, target, and local", config: `{"publish_mode": "ci", "targets": ["npm"], "pipelines": {"registry": {"type": "npm", "target": "npm", "local": true}}}`,
			declarations: []string{"[[members.pipelines]]", `name = "registry"`, `type = "npm"`, `target = "npm"`, "local = true", `artifact = "package"`}},
		{name: "provenance, the launcher keys, the asset keys, and token_var", config: `{"publish_mode": "ci", "targets": ["npm"], "pipelines": {"npm": {"type": "npm", "target": "npm", "local": false, "provenance": true, "wraps": "x", "binary_source": "x", "download": "x", "assets": [], "custom_assets": [], "max_asset_size_mb": 5, "token_var": "NPM_TOKEN"}}}`,
			notes: []string{"pipelines.npm.provenance is dropped", "pipelines.npm.wraps is dropped", "pipelines.npm.binary_source is dropped", "pipelines.npm.download is dropped", "pipelines.npm.assets is dropped", "pipelines.npm.custom_assets is dropped", "pipelines.npm.max_asset_size_mb is dropped", "pipelines.npm.token_var is dropped"}},
		{name: "a go pipeline's artifact and install paths, with homebrew", config: `{"publish_mode": "ci", "targets": ["go"], "homebrew": {"tap": "owner/homebrew-tap", "description": "x", "license": "MIT"}, "pipelines": {"go": {"type": "go", "target": "go", "local": false, "artifact": "binary", "install_paths": ["./cmd/portal"]}}}`,
			setup:        goModule,
			declarations: []string{`artifact = "binary"`, `install_paths = ["./cmd/portal"]`, `homebrew_tap = "owner/homebrew-tap"`},
			notes:        []string{"homebrew.description is dropped", "homebrew.license is dropped"}},
		{name: "npm_wrapper.enabled", config: `{"publish_mode": "ci", "targets": ["go", "npm"], "npm_wrapper": {"enabled": true}, "pipelines": {"go": {"type": "go", "target": "go", "local": false, "artifact": "binary"}, "npm": {"type": "npm", "target": "npm", "local": false}}}`,
			setup:        goModule,
			declarations: []string{`artifact = "go-binary"`, `binary_pipeline = "go"`}},
		{name: "release_branches", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "release_branches": ["main"]}`, declarations: []string{`release_branches = ["main"]`}},
		{name: "the deleted keys", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "changelog_format": "jsonl", "changelog_format_version_enforced": true, "private": false, "deploy": [], "services": [], "test_env": {}, "uv_sync_verbose": true}`,
			notes: []string{"changelog_format is dropped", "changelog_format_version_enforced is dropped", "private is dropped", "deploy is dropped", "services is dropped", "test_env is dropped", "uv_sync_verbose is dropped"}},
		{name: "the timeouts", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "push_timeout": 30, "ci_timeout": 600, "check_timeout": 900, "hook_timeout": 60}`,
			declarations: []string{"[timeouts]", "push_seconds = 30", "ci_seconds = 600", "check_seconds = 900", "hook_seconds = 60"}},
		{name: "the test settings", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "test": {"pypi": {"markers": "not slow"}, "go": {"command": "go test ./..."}}}`,
			declarations: []string{`test = { pypi_markers = "not slow", go_command = "go test ./..." }`}},
		{name: "external checks, their form key dropped", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "external_checks": [{"name": "spell", "` + externalCheckFormKey + `": "freeform", "command": "make spell", "tag": "preflight", "cwd": "docs"}]}`,
			setup:        func(f *fixture) { f.write("docs/README.md", "docs\n") },
			declarations: []string{`external_checks = [{ name = "spell", tag = "preflight", command = "make spell", cwd = "docs" }]`}},
		{name: "the tool checks", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "checks": {"format": {"paths": ["src"]}, "type-check": {"paths": ["src"], "cwd": "py"}}}`,
			files: map[string]string{StrictcodeFile: "[python_tools.type-check]"}},
		{name: "strictspec_gate", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "strictspec_gate": {"certificate": "cert.json", "adjudication": "adj.toml"}}`,
			files: map[string]string{StrictcodeFile: `certificate = "cert.json"`}},
		{name: "internal_dep_floors", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "internal_dep_floors": ["strictcli"]}`, declarations: []string{`internal_dep_floors = ["strictcli"]`}},
		{name: "env_file and github_repo", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "env_file": "~/Projects/.env", "github_repo": "owner/portal"}`,
			declarations: []string{`environment_file = "~/Projects/.env"`, `github_repository = "owner/portal"`}},
		{name: "hooks", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "hooks": {"pre_checks": ["make setup"], "pre_release": [{"cmd": "make test", "env": {"CI": "1"}}], "post_release": ["make announce"]}}`,
			declarations: []string{`hooks = { pre_checks = ["make setup"], pre_release = [{ cmd = "make test", env = { CI = "1" } }], post_release = ["make announce"] }`}},
		{name: "a standalone project's publish_gate_check_regex", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "publish_gate_check_regex": "^ci$"}`,
			notes: []string{"publish_gate_check_regex is dropped"}},
		{name: "test_sandbox", config: `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "test_sandbox": {"runner_path": "scripts/test.sh", "command": "npm test", "prewarm": ["scripts/warm.sh"], "extra_env": {"NODE_ENV": "test"}, "ci_workflows": [".github/workflows/ci.yml"], "carry_ignored": [".cache"], "default_args": "-q"}}`,
			files: map[string]string{declarations.TestRunnerFile: `extra_env = { NODE_ENV = "test" }`}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hygiene.Isolate(t)
			f := standalone(t)
			f.write(".rlsbl/config.json", c.config+"\n")
			if c.setup != nil {
				c.setup(f)
			}
			f.commit("the config under test")
			plan := f.mustPlan()
			text := planned(t, plan, declarations.ReleasablesFile)
			for _, want := range c.declarations {
				contains(t, text, want)
			}
			for _, want := range c.notes {
				if !hasNote(plan, want) {
					t.Fatalf("no note holds %q: %v", want, plan.notes)
				}
			}
			for rel, want := range c.files {
				contains(t, planned(t, plan, rel), want)
			}
		})
	}
}

func TestEveryWorkspaceKeyConverts(t *testing.T) {
	hygiene.Isolate(t)
	f := workspaceFixture(t)
	f.edit(".rlsbl-monorepo/workspace.toml", "path = \"widget\"\nname = \"widget\"\n", "path = \"widget\"\nname = \"widget\"\nlibrary = true\ntest_only = false\nimport_name = \"widget_lib\"\nregistry_name = \"widget-js\"\ndescription = \"Widgets\"\nlint_allow = [\"net/http\"]\n")
	f.edit(".rlsbl-monorepo/workspace.toml", "[[releasables]]\nname = \"gadget\"\n", "[[releasables]]\nname = \"gadget\"\ntag_format = \"gadget-v{version}\"\nsubtree_remote = \"\"\n")
	f.write("tools/.keep", "")
	f.edit(".rlsbl-monorepo/workspace.toml", "[layers]", "[[projects]]\npath = \"tools\"\nname = \"tools\"\ndev_only = true\nreleasable = false\n\n[layers]")
	f.commit("every member key")
	text := planned(t, f.mustPlan(), declarations.ReleasablesFile)
	for _, want := range []string{"library = true", `import_name = "widget_lib"`, `registry_name = "widget-js"`, `description = "Widgets"`, `lint_allow = ["net/http"]`, `tag_format = "gadget-v{version}"`, "dev_only = true", `path = "tools"`} {
		contains(t, text, want)
	}
	lacks(t, text, "subtree_remote")
	lacks(t, text, "layers")
}

func TestARootReleasablePublishingFromCINamesItsCheckPattern(t *testing.T) {
	hygiene.Isolate(t)
	f := rootOnlyWorkspace(t)
	f.write(".rlsbl-monorepo/releasables/portal/config.json", `{"publish_mode": "ci", "targets": ["npm"], "pipelines": {"npm": {"type": "npm", "target": "npm", "local": false}}, "publish_gate_check_regex": "^ci / "}`+"\n")
	f.commit("a root releasable publishing from CI")
	contains(t, planned(t, f.mustPlan(), declarations.ReleasablesFile), `publish_ci_check_pattern = "^ci / "`)
}

func TestTheUserLevelConfigIsNotRead(t *testing.T) {
	hygiene.Isolate(t)
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(home, ".rlsbl"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".rlsbl", "config.json"), []byte(`{"tag": false}`), 0o644); err != nil {
		t.Fatal(err)
	}
	f := standalone(t)
	if writes(f.mustPlan(), ".strictmetadata/options/project.toml") {
		t.Fatal("the user-level config switched ecosystem tagging off")
	}
}

func TestArchiveFatesAndReleaseFilesConvert(t *testing.T) {
	hygiene.Isolate(t)
	f := standalone(t)
	f.write(".rlsbl/releases/v0.0.9.toml", "format_version = 1\nbump = \"patch\"\ndescription = \"Abandoned\"\ninclude = [\"npm\"]\nexclude = []\nnever_released = true\n")
	f.write(".rlsbl/releases/v0.0.8.toml", "bump = \"patch\"\ndescription = \"Lost\"\ninclude = [\"npm\"]\nexclude = []\nunrecoverable = true\nshipped_as = \"portal-0.0.8\"\nrelease_notices = [\"Deprecated: use 0.1.0.\"]\n")
	f.write(".rlsbl/releases/unreleased.toml", "# Version bump type\nbump = \"minor\"\ndescription = \"The next release\"\ncontext = \"Why\"\n# preid = \"\"\ninclude = [\"npm\"]\nexclude = []\n")
	f.write(".rlsbl/releases/retry.toml", "ref = \"v0.1.0\"\ndispatch = [\"publish.yml\"]\n")
	f.commit("fates")
	plan := f.mustPlan()
	contains(t, planned(t, plan, ".strictmetadata/releases/portal/v0.0.9.toml"), "never_released = true")
	lost := planned(t, plan, ".strictmetadata/releases/portal/v0.0.8.toml")
	contains(t, lost, "format_version = 2")
	contains(t, lost, `shipped_as = "portal-0.0.8"`)
	contains(t, lost, "release_notices")
	file := planned(t, plan, ".strictmetadata/releases/portal/unreleased.toml")
	contains(t, file, "format_version = 2")
	contains(t, file, `context = "Why"`)
	retry := planned(t, plan, ".strictmetadata/.release-state/portal/retry.toml")
	contains(t, retry, `workflows = ["publish.yml"]`)
}

func TestAnUnfilledReleaseFileIsDropped(t *testing.T) {
	hygiene.Isolate(t)
	f := standalone(t)
	f.write(".rlsbl/releases/unreleased.toml", "format_version = 1\nbump = \"\"\ndescription = \"\"\ninclude = [\"npm\"]\nexclude = []\n")
	f.commit("an unfilled release file")
	plan := f.mustPlan()
	if writes(plan, ".strictmetadata/releases/portal/unreleased.toml") || !hasNote(plan, "unfilled release file") {
		t.Fatalf("the unfilled release file was not dropped: %v", plan.notes)
	}
}

func TestAReleasedIdentityTransitionClosesTheOldIdentity(t *testing.T) {
	hygiene.Isolate(t)
	f := standalone(t)
	f.write(".rlsbl/transitions.jsonl", `{"format_version":1,"`+oldKey+`":"identity-transition","id":"c1","recorded_at":"2026-09-01T10:00:00+02:00","facet":"package-name","old":"portal-old","new":"portal","effective_version":"0.1.0"}`+"\n"+
		`{"format_version":1,"`+oldKey+`":"release-commit-remap","id":"c2","recorded_at":"2026-09-02T10:00:00+02:00","rewrite":"a scrub","mappings":[{"old_sha":"`+someCommit+`","new_sha":"`+otherSHA+`"}]}`+"\n")
	f.commit("an identity transition")
	plan := f.mustPlan()
	contains(t, planned(t, plan, declarations.TransitionsFile), `"event":"release-commit-remap"`)
	rec, err := lifecycle.Parse([]byte(planned(t, plan, lifecycle.RecordFile)))
	if err != nil {
		t.Fatal(err)
	}
	var closed, open bool
	for _, id := range rec.Identities() {
		if id.Facet != lifecycle.FacetPackageName {
			continue
		}
		if id.Value == "portal-old" && !id.Open() && !id.Pending() {
			closed = true
		}
		if id.Value == "portal" && id.Open() && !id.Pending() {
			open = true
		}
	}
	if !closed || !open {
		t.Fatalf("the package-name identities: %+v", rec.Identities())
	}
}

func TestAScrubArchiveBecomesAHistoryRewriteArchive(t *testing.T) {
	hygiene.Isolate(t)
	f := standalone(t)
	head := f.repo.Head()
	f.write(".rlsbl/scrubs/scrub-"+head[:12]+".json", `{"schema_version": 1, "mode": "match", "reason": "a leaked token", "old_head": "`+someCommit+`", "new_head": "`+head+`", "rewrites": {"`+someCommit+`": "`+head+`"}, "tags": [{"refname": "refs/tags/v0.1.0", "old_sha": "`+someCommit+`", "new_sha": "`+head+`", "annotated": false}], "commits_rewritten": 1, "completed_steps": ["pushed"]}`+"\n")
	f.commit("a scrub archive")
	plan := f.mustPlan()
	found := false
	for _, w := range plan.writes {
		if strings.HasPrefix(w.path, declarations.HistoryRewritesDir+"/") && w.writer != nil {
			found = true
		}
	}
	if !found {
		t.Fatal("the scrub archive was not converted")
	}
	f.migrate()
	entries, err := os.ReadDir(f.repo.Path(declarations.HistoryRewritesDir))
	if err != nil {
		t.Fatal(err)
	}
	var archives []string
	for _, e := range entries {
		if e.Name() != "manifest.toml" {
			archives = append(archives, e.Name())
		}
	}
	if len(archives) != 1 {
		t.Fatalf("history rewrites: %v", archives)
	}
	text := f.read(declarations.HistoryRewritesDir + "/" + archives[0])
	contains(t, text, `mode = "pattern"`)
	contains(t, text, `reason = "a leaked token"`)
}

func TestTheLibraryLintListsMoveToStrictcode(t *testing.T) {
	hygiene.Isolate(t)
	f := standalone(t)
	f.write(".rlsbl/lint/python.toml", "[forbidden-imports]\nmodules = [\"argparse\", \"click\", \"typer\", \"flask\", \"fastapi\", \"django\", \"uvicorn\", \"granian\", \"starlette\", \"tornado\", \"bottle\"]\nallow = []\n\n[stdout]\nenabled = true\nignore = []\n\n[entry-point]\nenabled = true\nignore = []\n\n[files]\nexclude = []\n")
	f.write(".rlsbl/lint/npm.toml", "[forbidden-imports]\nmodules = [\"express\"]\nallow = [\"koa\"]\n\n[stdout]\nenabled = true\nignore = [\"console.log\"]\n")
	f.commit("lint configs")
	text := planned(t, f.mustPlan(), StrictcodeFile)
	contains(t, text, `ts = ["express"]`)
	contains(t, text, `ts = ["koa"]`)
	contains(t, text, `ts = ["console.log"]`)
	lacks(t, text, "argparse")
}
