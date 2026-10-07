package release

import (
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A release runs the hooks its declarations name, and nothing else: a hook is
// a command line in releasables.toml, declared on the releasable or on one of
// its members, never a script found on disk.

// HookPoint is one of the points of a release that run hooks.
type HookPoint string

// The hook points, in the order a release reaches them.
const (
	// PreChecks run first, before the pre-release pipeline.
	PreChecks HookPoint = "pre-checks"
	// PreRelease run after the preflight checks; declaring one replaces the
	// built-in tests (see preflightSelection).
	PreRelease HookPoint = "pre-release"
	// PostRelease run after the release is published; their failure does
	// not stop the release.
	PostRelease HookPoint = "post-release"
)

// HookPoints are the hook points in release order.
var HookPoints = []HookPoint{PreChecks, PreRelease, PostRelease}

// Fatal reports whether a failing hook at the point stops the release.
func (p HookPoint) Fatal() bool { return p != PostRelease }

// of are the hooks declared for the point.
func (p HookPoint) of(h declarations.Hooks) []declarations.Hook {
	switch p {
	case PreChecks:
		return h.PreChecks
	case PreRelease:
		return h.PreRelease
	case PostRelease:
		return h.PostRelease
	}
	return nil
}

// HookContext is what a release tells its hooks, through RLSBL_VERSION,
// RLSBL_BUMP_TYPE, RLSBL_PREV_VERSION, and RLSBL_DESCRIPTION; a member's
// hook also gets RLSBL_PACKAGE, the member's name.
type HookContext struct {
	Version         string
	Bump            string
	PreviousVersion string
	Description     string
}

func (c HookContext) environment() map[string]string {
	return map[string]string{
		"RLSBL_VERSION":      c.Version,
		"RLSBL_BUMP_TYPE":    c.Bump,
		"RLSBL_PREV_VERSION": c.PreviousVersion,
		"RLSBL_DESCRIPTION":  c.Description,
	}
}

// HookRun is one hook command a release runs.
type HookRun struct {
	Point HookPoint
	// Declarer names who declared it: `the releasable "portal"` or `the
	// member "widget"`.
	Declarer string
	Command  string
	// Dir is where it runs, absolute.
	Dir string
	// Env is what the release adds to its environment for it, the hook's
	// own env last.
	Env map[string]string
}

// HookRuns are the hooks the releasable's release runs at the point, in
// order, from the repository at w.Root. A releasable's hooks run from the
// representative member's directory (the member the release was started
// from); a member's hooks run from its own directory. The order is the
// releasable's hooks and then its members' (by name) at the pre-checks and
// post-release points, and the members' and then the releasable's at the
// pre-release point, so the releasable's own pre-release hook runs last.
//
// A hook's dir must name a directory its declarer owns: a member's, one the
// member owns; a releasable's, one any of its members owns. A directory
// another member owns belongs to that member's hooks, and is refused.
func HookRuns(w *workspace.Workspace, releasable, representative string, point HookPoint, c HookContext) ([]HookRun, error) {
	r, ok := w.Declarations.Releasable(releasable)
	if !ok {
		return nil, fmt.Errorf("no releasable %q is declared in %s", releasable, declarations.ReleasablesFile)
	}
	rep, ok := w.Declarations.Member(representative)
	if !ok || rep.Releasable != releasable {
		return nil, fmt.Errorf("the member %q is not versioned under the releasable %q", representative, releasable)
	}
	members := w.MembersOf(releasable)
	sort.Slice(members, func(i, j int) bool { return members[i].Name < members[j].Name })
	owners := map[string]bool{}
	for _, m := range members {
		owners[m.Name] = true
	}
	base := c.environment()
	releasableRuns := func() ([]HookRun, error) {
		var out []HookRun
		for _, h := range point.of(r.Hooks) {
			run, err := hookRun(w, point, fmt.Sprintf("the releasable %q", r.Name), rep, owners, h, base)
			if err != nil {
				return nil, err
			}
			out = append(out, run)
		}
		return out, nil
	}
	memberRuns := func() ([]HookRun, error) {
		var out []HookRun
		for _, m := range members {
			env := map[string]string{"RLSBL_PACKAGE": m.Name}
			for k, v := range base {
				env[k] = v
			}
			for _, h := range point.of(m.Hooks) {
				run, err := hookRun(w, point, fmt.Sprintf("the member %q", m.Name), m, map[string]bool{m.Name: true}, h, env)
				if err != nil {
					return nil, err
				}
				out = append(out, run)
			}
		}
		return out, nil
	}
	first, second := releasableRuns, memberRuns
	if point == PreRelease {
		first, second = memberRuns, releasableRuns
	}
	a, err := first()
	if err != nil {
		return nil, err
	}
	b, err := second()
	if err != nil {
		return nil, err
	}
	return append(a, b...), nil
}

// hookRun places one hook: it runs from base's directory, or from its dir
// below it, which a member named in allowed must own.
func hookRun(w *workspace.Workspace, point HookPoint, declarer string, base declarations.Member, allowed map[string]bool, h declarations.Hook, env map[string]string) (HookRun, error) {
	rel := base.Path
	if h.Dir != "" {
		rel = declarations.Join(base.Path, h.Dir)
		owner, ok := w.Declarations.MemberForPath(rel)
		if ok && !allowed[owner.Name] {
			return HookRun{}, fmt.Errorf("the %s hook `%s` of %s runs in %s, which the member %q owns (%s): a command run there belongs to that member's hooks, not to %s; declare it in the hooks of the member %q in %s", point, h.Command, declarer, rel, owner.Name, owner.Path, declarer, owner.Name, declarations.ReleasablesFile)
		}
	}
	merged := map[string]string{}
	for k, v := range env {
		merged[k] = v
	}
	for k, v := range h.Env {
		merged[k] = v
	}
	return HookRun{Point: point, Declarer: declarer, Command: h.Command, Dir: filepath.Join(w.Root, filepath.FromSlash(rel)), Env: merged}, nil
}

// HookRunner starts programs: the strictcli effects handle.
type HookRunner interface {
	Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error)
}

// RunHooks runs the hooks in order through bash, each with the release's
// environment and then its own, bounded by timeout (zero: no bound, the
// declarations' hook_seconds unset). At a fatal point the first failure
// stops the run and is the error. At the post-release point every hook runs,
// and the error names each failure, for the release to record against its
// post-release step, which does not stop the release.
func RunHooks(r HookRunner, runs []HookRun, environment map[string]string, timeout time.Duration) error {
	var failures []string
	for _, run := range runs {
		env := map[string]string{}
		for k, v := range environment {
			env[k] = v
		}
		for k, v := range run.Env {
			env[k] = v
		}
		opts := []strictcli.EffectOption{strictcli.Cwd(run.Dir), strictcli.EffectEnv(env), strictcli.Stream(true), strictcli.Resource("hook:" + string(run.Point))}
		if timeout > 0 {
			opts = append(opts, strictcli.Timeout(timeout))
		}
		if _, err := r.Run([]interface{}{"bash", "-c", run.Command}, opts...); err != nil {
			failure := fmt.Sprintf("the %s hook `%s` of %s failed: %v", run.Point, run.Command, run.Declarer, err)
			if run.Point.Fatal() {
				return errors.New(failure)
			}
			failures = append(failures, failure)
		}
	}
	if len(failures) > 0 {
		return errors.New(strings.Join(failures, "\n"))
	}
	return nil
}

// DeclaresPreReleaseHook reports whether a pre-release hook replaces the
// built-in tests of the member's preflight: the releasable declares one
// (its hook acts for every member), or the member declares its own. The
// declarer is named for the release's log.
func DeclaresPreReleaseHook(r declarations.Releasable, m declarations.Member) (declarer string, declared bool) {
	if len(r.Hooks.PreRelease) > 0 {
		return fmt.Sprintf("the releasable %q", r.Name), true
	}
	if len(m.Hooks.PreRelease) > 0 {
		return fmt.Sprintf("the member %q", m.Name), true
	}
	return "", false
}
