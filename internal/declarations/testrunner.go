package declarations

import (
	"fmt"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations/testrunnerspec"
)

// SandboxVariable is the environment variable the runner always exports;
// the testisolation floor reads it to lift its refusal of a bare test run.
const SandboxVariable = "TESTISOLATION_SANDBOX"

// The caches the runner script knows how to bind into the sandbox.
const (
	CacheUV             = "uv"
	CacheGo             = "go"
	CachePythonUserBase = "python_user_base"
)

// TestRunner is the sandboxed test runner's settings,
// .strictmetadata/test-runner/test-runner.toml. Whether the file may exist
// follows the rlsbl:test-sandbox option: it is required while the option is
// on and refused while it is off, which internal/options decides.
type TestRunner struct {
	// RunnerPath is where scaffold writes the runner script, canonical and
	// repository-relative.
	RunnerPath string
	// Command runs inside the sandbox.
	Command string
	// DefaultArgs, or empty, are passed when the runner is given none.
	DefaultArgs string
	Caches      []string
	// Prewarm commands run outside the sandbox, with network access, before
	// it is entered.
	Prewarm []string
	// ExtraEnv is exported inside the sandbox; nil when none.
	ExtraEnv map[string]string
	// CIWorkflows are workflow files that must run the runner.
	CIWorkflows []string
	// CarryIgnored are git-ignored paths copied into the sandbox when
	// present.
	CarryIgnored []string
}

type rawTestRunner struct {
	FormatVersion int64             `toml:"format_version,required"`
	RunnerPath    string            `toml:"runner_path,required"`
	Command       string            `toml:"command,required"`
	DefaultArgs   *string           `toml:"default_args"`
	Caches        []string          `toml:"caches"`
	Prewarm       []string          `toml:"prewarm"`
	ExtraEnv      map[string]string `toml:"extra_env"`
	CIWorkflows   []string          `toml:"ci_workflows"`
	CarryIgnored  []string          `toml:"carry_ignored"`
}

// LoadTestRunner reads the test runner's settings of the repository rooted
// at root; found is false when the file does not exist.
func LoadTestRunner(root string) (runner *TestRunner, found bool, err error) {
	data, found, err := readRecord(root, TestRunnerFile)
	if err != nil || !found {
		return nil, false, err
	}
	runner, err = ParseTestRunner(data)
	if err != nil {
		return nil, true, err
	}
	return runner, true, nil
}

// ParseTestRunner parses the test runner's settings: the generated
// validator, the strict typed decode, then the rules a schema cannot state.
func ParseTestRunner(data []byte) (*TestRunner, error) {
	if _, diags := testrunnerspec.ValidateBytes(data, "toml"); len(diags) > 0 {
		return nil, refuse(TestRunnerFile, diagnosticProblems(diags))
	}
	raw, err := tomledit.Unmarshal[rawTestRunner](data)
	if err != nil {
		return nil, refuse(TestRunnerFile, []string{err.Error()})
	}
	t := &TestRunner{
		RunnerPath:   raw.RunnerPath,
		Command:      raw.Command,
		DefaultArgs:  deref(raw.DefaultArgs),
		Caches:       raw.Caches,
		Prewarm:      raw.Prewarm,
		ExtraEnv:     raw.ExtraEnv,
		CIWorkflows:  raw.CIWorkflows,
		CarryIgnored: raw.CarryIgnored,
	}
	var problems []string
	if raw.FormatVersion != 1 {
		problems = append(problems, fmt.Sprintf("format_version is %d; this rlsbl reads format_version 1", raw.FormatVersion))
	}
	problems = append(problems, t.problems()...)
	if err := refuse(TestRunnerFile, problems); err != nil {
		return nil, err
	}
	return t, nil
}

// singleQuoteProblem refuses a value the runner script embeds in a
// single-quoted shell literal, which a single quote would end.
func singleQuoteProblem(key, value string) string {
	if !strings.Contains(value, "'") {
		return ""
	}
	return fmt.Sprintf("%s holds a single quote, and the runner embeds it in a single-quoted shell literal; use double quotes, or move the command into a script and name the script", key)
}

func (t *TestRunner) problems() []string {
	var problems []string
	add := func(problem string) {
		if problem != "" {
			problems = append(problems, problem)
		}
	}
	if problem := PathProblem(t.RunnerPath); problem != "" {
		add("runner_path: " + problem)
	} else if t.RunnerPath == RootPath {
		add("runner_path names the repository root; name the script file")
	}
	if strings.TrimSpace(t.Command) == "" {
		add("command is blank; name the command the runner runs inside the sandbox")
	}
	add(singleQuoteProblem("command", t.Command))
	add(singleQuoteProblem("default_args", t.DefaultArgs))
	for _, c := range duplicates(t.Caches) {
		add(fmt.Sprintf("caches names %q more than once", c))
	}
	for _, c := range t.Caches {
		switch c {
		case CacheUV, CacheGo, CachePythonUserBase:
		default:
			add(fmt.Sprintf("caches names %q, which the runner does not bind; it binds %s, %s, and %s", c, CacheUV, CacheGo, CachePythonUserBase))
		}
	}
	for _, p := range t.CIWorkflows {
		if problem := PathProblem(p); problem != "" {
			add("ci_workflows: " + problem)
		}
	}
	for _, p := range t.CarryIgnored {
		if problem := PathProblem(p); problem != "" {
			add("carry_ignored: " + problem)
		}
		add(singleQuoteProblem("carry_ignored entry "+p, p))
	}
	names := make([]string, 0, len(t.ExtraEnv))
	for name := range t.ExtraEnv {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if name == SandboxVariable {
			add(fmt.Sprintf("extra_env declares %s, which the runner always exports (the testisolation floor reads it); delete it", SandboxVariable))
		}
		if strings.TrimSpace(t.ExtraEnv[name]) == "" {
			add(fmt.Sprintf("extra_env.%s is blank", name))
		}
	}
	return problems
}

// WriteTestRunner writes data as the test runner's settings of the
// repository rooted at root, after parsing it, through the effects handle.
func WriteTestRunner(e *strictcli.Effects, root string, data []byte) (*TestRunner, error) {
	t, err := ParseTestRunner(data)
	if err != nil {
		return nil, err
	}
	if err := writeRecord(e, root, TestRunnerDir, TestRunnerFile, data); err != nil {
		return nil, err
	}
	return t, nil
}
