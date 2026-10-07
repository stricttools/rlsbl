package checks

import (
	"fmt"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The scope tokens a check's scope in checks.toml is written in, colon
// separated and applied left to right.
const (
	// tokenRepository: the check answers for the whole repository, so
	// standing at a workspace root is a position it answers from.
	tokenRepository = "repository"
	// tokenWorkspace: the check answers for a workspace; it skips in a
	// standalone repository, and answers from a workspace root.
	tokenWorkspace = "workspace"
	// tokenNonDevOnly narrows the members the check sees to those that are
	// not dev-only.
	tokenNonDevOnly = "non_dev_only"
	// tokenNonDevNode narrows them to those that are not dev nodes.
	tokenNonDevNode = "non_dev_node"
	// tokenLibrary narrows them to libraries.
	tokenLibrary = "library"
	// tokenReleasable narrows them to members versioned under a releasable.
	tokenReleasable = "releasable"
	// tokenPush: the check runs only for a push the pre-push hook reports.
	tokenPush = "push"
)

var scopeTokens = map[string]bool{
	tokenRepository: true, tokenWorkspace: true, tokenNonDevOnly: true,
	tokenNonDevNode: true, tokenLibrary: true, tokenReleasable: true, tokenPush: true,
}

// declaration is one check of checks.toml. strictcli validates the
// fields; package checks reads the severity and the scope.
type declaration struct {
	Description  string   `toml:"description,required"`
	Subject      string   `toml:"subject,required"`
	Tags         []string `toml:"tags,required"`
	Severity     string   `toml:"severity,required"`
	Fast         bool     `toml:"fast"`
	Pure         bool     `toml:"pure"`
	NeedsNetwork bool     `toml:"needs_network"`
	DependsOn    []string `toml:"depends_on"`
	Scope        string   `toml:"scope"`
}

// registryDocument is checks.toml.
type registryDocument struct {
	App    string                 `toml:"app,required"`
	Checks map[string]declaration `toml:"checks"`
	Hooks  map[string]any         `toml:"hooks"`
}

// declared reads every check checks.toml declares, refusing a scope written
// with a token package checks does not interpret.
func declared() (map[string]declaration, error) {
	doc, err := tomledit.Unmarshal[registryDocument](Registry)
	if err != nil {
		return nil, fmt.Errorf("internal/checks/checks.toml: %w", err)
	}
	for name, d := range doc.Checks {
		if d.Scope == "" {
			continue
		}
		for _, token := range strings.Split(d.Scope, ":") {
			if !scopeTokens[token] {
				return nil, fmt.Errorf("internal/checks/checks.toml: the check %s has the scope %q, whose token %q package checks does not interpret", name, d.Scope, token)
			}
		}
	}
	return doc.Checks, nil
}

// Unanswered is the refusal of a check that cannot answer where it was run:
// the declarations cannot be read, or the run stands at a workspace root
// and the check answers for one releasable. It is raised as a panic, which
// strictcli reports as the check's own error-severity failure whatever its
// severity, because a warning check's reporter cannot mint an error and an
// unanswered check must never pass.
type Unanswered struct {
	Reason string
}

func (u Unanswered) Error() string { return u.Reason }

func unanswered(reason string) Unanswered { return Unanswered{Reason: reason} }

// check is one check's implementation: one of the two forms, the
// form matching the severity checks.toml declares.
type check struct {
	name     string
	errorRun func(*Context, *strictcli.ErrorReporter) strictcli.CheckOutcome
	warnRun  func(*Context, *strictcli.WarnReporter) strictcli.CheckOutcome
	// reportsLoadErrors marks a check that reports unreadable declarations
	// or options itself instead of refusing to answer.
	reportsLoadErrors bool
}

func errorCheck(name string, run func(*Context, *strictcli.ErrorReporter) strictcli.CheckOutcome) check {
	return check{name: name, errorRun: run}
}

func warnCheck(name string, run func(*Context, *strictcli.WarnReporter) strictcli.CheckOutcome) check {
	return check{name: name, warnRun: run}
}

// families are every family of checks package checks implements, each in a
// file of its own.
var families = []func() []check{
	projectChecks,
	changelogChecks,
	prepushChecks,
	releaseChecks,
	lifecycleChecks,
	strictcodeChecks,
	workspaceChecks,
	routerChecks,
	nestedChecks,
	uploadChecks,
	goTagChecks,
	goWorkspaceChecks,
	goBuildChecks,
	dependencyChecks,
	testRunnerChecks,
	scaffoldedChecks,
	matrixChecks,
}

// Implemented are the names of every check package checks implements.
func Implemented() []string {
	var names []string
	for _, family := range families {
		for _, c := range family() {
			names = append(names, c.name)
		}
	}
	sort.Strings(names)
	return names
}

// Register registers every check package checks implements on app, whose
// checks registry is Registry, and the provider of the external checks
// the members declare. Each implementation sees the run's *Context
// narrowed by the check's scope, and a check answering for one releasable
// refuses at a workspace root.
func Register(app *strictcli.App) error {
	decls, err := declared()
	if err != nil {
		return err
	}
	for _, family := range families {
		for _, c := range family() {
			d, ok := decls[c.name]
			if !ok {
				return fmt.Errorf("checks: %s is implemented but internal/checks/checks.toml does not declare it", c.name)
			}
			if err := register(app, c, d); err != nil {
				return err
			}
		}
	}
	app.RegisterCheckProvider(externalCheckProvider(decls))
	return nil
}

func register(app *strictcli.App, c check, d declaration) error {
	switch {
	case c.errorRun != nil && d.Severity == "error":
		app.RegisterErrorCheck(c.name, func(cc strictcli.CheckContext, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
			ctx, skip := prepare(cc, c, d)
			if skip != "" {
				return r.Skipped(skip)
			}
			return c.errorRun(ctx, r)
		})
	case c.warnRun != nil && d.Severity == "warn":
		app.RegisterWarnCheck(c.name, func(cc strictcli.CheckContext, r *strictcli.WarnReporter) strictcli.CheckOutcome {
			ctx, skip := prepare(cc, c, d)
			if skip != "" {
				return r.Skipped(skip)
			}
			return c.warnRun(ctx, r)
		})
	default:
		return fmt.Errorf("checks: %s is declared with severity %q, and its implementation is not of that form", c.name, d.Severity)
	}
	return nil
}

// prepare is the context one check sees: the run's context narrowed by the
// check's scope, and a skip reason when the scope does not apply. It
// refuses (panics with Unanswered) where the check cannot answer.
func prepare(cc strictcli.CheckContext, c check, d declaration) (*Context, string) {
	ctx, ok := cc.(*Context)
	if !ok {
		panic(unanswered(fmt.Sprintf("rlsbl's checks run with rlsbl's check context, and this run was given a %T", cc)))
	}
	if !c.reportsLoadErrors {
		if ctx.declErr != nil {
			panic(unanswered(fmt.Sprintf("the release declarations cannot be read, so this check cannot be answered (declarations-valid names the problem): %v", ctx.declErr)))
		}
		if ctx.optsErr != nil {
			panic(unanswered(fmt.Sprintf("the options cannot be read, so this check cannot be answered (declarations-valid names the problem): %v", ctx.optsErr)))
		}
	}
	scoped := *ctx
	if d.Scope != "" {
		for _, token := range strings.Split(d.Scope, ":") {
			if skip := scoped.applyToken(token); skip != "" {
				return nil, skip
			}
		}
	}
	if scoped.unselected != nil && scoped.declErr == nil {
		panic(unanswered(unselectedRefusal(scoped.unselected)))
	}
	return &scoped, ""
}

// applyToken narrows the context by one scope token, or names why the check
// does not apply.
func (c *Context) applyToken(token string) string {
	keep := func(pred func(declarations.Member) bool) {
		var out []declarations.Member
		for _, m := range c.members {
			if pred(m) {
				out = append(out, m)
			}
		}
		c.members = out
	}
	switch token {
	case tokenRepository:
		c.unselected = nil
	case tokenWorkspace:
		if c.declErr == nil && !c.ws.IsWorkspace() {
			return "not a workspace: .strictmetadata/releasables/releasables.toml declares repository_layout = \"standalone\""
		}
		c.unselected = nil
	case tokenNonDevOnly:
		keep(func(m declarations.Member) bool { return !m.DevOnly })
	case tokenNonDevNode:
		keep(func(m declarations.Member) bool { return !m.DevNode() })
	case tokenLibrary:
		keep(func(m declarations.Member) bool { return m.Library })
	case tokenReleasable:
		keep(func(m declarations.Member) bool { return m.Versioned() })
	case tokenPush:
		if c.in.PushLines == nil {
			return "not in a push: only the pre-push hook reports the refs a push sends"
		}
	}
	return ""
}

// unselectedRefusal is the refusal of a check answering for one releasable
// run at a workspace root.
func unselectedRefusal(names []string) string {
	if len(names) == 0 {
		return "a workspace root names the workspace, not a releasable in it, and this workspace declares no releasable, so there is no releasable here for this check to answer for"
	}
	return fmt.Sprintf("a workspace root names the workspace, not a releasable in it, so this check has no releasable to answer for: run the checks from a directory of a member of the releasable they are for (this workspace declares %s)", strings.Join(names, ", "))
}
