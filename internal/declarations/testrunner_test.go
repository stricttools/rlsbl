package declarations

import (
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

const testRunnerSample = `format_version = 1
runner_path = "scripts/test.sh"
command = "uv sync --offline && uv run --offline pytest"
default_args = "-q -n auto"
caches = ["uv", "go", "python_user_base"]
prewarm = ["scripts/test-prewarm.sh"]
ci_workflows = [".github/workflows/ci.yml"]
carry_ignored = [".test-tools"]

[extra_env]
LEGACY_SANDBOX_VAR = "1"
`

func TestEveryTestRunnerKeyDecodes(t *testing.T) {
	hygiene.Isolate(t)
	got, err := ParseTestRunner([]byte(testRunnerSample))
	if err != nil {
		t.Fatal(err)
	}
	want := &TestRunner{
		RunnerPath:   "scripts/test.sh",
		Command:      "uv sync --offline && uv run --offline pytest",
		DefaultArgs:  "-q -n auto",
		Caches:       []string{"uv", "go", "python_user_base"},
		Prewarm:      []string{"scripts/test-prewarm.sh"},
		ExtraEnv:     map[string]string{"LEGACY_SANDBOX_VAR": "1"},
		CIWorkflows:  []string{".github/workflows/ci.yml"},
		CarryIgnored: []string{".test-tools"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("decoded %#v\nwant %#v", got, want)
	}
}

func TestTestRunnerRefusals(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ name, old, new string }{
		{"an unknown key", "prewarm = ", "prewarm_offline = true\nprewarm = "},
		{"a missing command", "command = \"uv sync --offline && uv run --offline pytest\"\n", ""},
		{"an absolute runner path", "runner_path = \"scripts/test.sh\"", "runner_path = \"/scripts/test.sh\""},
		{"a runner path climbing out", "runner_path = \"scripts/test.sh\"", "runner_path = \"../test.sh\""},
		{"a single quote in the command", "uv run --offline pytest\"", "uv run --offline pytest -k 'x'\""},
		{"a single quote in the default args", "-q -n auto", "-q -k 'x'"},
		{"a cache the runner does not bind", "\"python_user_base\"]", "\"cargo\"]"},
		{"a workflow outside the repository", ".github/workflows/ci.yml", "../ci.yml"},
		{"the sandbox variable redeclared", "LEGACY_SANDBOX_VAR = \"1\"", SandboxVariable + " = \"1\""},
		{"an environment value the runner cannot pass", "LEGACY_SANDBOX_VAR = \"1\"", "LEGACY_SANDBOX_VAR = \"a b\""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if strings.Count(testRunnerSample, c.old) != 1 {
				t.Fatalf("the sample holds %q other than once", c.old)
			}
			if _, err := ParseTestRunner([]byte(strings.Replace(testRunnerSample, c.old, c.new, 1))); err == nil {
				t.Fatal("ParseTestRunner accepted it")
			}
		})
	}
}

func TestLoadTestRunnerReportsAMissingFile(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	if _, found, err := LoadTestRunner(root); found || err != nil {
		t.Fatalf("LoadTestRunner = %t, %v; want not found and no error", found, err)
	}
	writeFixture(t, root, TestRunnerFile, testRunnerSample)
	if runner, found, err := LoadTestRunner(root); !found || err != nil || runner.RunnerPath != "scripts/test.sh" {
		t.Fatalf("LoadTestRunner = %#v, %t, %v", runner, found, err)
	}
}

func TestTheEnvironmentFile(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	path := root + "/release.env"
	_, err := LoadEnvironmentFile(path)
	if err == nil || !strings.Contains(err.Error(), "create it") {
		t.Fatalf("LoadEnvironmentFile = %v, want a refusal naming the fix", err)
	}
	writeFixture(t, root, "release.env", "# tokens\n\nNPM_TOKEN=\"abc\"\nPLAIN = value \nQUOTED='x y'\n")
	env, err := LoadEnvironmentFile(path)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"NPM_TOKEN": "abc", "PLAIN": "value", "QUOTED": "x y"}
	if !reflect.DeepEqual(env, want) {
		t.Fatalf("env = %#v, want %#v", env, want)
	}
	if _, err := ParseEnvironment("export TOKEN\n", "release.env"); err == nil || !strings.Contains(err.Error(), "release.env:1") {
		t.Fatalf("ParseEnvironment = %v, want a refusal naming the line", err)
	}
}
