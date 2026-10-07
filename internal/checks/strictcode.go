package checks

import (
	"encoding/json"
	"fmt"
	"os/exec"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// RequiredStrictcodeRules are the strictcode rules every release runs: the
// rules the source analyses rlsbl moved to strictcode became. A strictcode
// lacking any of them is refused, whatever version it reports, since a
// strictcode built from source reports the version its release has not
// bumped yet.
var RequiredStrictcodeRules = []string{
	"dead-modules",
	"dead-workspace-packages",
	"deps-dev-in-production",
	"deps-runtime-test-only",
	"deps-stale",
	"deps-undeclared",
	"deps-unused",
	"format",
	"format-scope-guard",
	"import-cycles",
	"library-entry-point",
	"library-forbidden-imports",
	"library-stdout",
	"lint",
	"lint-scope-guard",
	"stale-suppression",
	"strictspec-certificate",
	"type-check",
	"type-check-scope-guard",
}

// StrictcodeInstall is the command that installs a strictcode with every
// rule rlsbl requires.
const StrictcodeInstall = "go install github.com/smm-h/strictcode/cmd/strictcode@latest"

// The strictcode check family: one check, running the strictcode on PATH.
func strictcodeChecks() []check {
	return []check{repositoryWide(errorCheck("strictcode", checkStrictcode))}
}

// strictcodeEnvelope is the part of strictcli's machine-mode document the
// check reads from each strictcode command.
type strictcodeEnvelope struct {
	ExitCode    int             `json:"exit_code"`
	Payload     json.RawMessage `json:"payload"`
	Diagnostics []struct {
		Message string `json:"message"`
	} `json:"diagnostics"`
}

// strictcodeRules is the payload of `strictcode registry rules --json`.
type strictcodeRules struct {
	Rules []struct {
		ID string `json:"id"`
	} `json:"rules"`
}

// strictcodeFindings is the payload of `strictcode analyze --json`.
type strictcodeFindings struct {
	Findings []struct {
		Rule     string `json:"rule"`
		Severity string `json:"severity"`
		Message  string `json:"message"`
		Target   struct {
			File string `json:"file"`
			Line int    `json:"line"`
		} `json:"target"`
	} `json:"findings"`
}

// runStrictcode runs strictcode with args and reads its machine-mode
// document. Each run is declared an observed run, which is what lets the
// read-only check command start it. `strictcode registry rules` writes
// nothing; strictcode declares `analyze` mutating, because its lint, format,
// and type-check rules, while on, start their tools with uv run, which can
// create or update .venv and rewrite uv.lock. The check is declared impure,
// so a dry run lists it without starting strictcode.
func runStrictcode(c *Context, args ...string) (strictcodeEnvelope, strictcli.Completed, error) {
	argv := []interface{}{"strictcode"}
	for _, a := range args {
		argv = append(argv, a)
	}
	done, err := c.Effects().Run(argv, strictcli.Cwd(c.Root()), strictcli.Check(false), strictcli.Timeout(c.CheckTimeout()), strictcli.Observe())
	if err != nil {
		return strictcodeEnvelope{}, done, fmt.Errorf("`strictcode %s` did not finish: %w", strings.Join(args, " "), err)
	}
	var env strictcodeEnvelope
	if err := json.Unmarshal([]byte(done.Stdout()), &env); err != nil {
		return strictcodeEnvelope{}, done, fmt.Errorf("`strictcode %s` exited %d without the JSON document --json promises (%v): %s", strings.Join(args, " "), done.ExitCode(), err, firstLine(done.Stderr()+done.Stdout()))
	}
	return env, done, nil
}

// envelopeFailure words a strictcode command that failed, with what it
// said.
func envelopeFailure(command string, env strictcodeEnvelope, done strictcli.Completed) string {
	var said []string
	for _, d := range env.Diagnostics {
		if strings.TrimSpace(d.Message) != "" {
			said = append(said, d.Message)
		}
	}
	if len(said) == 0 {
		said = reportableLines(done.Stderr(), externalOutputLines)
	}
	return fmt.Sprintf("`%s` exited %d: %s", command, done.ExitCode(), strings.Join(said, "; "))
}

// missingStrictcodeRules are the required rules the listing lacks, sorted.
func missingStrictcodeRules(listed strictcodeRules) []string {
	have := map[string]bool{}
	for _, r := range listed.Rules {
		have[r.ID] = true
	}
	var missing []string
	for _, id := range RequiredStrictcodeRules {
		if !have[id] {
			missing = append(missing, id)
		}
	}
	sort.Strings(missing)
	return missing
}

func checkStrictcode(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	if _, err := exec.LookPath("strictcode"); err != nil {
		message := fmt.Sprintf("strictcode is not on PATH, and every release runs it: install it with `%s`", StrictcodeInstall)
		return reportErrors(r, []string{message}, "strictcode is not installed", "")
	}
	env, done, err := runStrictcode(c, "registry", "rules", "--json")
	if err != nil {
		return reportErrors(r, []string{err.Error()}, "strictcode's rules could not be listed", "")
	}
	if done.ExitCode() != 0 {
		message := envelopeFailure("strictcode registry rules --json", env, done)
		return reportErrors(r, []string{message}, "strictcode's rules could not be listed", "")
	}
	var listed strictcodeRules
	if err := json.Unmarshal(env.Payload, &listed); err != nil {
		message := fmt.Sprintf("`strictcode registry rules --json` printed a payload that is not the rule list (%v)", err)
		return reportErrors(r, []string{message}, "strictcode's rules could not be read", "")
	}
	if missing := missingStrictcodeRules(listed); len(missing) > 0 {
		problems := make([]string, len(missing))
		for i, id := range missing {
			problems[i] = fmt.Sprintf("the strictcode on PATH does not implement the rule %s, which every release runs: install a strictcode that does with `%s`", id, StrictcodeInstall)
		}
		return reportErrors(r, problems, fmt.Sprintf("strictcode lacks %d required rule(s): %s", len(missing), strings.Join(missing, ", ")), "")
	}
	env, done, err = runStrictcode(c, "analyze", c.Root(), "--json")
	if err != nil {
		return reportErrors(r, []string{err.Error()}, "strictcode could not analyze the repository", "")
	}
	if done.ExitCode() != 0 && done.ExitCode() != 1 {
		message := envelopeFailure("strictcode analyze "+c.Root()+" --json", env, done)
		return reportErrors(r, []string{message}, "strictcode could not analyze the repository", "")
	}
	var found strictcodeFindings
	if err := json.Unmarshal(env.Payload, &found); err != nil {
		message := fmt.Sprintf("`strictcode analyze --json` printed a payload that is not the findings document (%v)", err)
		return reportErrors(r, []string{message}, "strictcode's findings could not be read", "")
	}
	errorsFound, warnings := 0, 0
	for _, f := range found.Findings {
		text := fmt.Sprintf("%s: %s:%d: %s", f.Rule, f.Target.File, f.Target.Line, f.Message)
		if f.Severity == "error" {
			errorsFound++
			r.Error(text)
		} else {
			warnings++
			r.Warn(text)
		}
	}
	if done.ExitCode() == 1 && errorsFound == 0 {
		r.Error("`strictcode analyze` exited 1, which it does only for an error finding, yet its document reports none")
		errorsFound++
	}
	if errorsFound+warnings == 0 {
		return r.Passed(fmt.Sprintf("strictcode implements every required rule and reports no finding (%d rules listed)", len(listed.Rules)))
	}
	return r.Found(fmt.Sprintf("strictcode reports %d error(s) and %d warning(s)", errorsFound, warnings))
}
