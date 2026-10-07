package checks

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The environment an external check runs with, beside the inherited one.
const (
	// EnvProjectRoot is the directory of the member that declares the check.
	EnvProjectRoot = "RLSBL_PROJECT_ROOT"
	// EnvLastTag is the tag of the nearest release of the member's
	// releasable in this checkout, and empty when there is none: a check
	// reads the empty string as "no release yet".
	EnvLastTag = "RLSBL_LAST_TAG"
	// EnvUnreleasedRange is <release commit>..HEAD, or HEAD when there is no
	// release, so the range resolves even when the tag moved or was deleted.
	EnvUnreleasedRange = "RLSBL_UNRELEASED_RANGE"
)

// checkName is the naming rule strictcli holds every check name to:
// lowercase kebab-case of at least two characters. It leaves out glob
// metacharacters, so an external check's name can never select a shipped
// check by pattern.
var checkName = regexp.MustCompile(`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`)

// externalOutputLines caps how many lines of each stream a failing
// external check reports.
const externalOutputLines = 20

// externalCheckProvider supplies, at materialization, the external checks
// the member the directory the run answers for lies in declares. A
// directory whose declarations cannot be read supplies none:
// declarations-valid reports why, and every other check refuses to answer
// there. A name that is not a check name, or that a shipped check holds, is
// a hard error naming the declaration to fix.
func externalCheckProvider(shipped map[string]declaration, directory func() (string, error)) func() []strictcli.CheckSpec {
	return func() []strictcli.CheckSpec {
		dir, err := directory()
		if err != nil {
			return nil
		}
		ws, err := workspace.Discover(dir)
		if err != nil {
			return nil
		}
		m, err := ws.MemberAtDirectory(dir)
		if err != nil {
			return nil
		}
		specs := make([]strictcli.CheckSpec, 0, len(m.ExternalChecks))
		for _, ec := range m.ExternalChecks {
			if err := externalNameProblem(ec.Name, m, shipped); err != "" {
				panic(err)
			}
			specs = append(specs, externalSpec(m, ec))
		}
		return specs
	}
}

// externalNameProblem says why an external check's name cannot be
// registered, and is empty when it can.
func externalNameProblem(name string, m declarations.Member, shipped map[string]declaration) string {
	where := fmt.Sprintf("the external check %q of the member %q in %s", name, m.Name, declarations.ReleasablesFile)
	if len(name) < 2 || !checkName.MatchString(name) {
		return where + " is not a check name: a check name is lowercase kebab-case (letters, digits, and single hyphens, starting with a letter), at least two characters long; rename it"
	}
	if _, ok := shipped[name]; ok {
		return where + " takes the name of a check rlsbl ships; rename it"
	}
	return ""
}

// externalSpec is the check spec of one external check: an error check that
// runs the declared command through a shell.
func externalSpec(m declarations.Member, ec declarations.ExternalCheck) strictcli.CheckSpec {
	meta := strictcli.CheckSpecMeta{
		Name:      ec.Name,
		Tags:      []string{ec.Tag},
		Severity:  "error",
		DependsOn: append([]string{}, ec.DependsOn...),
	}
	return strictcli.NewErrorCheckSpec(meta, func(cc strictcli.CheckContext, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
		ctx, ok := cc.(*Context)
		if !ok {
			panic(unanswered(fmt.Sprintf("rlsbl's checks run with rlsbl's check context, and this run was given a %T", cc)))
		}
		return runExternalCheck(ctx, r, m, ec)
	})
}

// releaseContextEnv is the release context an external check of the member
// m runs with: the member's directory and its releasable's nearest release.
func releaseContextEnv(c *Context, m declarations.Member) map[string]string {
	env := map[string]string{
		EnvProjectRoot:     c.Workspace().MemberDir(m),
		EnvLastTag:         "",
		EnvUnreleasedRange: "HEAD",
	}
	rel, ok := c.Workspace().ReleasableOf(m)
	if !ok {
		return env
	}
	scheme, err := workspace.SchemeOf(rel)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	head, err := c.Repo().Head()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	nearest, err := releaserecord.New(c.Repo(), rel.Name, scheme, c.Upstream()).Nearest(head)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if nearest != nil {
		env[EnvLastTag] = nearest.Tag(scheme)
		env[EnvUnreleasedRange] = nearest.ReleaseCommit + "..HEAD"
	}
	return env
}

// programProblem says why the program a command starts with cannot be run,
// and is empty when it can.
func programProblem(command string) string {
	fields := strings.Fields(command)
	if len(fields) == 0 {
		return "the command is empty"
	}
	program := fields[0]
	if filepath.IsAbs(program) {
		if info, err := os.Stat(program); err != nil || info.IsDir() {
			return fmt.Sprintf("the command's program %s does not exist", program)
		}
		return ""
	}
	if _, err := exec.LookPath(program); err != nil {
		return fmt.Sprintf("the command's program %s is not on PATH", program)
	}
	return ""
}

func runExternalCheck(c *Context, r *strictcli.ErrorReporter, m declarations.Member, ec declarations.ExternalCheck) strictcli.CheckOutcome {
	if problem := programProblem(ec.Command); problem != "" {
		message := fmt.Sprintf("the external check %q of the member %q cannot run: %s; install it, or correct the command in %s", ec.Name, m.Name, problem, declarations.ReleasablesFile)
		return reportErrors(r, []string{message}, message, "")
	}
	dir := c.Workspace().MemberDir(m)
	if ec.Cwd != "" {
		dir = filepath.Join(dir, filepath.FromSlash(ec.Cwd))
	}
	budget := c.CheckTimeout()
	// An external check is a check: it reports and changes nothing, which
	// is what lets the read-only check command start it. Under --dry-run it
	// is not started, because it is declared impure.
	done, err := c.Effects().Run([]interface{}{"sh", "-c", ec.Command},
		strictcli.Cwd(dir), strictcli.Check(false), strictcli.Timeout(budget),
		strictcli.EffectEnv(releaseContextEnv(c, m)), strictcli.Observe())
	if err != nil {
		message := fmt.Sprintf("the external check %q did not finish: %v (an external check runs within %s, the check_seconds under [timeouts] in %s)", ec.Name, err, budget, declarations.ReleasablesFile)
		return reportErrors(r, []string{message}, firstLine(message), "")
	}
	if done.ExitCode() == 0 {
		return r.Passed(orPassed(firstLine(done.Stdout())))
	}
	out := reportableLines(done.Stdout(), externalOutputLines)
	errs := reportableLines(done.Stderr(), externalOutputLines)
	problems := append(append([]string(nil), out...), errs...)
	if len(problems) == 0 {
		problems = []string{fmt.Sprintf("exit status %d", done.ExitCode())}
	}
	first := problems[0]
	if len(errs) > 0 {
		first = errs[0]
	}
	return reportErrors(r, problems, fmt.Sprintf("the external check %q failed (exit %d): %s", ec.Name, done.ExitCode(), firstLine(first)), "")
}

// reportableLines are the non-blank lines of text, right-trimmed, at most
// limit of them: a reporter refuses an empty problem.
func reportableLines(text string, limit int) []string {
	var out []string
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimRight(line, " \t\r")
		if strings.TrimSpace(line) == "" {
			continue
		}
		out = append(out, line)
		if len(out) == limit {
			break
		}
	}
	return out
}
