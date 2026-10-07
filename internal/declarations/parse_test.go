package declarations

import (
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// fullSample carries every key releasables.toml has, each with a value that
// is not its zero, so a key the decode drops shows up as a zero.
const fullSample = `format_version = 1
repository_layout = "workspace"
release_branches = ["main", "release"]
github_repository = "acme/portal"
environment_file = "~/Projects/.env"

[timeouts]
push_seconds = 300
ci_seconds = 3600
check_seconds = 900
hook_seconds = 10800

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "ci"
publish_ci_check_pattern = '^(test|lint)$'
deploy_command = ["tool", "deploy", "portal-server", "--version", "{version}"]
hooks = { pre_checks = ["make lint"], pre_release = [{ cmd = "make dist", dir = "cmd", env = { MODE = "release" } }], post_release = ["echo done"] }

[[releasables]]
name = "widget"
tag_format = "{name}@v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
releasable = "portal"
library = true
test_only = true
depends_on = ["widget"]
import_name = "portal"
registry_name = "portal-cli"
description = "The portal command."
lint_allow = ["os/exec"]
internal_dep_floors = ["widget"]
targets = [{ name = "go", path = "." }, { name = "npm", path = "web" }]
hooks = { pre_release = ["make test"] }
external_checks = [{ name = "spell", tag = "preflight", command = "codespell .", depends_on = ["lock"], cwd = "docs" }]
test = { pypi_markers = "not slow", go_command = "scripts/suite.sh" }

[[members.pipelines]]
name = "go"
type = "go"
target = "go"
local = true
artifact = "binary"
install_paths = ["./cmd/portal"]
homebrew_tap = "homebrew-tap"

[[members.pipelines]]
name = "npm"
type = "npm"
target = "npm"
local = false
artifact = "go-binary"
binary_pipeline = "go"

[[members]]
path = "widget"
name = "widget"
releasable = "widget"
dev_only = true
`

// standaloneSample is the smallest standalone declaration.
const standaloneSample = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]

[[releasables]]
name = "gadget"
tag_format = "v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
releasable = "gadget"
`

// workspaceOfRootOnly is a workspace whose only member is the root member.
const workspaceOfRootOnly = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]
releasables = []

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false
`

func mustParse(t *testing.T, text string) *Releasables {
	t.Helper()
	d, err := Parse([]byte(text))
	if err != nil {
		t.Fatalf("Parse refused:\n%s\n\n%v", text, err)
	}
	return d
}

// refusal parses text and returns the refusal's text, failing when it
// parses.
func refusal(t *testing.T, text string) string {
	t.Helper()
	_, err := Parse([]byte(text))
	if err == nil {
		t.Fatalf("Parse accepted:\n%s", text)
	}
	var refused *Error
	if !errors.As(err, &refused) {
		t.Fatalf("Parse returned %T, not *Error: %v", err, err)
	}
	return err.Error()
}

func TestEveryKeyOfTheDocumentedFileDecodes(t *testing.T) {
	hygiene.Isolate(t)
	d := mustParse(t, fullSample)
	want := &Releasables{
		Layout:           LayoutWorkspace,
		ReleaseBranches:  []string{"main", "release"},
		GitHubRepository: "acme/portal",
		EnvironmentFile:  "~/Projects/.env",
		Timeouts:         Timeouts{PushSeconds: 300, CISeconds: 3600, CheckSeconds: 900, HookSeconds: 10800},
		Releasables: []Releasable{
			{
				Name:                  "portal",
				TagFormat:             "v{version}",
				PublishMode:           PublishCI,
				PublishCICheckPattern: "^(test|lint)$",
				DeployCommand:         []string{"tool", "deploy", "portal-server", "--version", "{version}"},
				Hooks: Hooks{
					PreChecks:   []Hook{{Command: "make lint"}},
					PreRelease:  []Hook{{Command: "make dist", Dir: "cmd", Env: map[string]string{"MODE": "release"}}},
					PostRelease: []Hook{{Command: "echo done"}},
				},
			},
			{Name: "widget", TagFormat: "{name}@v{version}", PublishMode: PublishNone},
		},
		Members: []Member{
			{
				Path:              ".",
				Name:              "root",
				Releasable:        "portal",
				Library:           true,
				TestOnly:          true,
				DependsOn:         []string{"widget"},
				ImportName:        "portal",
				RegistryName:      "portal-cli",
				Description:       "The portal command.",
				LintAllow:         []string{"os/exec"},
				InternalDepFloors: []string{"widget"},
				Targets:           []Target{{Name: "go", Path: "."}, {Name: "npm", Path: "web"}},
				Hooks:             Hooks{PreRelease: []Hook{{Command: "make test"}}},
				ExternalChecks: []ExternalCheck{
					{Name: "spell", Tag: "preflight", Command: "codespell .", DependsOn: []string{"lock"}, Cwd: "docs"},
				},
				Test: TestSettings{PyPIMarkers: "not slow", GoCommand: "scripts/suite.sh"},
				Pipelines: []Pipeline{
					{Name: "go", Type: "go", Target: "go", Local: true, Artifact: "binary", InstallPaths: []string{"./cmd/portal"}, HomebrewTap: "homebrew-tap"},
					{Name: "npm", Type: "npm", Target: "npm", Artifact: "go-binary", BinaryPipeline: "go"},
				},
			},
			{Path: "widget", Name: "widget", Releasable: "widget", DevOnly: true},
		},
	}
	if !reflect.DeepEqual(d, want) {
		t.Fatalf("decoded\n%#v\nwant\n%#v", d, want)
	}
}

func TestAnUnknownKeyIsRefusedAtEveryLevel(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ name, old, new string }{
		{"top level", "release_branches = [\"main\", \"release\"]\n", "release_branches = [\"main\", \"release\"]\nreleased_branches = [\"main\"]\n"},
		{"timeouts", "hook_seconds = 10800\n", "hook_seconds = 10800\nbuild_seconds = 60\n"},
		{"releasable", "publish_mode = \"none\"\n", "publish_mode = \"none\"\nsubtree_remote = \"git@example.com:x\"\n"},
		{"hooks", "post_release = [\"echo done\"] }", "post_release = [\"echo done\"], post_publish = [\"x\"] }"},
		{"hook command", "dir = \"cmd\",", "dir = \"cmd\", shell = \"bash\","},
		{"member", "dev_only = true\n", "dev_only = true\nwatch = [\"src\"]\n"},
		{"target", "{ name = \"npm\", path = \"web\" }", "{ name = \"npm\", path = \"web\", provenance = true }"},
		{"external", "cwd = \"docs\" }", "cwd = \"docs\", style = \"freeform\" }"},
		{"test settings", "go_command = \"scripts/suite.sh\" }", "go_command = \"scripts/suite.sh\", markers = \"x\" }"},
		{"pipeline", "homebrew_tap = \"homebrew-tap\"\n", "homebrew_tap = \"homebrew-tap\"\nassets = true\n"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if strings.Count(fullSample, c.old) != 1 {
				t.Fatalf("the sample holds %q other than once", c.old)
			}
			refusal(t, strings.Replace(fullSample, c.old, c.new, 1))
		})
	}
}

func TestTheLayoutIsDeclaredNeverCounted(t *testing.T) {
	hygiene.Isolate(t)
	ws := mustParse(t, workspaceOfRootOnly)
	if !ws.IsWorkspace() || len(ws.Members) != 1 {
		t.Fatalf("a workspace whose only member is the root member read as layout %q with %d members", ws.Layout, len(ws.Members))
	}
	standalone := mustParse(t, standaloneSample)
	if standalone.IsWorkspace() {
		t.Fatalf("a standalone declaration read as a workspace")
	}
	refusal(t, strings.Replace(workspaceOfRootOnly, "repository_layout = \"workspace\"\n", "", 1))
}

func TestAStandaloneLayoutWithSeveralMembersIsRefusedUntilItDeclaresAWorkspace(t *testing.T) {
	hygiene.Isolate(t)
	text := standaloneSample + "\n[[members]]\npath = \"widget\"\nname = \"widget\"\nreleasable = \"gadget\"\n"
	got := refusal(t, text)
	if !strings.Contains(got, "repository_layout = \"workspace\"") {
		t.Fatalf("the refusal does not name the fix:\n%s", got)
	}
	mustParse(t, strings.Replace(text, "repository_layout = \"standalone\"", "repository_layout = \"workspace\"", 1))
}

func TestAStandaloneRootVersionedUnderNoReleasableIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	got := refusal(t, strings.Replace(standaloneSample, "releasable = \"gadget\"", "releasable = false", 1))
	if !strings.Contains(got, "standalone") {
		t.Fatalf("the refusal does not name the layout:\n%s", got)
	}
}

func TestTagFormatsAndNamesAreRequiredNeverDerived(t *testing.T) {
	hygiene.Isolate(t)
	for _, line := range []string{"tag_format = \"v{version}\"\n", "name = \"gadget\"\n", "publish_mode = \"none\"\n"} {
		refusal(t, strings.Replace(standaloneSample, line, "", 1))
	}
	refusal(t, strings.Replace(standaloneSample, "name = \"root\"\n", "", 1))
}

func TestTheRootMemberRules(t *testing.T) {
	hygiene.Isolate(t)

	t.Run("a declaration without a root member is refused and the shown member clears it", func(t *testing.T) {
		text := strings.Replace(workspaceOfRootOnly, "[[members]]\npath = \".\"\nname = \"root\"\ndev_only = true\nreleasable = false\n", "[[members]]\npath = \"tools\"\nname = \"tools\"\nreleasable = false\n", 1)
		got := refusal(t, text)
		if !strings.Contains(got, rootMemberSnippet) {
			t.Fatalf("the refusal does not show the root member to add:\n%s", got)
		}
		snippet := strings.ReplaceAll(rootMemberSnippet, "\n  ", "\n")
		mustParse(t, text+"\n"+strings.TrimPrefix(snippet, "  "))
	})

	t.Run("a root member named otherwise is refused until it is named root", func(t *testing.T) {
		text := strings.Replace(workspaceOfRootOnly, "name = \"root\"", "name = \"base\"", 1)
		got := refusal(t, text)
		if !strings.Contains(got, "Set name = \"root\"") {
			t.Fatalf("the refusal does not name the fix:\n%s", got)
		}
		mustParse(t, strings.Replace(text, "name = \"base\"", "name = \"root\"", 1))
	})

	t.Run("a member elsewhere named root is refused", func(t *testing.T) {
		refusal(t, workspaceOfRootOnly+"\n[[members]]\npath = \"tools\"\nname = \"root\"\nreleasable = false\n")
	})

	t.Run("two members at one path are refused", func(t *testing.T) {
		text := workspaceOfRootOnly + "\n[[members]]\npath = \"tools\"\nname = \"tools\"\nreleasable = false\n\n[[members]]\npath = \"tools\"\nname = \"more\"\nreleasable = false\n"
		refusal(t, text)
	})
}

func TestAMemberPathIsCanonicalOrRefusedNamingTheSpelling(t *testing.T) {
	hygiene.Isolate(t)
	for raw, canonical := range map[string]string{"tools/": "tools", "./tools": "tools", "tools//cli": "tools/cli"} {
		text := workspaceOfRootOnly + "\n[[members]]\npath = \"" + raw + "\"\nname = \"tools\"\nreleasable = false\n"
		got := refusal(t, text)
		if !strings.Contains(got, "Write it as \""+canonical+"\"") {
			t.Fatalf("the refusal of %q does not name %q:\n%s", raw, canonical, got)
		}
		mustParse(t, strings.Replace(text, "path = \""+raw+"\"", "path = \""+canonical+"\"", 1))
	}
	for _, raw := range []string{"../outside", "/abs"} {
		refusal(t, workspaceOfRootOnly+"\n[[members]]\npath = \""+raw+"\"\nname = \"tools\"\nreleasable = false\n")
	}
}

func TestAMemberNamesADeclaredReleasable(t *testing.T) {
	hygiene.Isolate(t)
	got := refusal(t, strings.Replace(standaloneSample, "releasable = \"gadget\"", "releasable = \"gizmo\"", 1))
	if !strings.Contains(got, "gizmo") || !strings.Contains(got, "gadget") {
		t.Fatalf("the refusal does not name the reference and the declared releasables:\n%s", got)
	}
	refusal(t, strings.Replace(standaloneSample, "releasable = \"gadget\"", "releasable = true", 1))
}

func TestAReleasableNoMemberNamesIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	text := strings.Replace(fullSample, "releasable = \"widget\"\n", "releasable = false\n", 1)
	if got := refusal(t, text); !strings.Contains(got, "\"widget\"") {
		t.Fatalf("the refusal does not name the releasable:\n%s", got)
	}
}

func TestTheRootReleasableOfAWorkspacePublishingFromCIDeclaresItsCheckPattern(t *testing.T) {
	hygiene.Isolate(t)
	const line = "publish_ci_check_pattern = '^(test|lint)$'\n"
	got := refusal(t, strings.Replace(fullSample, line, "", 1))
	_, example, found := strings.Cut(got, "for example:\n\n")
	if !found {
		t.Fatalf("the refusal shows no example:\n%s", got)
	}
	example = strings.TrimSpace(strings.SplitN(example, "\n", 2)[0])
	mustParse(t, strings.Replace(fullSample, line, example+"\n", 1))

	// Declared anywhere else, nothing reads it.
	refusal(t, strings.Replace(fullSample, "publish_mode = \"none\"\n", "publish_mode = \"none\"\n"+line, 1))
	refusal(t, strings.Replace(standaloneSample, "publish_mode = \"none\"\n", "publish_mode = \"ci\"\n"+line, 1))
}

func TestTagFormatProblems(t *testing.T) {
	hygiene.Isolate(t)
	for _, c := range []struct {
		format string
		ok     bool
	}{
		{"v{version}", true},
		{"{name}@v{version}", true},
		{"cmd/portal/v{version}", true},
		{"v{version}{version}", false},
		{"v", false},
		{"{owner}/v{version}", false},
		{"bad tag/v{version}", false},
		{"v{version}.lock", false},
		{"-v{version}", false},
		{"/v{version}", false},
	} {
		if got := TagFormatProblem(c.format, "portal") == ""; got != c.ok {
			t.Errorf("TagFormatProblem(%q) accepted = %t, want %t", c.format, got, c.ok)
		}
	}
}

func TestNameProblems(t *testing.T) {
	hygiene.Isolate(t)
	for _, c := range []struct {
		name string
		ok   bool
	}{
		{"portal", true},
		{"@scope", true},
		{"a.b", true},
		{"", false},
		{".", false},
		{"..", false},
		{".hidden", false},
		{"a/b", false},
		{"a b", false},
		{"a\\b", false},
		{"tab\tsep", false},
	} {
		if got := NameProblem(c.name) == ""; got != c.ok {
			t.Errorf("NameProblem(%q) accepted = %t, want %t", c.name, got, c.ok)
		}
	}
}

func TestPipelineRules(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ name, old, new string }{
		{"a type other than the target's", "type = \"npm\"\n", "type = \"pypi\"\n"},
		{"a target the member does not declare", "name = \"npm\"\ntype = \"npm\"\ntarget = \"npm\"", "name = \"npm\"\ntype = \"pypi\"\ntarget = \"pypi\""},
		{"a go artifact on npm", "artifact = \"go-binary\"", "artifact = \"binary\""},
		{"a package artifact on go", "artifact = \"binary\"", "artifact = \"package\""},
		{"a local go pipeline without installs", "install_paths = [\"./cmd/portal\"]\n", ""},
		{"a homebrew tap on a library", "artifact = \"binary\"\ninstall_paths", "artifact = \"library\"\ninstall_paths"},
		{"a go-binary pipeline wrapping none", "binary_pipeline = \"go\"\n", ""},
		{"a go-binary pipeline wrapping nothing declared", "binary_pipeline = \"go\"", "binary_pipeline = \"rust\""},
		{"install paths on npm", "binary_pipeline = \"go\"\n", "binary_pipeline = \"go\"\ninstall_paths = [\"x\"]\n"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if strings.Count(fullSample, c.old) != 1 {
				t.Fatalf("the sample holds %q other than once", c.old)
			}
			refusal(t, strings.Replace(fullSample, c.old, c.new, 1))
		})
	}
}

func TestPathsAMemberDeclaresStayInItsTerritory(t *testing.T) {
	hygiene.Isolate(t)
	got := refusal(t, strings.Replace(fullSample, "{ name = \"npm\", path = \"web\" }", "{ name = \"npm\", path = \"widget\" }", 1))
	if !strings.Contains(got, "\"widget\"") {
		t.Fatalf("the refusal does not name the owning member:\n%s", got)
	}
	refusal(t, strings.Replace(fullSample, "dir = \"cmd\"", "dir = \"widget/src\"", 1))
	refusal(t, strings.Replace(fullSample, "dir = \"cmd\"", "dir = \"../elsewhere\"", 1))
}

func TestDependsOnNamesOtherDeclaredMembers(t *testing.T) {
	hygiene.Isolate(t)
	refusal(t, strings.Replace(fullSample, "depends_on = [\"widget\"]", "depends_on = [\"gizmo\"]", 1))
	refusal(t, strings.Replace(fullSample, "depends_on = [\"widget\"]", "depends_on = [\"root\"]", 1))
	refusal(t, strings.Replace(fullSample, "depends_on = [\"widget\"]", "depends_on = [\"widget\", \"widget\"]", 1))
}

func TestExternalChecks(t *testing.T) {
	hygiene.Isolate(t)
	refusal(t, strings.Replace(fullSample, "name = \"spell\"", "name = \"Spell*\"", 1))
	got := refusal(t, strings.Replace(fullSample, "command = \"codespell .\"", "command = \"LANG=C codespell .\"", 1))
	if !strings.Contains(got, "env LANG=C") {
		t.Fatalf("the refusal does not name the env form:\n%s", got)
	}
	mustParse(t, strings.Replace(fullSample, "command = \"codespell .\"", "command = \"env LANG=C codespell .\"", 1))
	twice := strings.Replace(fullSample, "dev_only = true\n", "dev_only = true\nexternal_checks = [{ name = \"spell\", tag = \"preflight\", command = \"true\" }]\n", 1)
	refusal(t, twice)
}

func TestAnEmptyHookCommandIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	refusal(t, strings.Replace(fullSample, "pre_checks = [\"make lint\"]", "pre_checks = [\" \"]", 1))
}

func TestMemberForPathTakesTheMostSpecificClaim(t *testing.T) {
	hygiene.Isolate(t)
	d := mustParse(t, workspaceOfRootOnly+"\n[[members]]\npath = \"pkg\"\nname = \"pkg\"\nreleasable = false\n\n[[members]]\npath = \"pkg/inner\"\nname = \"inner\"\nreleasable = false\n")
	for _, c := range [][2]string{
		{".", "root"},
		{"README.md", "root"},
		{"pkg", "pkg"},
		{"pkg/a.go", "pkg"},
		{"pkg/inner/a", "inner"},
		{"pkgx/a.go", "root"},
		{"pkg/innerx/a", "pkg"},
	} {
		m, ok := d.MemberForPath(c[0])
		if !ok || m.Name != c[1] {
			t.Errorf("MemberForPath(%q) = %q, want %q", c[0], m.Name, c[1])
		}
	}
}
