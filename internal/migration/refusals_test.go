package migration

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// refusalCase is one refusal that names a hand edit: the old layout that
// earns it, the refusal, and the edit it names, after which the run is no
// longer refused.
type refusalCase struct {
	name    string
	fixture func(*testing.T) *fixture
	breakIt func(f *fixture)
	want    string
	fix     func(f *fixture)
}

// standaloneConfig replaces a standalone fixture's config.
func standaloneConfig(body string) func(f *fixture) {
	return func(f *fixture) { f.write(".rlsbl/config.json", body+"\n") }
}

const plainConfig = `{"publish_mode": "ci", "targets": ["npm"], ` + npmPipeline + `}`

// workspaceWithTools is the workspace fixture with a dev-only member,
// tools, the hand edits below change.
func workspaceWithTools(t *testing.T) *fixture {
	f := workspace(t)
	f.write("tools/.keep", "")
	f.edit(".rlsbl-monorepo/workspace.toml", "[layers]", "[[projects]]\npath = \"tools\"\nname = \"tools\"\ndev_only = true\nreleasable = false\n\n[layers]")
	f.commit("a tools member")
	return f
}

// rootReleasableWorkspace is a workspace whose root is versioned under
// portal, with a member widget versioned under it too.
func rootReleasableWorkspace(t *testing.T) *fixture {
	f := rootOnlyWorkspace(t)
	f.write(".rlsbl-monorepo/workspace.toml", "[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\n\n[[projects]]\npath = \".\"\nreleasable = \"portal\"\n\n[[projects]]\npath = \"widget\"\nname = \"widget\"\nreleasable = \"portal\"\n")
	f.write("widget/package.json", `{"name": "widget", "version": "0.2.0", "license": "MIT"}`+"\n")
	f.commit("a member below the root")
	return f
}

func eventLine(event, fields string) string {
	return `{"format_version":1,"` + oldKey + `":"` + event + `","id":"` + event + `-1","recorded_at":"2026-09-01T10:00:00+02:00",` + fields + "}\n"
}

func TestEveryRefusalNamingAHandEditClearsOnceTheEditIsMade(t *testing.T) {
	hygiene.Isolate(t)
	cases := []refusalCase{
		{
			name: "a cloudflare-pages pipeline", fixture: standalone,
			breakIt: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], "pipelines": {"npm": {"type": "npm", "target": "npm", "local": false}, "docs": {"type": "cloudflare-pages", "local": false}}}`),
			want:    "is a cloudflare-pages pipeline", fix: standaloneConfig(plainConfig),
		},
		{
			name: "a pipeline of a removed target", fixture: standalone,
			breakIt: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], "pipelines": {"npm": {"type": "npm", "target": "npm", "local": false}, "image": {"type": "docker", "target": "docker", "local": false}}}`),
			want:    `has the type "docker"`, fix: standaloneConfig(plainConfig),
		},
		{
			name: "a launcher pipeline", fixture: standalone,
			breakIt: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], "pipelines": {"npm": {"type": "npm", "target": "npm", "local": false}, "shim": {"type": "npm", "target": "npm", "local": false, "artifact": "launcher"}}}`),
			want:    "publishes the launcher", fix: standaloneConfig(plainConfig),
		},
		{
			name: "a pipeline without a target", fixture: standalone,
			breakIt: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], "pipelines": {"npm": {"type": "npm", "local": false}}}`),
			want:    "names no target", fix: standaloneConfig(plainConfig),
		},
		{
			name: "npm_wrapper.platforms", fixture: standalone,
			breakIt: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], ` + npmPipeline + `, "npm_wrapper": {"enabled": false, "platforms": ["linux-x64"]}}`),
			want:    "Delete npm_wrapper.platforms", fix: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], ` + npmPipeline + `, "npm_wrapper": {"enabled": false}}`),
		},
		{
			name: "npm_scope", fixture: standalone,
			breakIt: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], ` + npmPipeline + `, "npm_scope": "@owner"}`),
			want:    "delete npm_scope by hand", fix: standaloneConfig(plainConfig),
		},
		{
			name: "a derived name that cannot name a directory", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write("package.json", `{"name": "@owner/portal", "version": "0.1.0", "license": "MIT"}`+"\n")
			},
			want: "holding name = ",
			fix:  func(f *fixture) { f.write(".rlsbl/releasable.toml", "name = \"portal\"\n") },
		},
		{
			name: "an implicit-mode workspace", fixture: workspace,
			breakIt: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "[[releasables]]\nname = \"widget\"\n\n[[releasables]]\nname = \"gadget\"\n", "")
			},
			want: "implicit-mode workspace",
			fix: func(f *fixture) {
				f.write(".rlsbl-monorepo/workspace.toml", "[[releasables]]\nname = \"widget\"\n\n[[releasables]]\nname = \"gadget\"\n\n"+f.read(".rlsbl-monorepo/workspace.toml"))
			},
		},
		{
			name: "a releasable's subtree_remote", fixture: workspace,
			breakIt: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "name = \"gadget\"\n\n[[projects]]", "name = \"gadget\"\nsubtree_remote = \"git@github.com:owner/gadget.git\"\n\n[[projects]]")
			},
			want: "Hand edit: delete the subtree_remote line",
			fix: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "subtree_remote = \"git@github.com:owner/gadget.git\"\n", "")
			},
		},
		{
			name: "a member's watch", fixture: workspace,
			breakIt: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "releasable = \"widget\"\n", "releasable = \"widget\"\nwatch = [\"widget/**\"]\n")
			},
			want: "watch is a retired key",
			fix:  func(f *fixture) { f.edit(".rlsbl-monorepo/workspace.toml", "watch = [\"widget/**\"]\n", "") },
		},
		{
			name: "a member's subtree_remote", fixture: workspace,
			breakIt: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "releasable = \"widget\"\n", "releasable = \"widget\"\nsubtree_remote = \"git@github.com:owner/widget.git\"\n")
			},
			want: ".subtree_remote is a retired key",
			fix: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "subtree_remote = \"git@github.com:owner/widget.git\"\n", "")
			},
		},
		{
			name: "a member's dev_node", fixture: workspaceWithTools,
			breakIt: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "name = \"tools\"\ndev_only = true\nreleasable = false\n", "name = \"tools\"\ndev_node = true\n")
			},
			want: "Hand edit: replace it with dev_only = true and releasable = false",
			fix: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "dev_node = true\n", "dev_only = true\nreleasable = false\n")
			},
		},
		{
			name: "a misnamed root member", fixture: workspace,
			breakIt: func(f *fixture) { f.edit(".rlsbl-monorepo/workspace.toml", "name = \"root\"", "name = \"top\"") },
			want:    `Hand edit: set name = "root"`,
			fix:     func(f *fixture) { f.edit(".rlsbl-monorepo/workspace.toml", "name = \"top\"", "name = \"root\"") },
		},
		{
			name: "a workspace without a root member", fixture: workspace,
			breakIt: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "[[projects]]\npath = \".\"\nname = \"root\"\ndev_only = true\nreleasable = false\n\n", "")
			},
			want: "declares no root member",
			fix: func(f *fixture) {
				f.write(".rlsbl-monorepo/workspace.toml", f.read(".rlsbl-monorepo/workspace.toml")+"\n[[projects]]\npath = \".\"\nname = \"root\"\ndev_only = true\nreleasable = false\n")
			},
		},
		{
			name: "a root releasable without a tag format", fixture: rootOnlyWorkspace,
			breakIt: func(f *fixture) { f.edit(".rlsbl-monorepo/workspace.toml", "tag_format = \"v{version}\"\n", "") },
			want:    "Hand edit: add tag_format",
			fix: func(f *fixture) {
				f.edit(".rlsbl-monorepo/workspace.toml", "name = \"portal\"\n", "name = \"portal\"\ntag_format = \"v{version}\"\n")
			},
		},
		{
			name: "a root releasable publishing from CI without a check pattern", fixture: rootOnlyWorkspace,
			breakIt: func(f *fixture) {
				f.write(".rlsbl-monorepo/releasables/portal/config.json", `{"publish_mode": "ci", "targets": ["npm"], `+npmPipeline+`}`+"\n")
			},
			want: `Hand edit: add "publish_gate_check_regex"`,
			fix: func(f *fixture) {
				f.write(".rlsbl-monorepo/releasables/portal/config.json", `{"publish_mode": "ci", "targets": ["npm"], `+npmPipeline+`, "publish_gate_check_regex": "^ci"}`+"\n")
			},
		},
		{
			name: "a promotion-split-map event", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".rlsbl/transitions.jsonl", eventLine("promotion-split-map", `"subtree_path":"widget","mirror_remote":"x","mappings":[]`))
			},
			want: "Hand edit: delete the line",
			fix:  func(f *fixture) { f.write(".rlsbl/transitions.jsonl", "") },
		},
		{
			name: "an identity transition in the repository's own record", fixture: workspace,
			breakIt: func(f *fixture) {
				f.write(".rlsbl-monorepo/transitions.jsonl", eventLine("identity-transition", `"facet":"package-name","old":"widget","new":"widget-client","effective_version":"0.4.0"`))
			},
			want: "move the line into the transition record of the releasable it concerns",
			fix: func(f *fixture) {
				line := f.read(".rlsbl-monorepo/transitions.jsonl")
				f.write(".rlsbl-monorepo/transitions.jsonl", "")
				f.write(".rlsbl-monorepo/releasables/widget/transitions.jsonl", line)
			},
		},
		{
			name: "a releasable-name identity transition", fixture: workspace,
			breakIt: func(f *fixture) {
				f.write(".rlsbl-monorepo/releasables/gadget/transitions.jsonl", eventLine("identity-transition", `"facet":"releasable-name","old":"gizmo","new":"gadget","effective_version":"0.1.0"`))
			},
			want: "replace it with a releasable-rename line",
			fix: func(f *fixture) {
				f.write(".rlsbl-monorepo/releasables/gadget/transitions.jsonl", eventLine("releasable-rename", `"old_name":"gizmo","new_name":"gadget","reason":"renamed"`))
			},
		},
		{
			name: "an archive carrying preid", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".rlsbl/releases/v0.0.5.toml", "format_version = 1\nbump = \"patch\"\ndescription = \"Old\"\ninclude = [\"npm\"]\nexclude = []\npreid = \"stable\"\nnever_released = true\n")
			},
			want: "Hand edit: delete the preid line",
			fix:  func(f *fixture) { f.edit(".rlsbl/releases/v0.0.5.toml", "preid = \"stable\"\n", "") },
		},
		{
			name: "a release file carrying blog", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".rlsbl/releases/unreleased.toml", "bump = \"minor\"\ndescription = \"Next\"\ninclude = [\"npm\"]\nexclude = []\nblog = false\n")
			},
			want: "Hand edit: delete the blog line",
			fix:  func(f *fixture) { f.edit(".rlsbl/releases/unreleased.toml", "blog = false\n", "") },
		},
		{
			name: "a release file carrying preid", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".rlsbl/releases/unreleased.toml", "bump = \"minor\"\ndescription = \"Next\"\ninclude = [\"npm\"]\nexclude = []\npreid = \"\"\n")
			},
			want: "Hand edit: delete the preid line",
			fix:  func(f *fixture) { f.edit(".rlsbl/releases/unreleased.toml", "preid = \"\"\n", "") },
		},
		{
			name: "a prerelease bump", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".rlsbl/releases/unreleased.toml", "bump = \"prerelease\"\ndescription = \"Next\"\ninclude = [\"npm\"]\nexclude = []\n")
			},
			want: "Hand edit: declare patch, minor, major, or infra",
			fix:  func(f *fixture) { f.edit(".rlsbl/releases/unreleased.toml", "\"prerelease\"", "\"minor\"") },
		},
		{
			name: "a batch release file of packages", fixture: workspace,
			breakIt: func(f *fixture) {
				f.write(".rlsbl-monorepo/releases/unreleased.toml", "[packages.widget]\nbump = \"minor\"\ndescription = \"More\"\ninclude = [\"npm\"]\nexclude = []\n")
			},
			want: "Hand edit: rename each [packages.<name>]",
			fix: func(f *fixture) {
				f.edit(".rlsbl-monorepo/releases/unreleased.toml", "[packages.widget]", "[releasables.widget]")
			},
		},
		{
			name: "per-member release state", fixture: workspace,
			breakIt: func(f *fixture) {
				f.write("widget/.rlsbl/changes/unreleased.jsonl", `{"format_version":1,"commits":[],"user_facing":false}`+"\n")
			},
			want: "is per-member release state",
			fix:  func(f *fixture) { f.remove("widget/.rlsbl/changes") },
		},
		{
			name: "a test_sandbox below the root", fixture: rootReleasableWorkspace,
			breakIt: func(f *fixture) {
				f.write("widget/.rlsbl/config.json", `{"test_sandbox": {"runner_path": "scripts/test.sh", "command": "npm test"}}`+"\n")
			},
			want: "Hand edit: move the section into the config of the root's releasable",
			fix: func(f *fixture) {
				f.write("widget/.rlsbl/config.json", "{}\n")
				f.write(".rlsbl-monorepo/releasables/portal/config.json", `{"publish_mode": "none", "targets": ["npm"], "pipelines": {}, "test_sandbox": {"runner_path": "scripts/test.sh", "command": "npm test"}}`+"\n")
			},
		},
		{
			name: "an rlsbl:library-lint entry", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".strictmetadata/options/code.toml", "format_version = 1\n\n[[entry]]\nid = \"rlsbl:library-lint\"\ncurrent = \"warn\"\nideal = \"error\"\nreason = \"later\"\n")
			},
			want: "Hand edit: delete the entry",
			fix:  func(f *fixture) { f.write(".strictmetadata/options/code.toml", "format_version = 1\n") },
		},
		{
			name: "an entry of a removed option", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".strictmetadata/options/project.toml", "format_version = 1\n\n[[entry]]\nid = \"rlsbl:layers-violations\"\ncurrent = \"off\"\nideal = \"error\"\nreason = \"later\"\n")
			},
			want: "names an option rlsbl removed with its check",
			fix:  func(f *fixture) { f.write(".strictmetadata/options/project.toml", "format_version = 1\n") },
		},
		{
			name: "a scoped certificate entry", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".strictmetadata/options/release.toml", "format_version = 1\n\n[[entry]]\nid = \"rlsbl:strictspec-certificate-gate\"\ncurrent = \"error\"\nideal = \"error\"\nreason = \"adopted\"\nscope = \"lib\"\n")
			},
			want: "Hand edit: delete the scope line",
			fix:  func(f *fixture) { f.edit(".strictmetadata/options/release.toml", "scope = \"lib\"\n", "") },
		},
		{
			name: "stdout.enabled = false", fixture: standalone,
			breakIt: func(f *fixture) { f.write(".rlsbl/lint/go.toml", "[stdout]\nenabled = false\n") },
			want:    "stdout.enabled = false has no counterpart",
			fix:     func(f *fixture) { f.write(".rlsbl/lint/go.toml", "[stdout]\n") },
		},
		{
			name: "entry-point.enabled = false", fixture: standalone,
			breakIt: func(f *fixture) { f.write(".rlsbl/lint/go.toml", "[entry-point]\nenabled = false\n") },
			want:    "entry-point.enabled = false has no counterpart",
			fix:     func(f *fixture) { f.write(".rlsbl/lint/go.toml", "[entry-point]\n") },
		},
		{
			name: "entry-point.ignore", fixture: standalone,
			breakIt: func(f *fixture) { f.write(".rlsbl/lint/go.toml", "[entry-point]\nignore = [\"cmd\"]\n") },
			want:    "Hand edit: empty the list",
			fix:     func(f *fixture) { f.write(".rlsbl/lint/go.toml", "[entry-point]\nignore = []\n") },
		},
		{
			name: "files.exclude", fixture: standalone,
			breakIt: func(f *fixture) { f.write(".rlsbl/lint/go.toml", "[files]\nexclude = [\"gen\"]\n") },
			want:    "files.exclude has no counterpart",
			fix:     func(f *fixture) { f.write(".rlsbl/lint/go.toml", "[files]\nexclude = []\n") },
		},
		{
			name: "the regex lint parser", fixture: standalone,
			breakIt: func(f *fixture) { f.write(".rlsbl/lint.toml", "parser = \"regex\"\n") },
			want:    "Hand edit: delete the file",
			fix:     func(f *fixture) { f.remove(".rlsbl/lint.toml") },
		},
		{
			name: "an exclusion of commits", fixture: standalone,
			breakIt: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], ` + npmPipeline + `, "batch_limits": {"exclusions": [{"reason": "bulk", "commits": ["` + someCommit + `"]}]}}`),
			want:    "Hand edit: delete its commits list",
			fix:     standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], ` + npmPipeline + `, "batch_limits": {"exclusions": []}}`),
		},
		{
			name: "an exclusion without a reason", fixture: standalone,
			breakIt: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], ` + npmPipeline + `, "batch_limits": {"exclusions": [{"entries": [{"version": "unreleased", "line": 1}]}]}}`),
			want:    "write one by hand",
			fix:     standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], ` + npmPipeline + `, "batch_limits": {"exclusions": [{"reason": "bulk", "entries": [{"version": "unreleased", "line": 1}]}]}}`),
		},
		{
			name: "a customized script beside a declared hook point", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".rlsbl/config.json", `{"publish_mode": "ci", "targets": ["npm"], `+npmPipeline+`, "hooks": {"pre_release": ["make check"]}}`+"\n")
				f.write(".rlsbl/hooks/pre-release.sh", "#!/usr/bin/env bash\nmake old-check\n")
			},
			want: "the Python never ran the script",
			fix:  func(f *fixture) { f.remove(".rlsbl/hooks/pre-release.sh") },
		},
		{
			name: "a releasable without a publish mode", fixture: workspace,
			breakIt: func(f *fixture) {
				f.write(".rlsbl-monorepo/releasables/gadget/config.json", "{}\n")
				f.write("gadget/.rlsbl/config.json", `{"targets": ["pypi"], "pipelines": {}}`+"\n")
			},
			want: "declares no publish_mode",
			fix: func(f *fixture) {
				f.write(".rlsbl-monorepo/releasables/gadget/config.json", `{"publish_mode": "none"}`+"\n")
			},
		},
		{
			name: "a missing standalone config", fixture: standalone,
			breakIt: func(f *fixture) { f.remove(".rlsbl/config.json") },
			want:    ".rlsbl/config.json is missing",
			fix:     standaloneConfig(plainConfig),
		},
		{
			name: "an unknown config key", fixture: standalone,
			breakIt: standaloneConfig(`{"publish_mode": "ci", "targets": ["npm"], ` + npmPipeline + `, "colour": "blue"}`),
			want:    "the key colour is not a key rlsbl's config ever had",
			fix:     standaloneConfig(plainConfig),
		},
		{
			name: "a lifecycle-and-license record already there", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".strictmetadata/lifecycle-and-license/lifecycle-and-license.toml", "format_version = 1\n")
			},
			want: "already exists, and the migration writes the record whole",
			fix:  func(f *fixture) { f.remove(".strictmetadata/lifecycle-and-license") },
		},
		{
			name: "strictcode.toml holding a key the migration writes", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".rlsbl/dead-modules.toml", "[[known_non_entry]]\npath = \"gallery.js\"\nreason = \"a demo\"\n")
				f.write(".rlsbl/config.json", `{"publish_mode": "ci", "targets": ["npm"], `+npmPipeline+`, "checks": {"lint": {"paths": ["src"]}}}`+"\n")
				f.write("strictcode.toml", "[python_tools.lint]\npaths = [\"lib\"]\n")
			},
			want: "already declares python_tools.lint",
			fix:  func(f *fixture) { f.write("strictcode.toml", "") },
		},
		{
			name: "disagreeing publish modes", fixture: workspace,
			breakIt: func(f *fixture) {
				f.write(".rlsbl-monorepo/releasables/widget/config.json", `{"publish_mode": "none"}`+"\n")
			},
			want: "make the configs agree by hand first",
			fix: func(f *fixture) {
				f.write(".rlsbl-monorepo/releasables/widget/config.json", `{"publish_mode": "ci"}`+"\n")
			},
		},
		{
			name: "batch_limits in a member's config", fixture: workspace,
			breakIt: func(f *fixture) {
				f.write("widget/.rlsbl/config.json", `{"targets": ["npm"], "publish_mode": "ci", `+npmPipeline+`, "batch_limits": {"exclusions": []}}`+"\n")
			},
			want: "move its exclusions into",
			fix: func(f *fixture) {
				f.write("widget/.rlsbl/config.json", `{"targets": ["npm"], "publish_mode": "ci", `+npmPipeline+`}`+"\n")
			},
		},
		{
			name: "a stray old-layout directory", fixture: workspace,
			breakIt: func(f *fixture) { f.write("docs/.rlsbl/config.json", "{}\n") },
			want:    "docs/.rlsbl/ is an old-layout directory of no declared member",
			fix:     func(f *fixture) { f.remove("docs/.rlsbl") },
		},
		{
			name: "an undeclared releasable directory without a closed history", fixture: workspace,
			breakIt: func(f *fixture) {
				f.write(".rlsbl-monorepo/releasables/gizmo/version", "0.1.0\n")
			},
			want: "record the closed history with `rlsbl transition record --release-history-closed gizmo`",
			fix: func(f *fixture) {
				f.write(".rlsbl-monorepo/transitions.jsonl", eventLine("release-history-closed", `"subject":"gizmo","reason":"merged into widget"`))
			},
		},
		{
			name: "a changelog line without user_facing", fixture: standalone,
			breakIt: func(f *fixture) {
				f.write(".rlsbl/changes/unreleased.jsonl", `{"format_version":1,"commits":["`+someCommit+`"]}`+"\n")
			},
			want: "add true or false by hand",
			fix: func(f *fixture) {
				f.write(".rlsbl/changes/unreleased.jsonl", `{"format_version":1,"commits":["`+someCommit+`"],"user_facing":false}`+"\n")
			},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hygiene.Isolate(t)
			f := c.fixture(t)
			c.breakIt(f)
			f.commit("the state under test")
			refusal := f.refused(c.want)
			if strings.Contains(c.want, "Hand edit") && !strings.Contains(refusal, "Hand edit") {
				t.Fatalf("the refusal names no hand edit:\n%s", refusal)
			}
			c.fix(f)
			f.commit("the edit the refusal names")
			if _, err := f.plan(); err != nil && strings.Contains(err.Error(), c.want) {
				t.Fatalf("the edit did not clear the refusal:\n%v", err)
			} else if err != nil {
				t.Fatalf("the edit cleared the refusal, and the run is refused for another reason:\n%v", err)
			}
		})
	}
}

func TestLicensesThatContradictTheManifestsAreRefusedUntilTheyAgree(t *testing.T) {
	hygiene.Isolate(t)
	f := standalone(t)
	f.licenses = map[string]string{"portal": "Apache-2.0", "gizmo": "MIT"}
	refusal := f.refused("declares Apache-2.0 for \"portal\", but its npm manifest in . says MIT")
	contains(t, refusal, `names "gizmo", which is no releasable of this repository`)
	f.licenses = map[string]string{"portal": "MIT"}
	f.mustPlan()
}

func TestAHalfMigratedRepositoryIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	f := standalone(t)
	f.write(".strictmetadata/releasables/releasables.toml", "format_version = 1\n")
	f.commit("half migrated")
	f.refused("the repository is half migrated")
	f.remove(".strictmetadata/releasables")
	f.commit("undo the half")
	f.mustPlan()
}
