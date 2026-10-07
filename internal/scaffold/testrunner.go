package scaffold

import (
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// While rlsbl:test-sandbox is on, scaffold renders the sandboxed test
// runner, a bubblewrap script, to the runner_path the test runner's
// settings (.strictmetadata/test-runner/test-runner.toml) declare: the
// outer layer of testisolation's protection, which every declared CI
// workflow must run. The member whose territory holds runner_path renders
// it.

// testRunnerTemplate is the runner's template.
const testRunnerTemplate = "shared/test-sandbox.sh.tpl"

// TestRunnerVars are the runner template's variables for the settings r.
func TestRunnerVars(r *declarations.TestRunner) Vars {
	var env []string
	names := make([]string, 0, len(r.ExtraEnv))
	for name := range r.ExtraEnv {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		env = append(env, fmt.Sprintf("  --setenv %s %s", name, r.ExtraEnv[name]))
	}
	carry := make([]string, len(r.CarryIgnored))
	for i, p := range r.CarryIgnored {
		carry[i] = "'" + p + "'"
	}
	uv := ""
	if containsString(r.Caches, declarations.CacheUV) {
		uv = "1"
	}
	return Vars{
		"sandboxRunnerPath":   r.RunnerPath,
		"sandboxRootRelative": rootRelative(r.RunnerPath),
		"sandboxCommand":      r.Command,
		"sandboxDefaultArgs":  r.DefaultArgs,
		"sandboxCaches":       strings.Join(r.Caches, " "),
		"sandboxPrewarm":      strings.Join(r.Prewarm, "\n"),
		"sandboxExtraEnv":     strings.Join(env, "\n"),
		"sandboxCarryIgnored": strings.Join(carry, " "),
		// The dev overlay block is uv's (it excludes packages from uv sync
		// and installs them editable), so it is rendered only for a runner
		// declaring the uv cache.
		"sandboxUvOverlays": uv,
	}
}

// rootRelative is the path from the runner's directory back to the
// repository root.
func rootRelative(runnerPath string) string {
	parent := parentDir(runnerPath)
	if parent == "." || parent == "" {
		return "."
	}
	n := len(strings.Split(parent, "/"))
	return strings.TrimSuffix(strings.Repeat("../", n), "/")
}

// testRunnerRender renders the runner when the test runner's settings
// exist and the member's territory holds their runner_path.
func (c *memberContext) testRunnerRender() (render, bool, error) {
	if c.runner == nil {
		return render{}, false, nil
	}
	owner, ok := c.ws.Declarations.MemberForPath(c.runner.RunnerPath)
	if !ok || owner.Path != c.member.Path {
		return render{}, false, nil
	}
	text, err := renderTemplate(testRunnerTemplate, TestRunnerVars(c.runner))
	if err != nil {
		return render{}, false, err
	}
	return render{path: c.runner.RunnerPath, theirs: text, executable: true}, true, nil
}
