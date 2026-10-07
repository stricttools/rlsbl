// Package devtools is rlsbl's developer commands: `rlsbl dev install`, which
// runs each target's own install command for local development, and `rlsbl
// dev sync` and `rlsbl dev status`, which overlay editable checkouts of
// sibling projects onto a member's locked Python environment and report
// whether the overlays are still in place.
package devtools

import (
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// installTimeout bounds one install command: a backstop against a hung
// process, not a budget.
const installTimeout = 30 * time.Minute

// InstallMode is where `rlsbl dev install` installs.
type InstallMode string

// The install modes.
const (
	// Global installs onto the machine (uv tool install -e, npm link, go
	// install).
	Global InstallMode = "global"
	// Venv installs into the project's own environment (uv sync, npm
	// install).
	Venv InstallMode = "venv"
)

// InstallRequest is one `rlsbl dev install` invocation.
type InstallRequest struct {
	Mode      InstallMode
	Uninstall bool
	// All, Include, and Exclude choose a workspace's members; a standalone
	// repository takes none of them.
	All     bool
	Include []string
	Exclude []string
}

// installStep is one install command to run, or a target skipped with the
// reason.
type installStep struct {
	member string
	target string
	dir    string
	argv   []string
	skip   string
}

// selectMembers are the members the request installs, in declaration
// order. In a workspace one of --all, --include, and --exclude is required;
// a name either names that is not a member is refused, naming the members;
// and a dev-only member is installed only when --include names it, since it
// ships nothing. A standalone repository's one project takes none of them.
func selectMembers(w *workspace.Workspace, req InstallRequest) ([]declarations.Member, error) {
	if !w.IsWorkspace() {
		if req.All || len(req.Include) > 0 || len(req.Exclude) > 0 {
			return nil, errors.New("--all, --include, and --exclude choose among a workspace's members, and this repository is standalone, with one project: run the command without them")
		}
		return []declarations.Member{w.RootMember()}, nil
	}
	if !req.All && len(req.Include) == 0 && len(req.Exclude) == 0 {
		return nil, errors.New("this repository is a workspace: say which members to install with --all (every member that is not dev-only), --include <names>, or --exclude <names>")
	}
	if req.All && len(req.Include) > 0 {
		return nil, errors.New("--all and --include contradict each other: --all installs every member that is not dev-only, --include only the members it names. Pass one of them")
	}
	var known []string
	for _, m := range w.Members() {
		known = append(known, m.Name)
	}
	for _, list := range [][]string{req.Include, req.Exclude} {
		for _, name := range list {
			if !slices.Contains(known, name) {
				return nil, fmt.Errorf("%q is not a member of this workspace; its members are %s", name, strings.Join(known, ", "))
			}
		}
	}
	var selected []declarations.Member
	for _, m := range w.Members() {
		included := slices.Contains(req.Include, m.Name)
		switch {
		case len(req.Include) > 0 && !included:
		case slices.Contains(req.Exclude, m.Name):
		case m.DevOnly && !included:
		default:
			selected = append(selected, m)
		}
	}
	if len(selected) == 0 {
		return nil, errors.New("no member matched: every member the flags leave is dev-only (it ships nothing to install) or excluded. Name the members to install with --include")
	}
	return selected, nil
}

// goInstallPaths are the main packages the member's go pipelines publishing
// target declare, nil when none does. Two pipelines declaring different
// lists are refused: which one `go install` runs is not rlsbl's to guess.
func goInstallPaths(m declarations.Member, target string) ([]string, error) {
	var found []string
	var from string
	for _, p := range m.Pipelines {
		if p.Type != declarations.TargetGo || p.Target != target || p.InstallPaths == nil {
			continue
		}
		if found != nil && !slices.Equal(found, p.InstallPaths) {
			return nil, fmt.Errorf("the member %q declares install_paths on two go pipelines that disagree (%s: %s; %s: %s): declare them on one", m.Name, from, strings.Join(found, ", "), p.Name, strings.Join(p.InstallPaths, ", "))
		}
		found, from = p.InstallPaths, p.Name
	}
	return found, nil
}

// planMember are the member's install steps.
func planMember(r targets.Runner, w *workspace.Workspace, m declarations.Member, req InstallRequest) ([]installStep, error) {
	found, err := targets.MemberTargets(w.Root, m)
	if err != nil {
		return nil, err
	}
	if len(found) == 0 {
		return nil, fmt.Errorf("the member %q has no target to install: it declares none and none is detected in %s", m.Name, w.MemberDir(m))
	}
	var steps []installStep
	for _, t := range found {
		target, err := targets.Get(t.Name)
		if err != nil {
			return nil, err
		}
		dir := filepath.Join(w.Root, filepath.FromSlash(m.TargetDir(t)))
		step := installStep{member: m.Name, target: t.Name, dir: dir}
		var paths []string
		if t.Name == targets.Go {
			if paths, err = goInstallPaths(m, t.Name); err != nil {
				return nil, err
			}
		}
		modes, err := targets.DevInstallOf(r, target, dir, paths)
		if err != nil {
			return nil, fmt.Errorf("cannot install %s from %s: %w", t.Name, dir, err)
		}
		cmd := modes.Global
		if req.Mode == Venv {
			cmd = modes.Venv
		}
		switch {
		case cmd == nil && req.Mode == Venv && modes.Global != nil:
			step.skip = "--target venv is not supported for this target"
		case cmd == nil && modes.Reason != "":
			step.skip = modes.Reason
		case cmd == nil:
			step.skip = fmt.Sprintf("this target has nothing to install with --target %s", req.Mode)
		case req.Uninstall && cmd.UninstallArgs == nil:
			step.skip = fmt.Sprintf("this target has no uninstall for --target %s", req.Mode)
		}
		if step.skip != "" {
			steps = append(steps, step)
			continue
		}
		if _, err := exec.LookPath(cmd.Tool); err != nil {
			return nil, fmt.Errorf("%s is not on PATH; it is needed %s (the %s target of %s). Install it and run this command again", cmd.Tool, cmd.Purpose, t.Name, m.Name)
		}
		args := cmd.Args
		if req.Uninstall {
			name, ok, err := target.ReadName(dir)
			if err != nil {
				return nil, err
			}
			if !ok {
				return nil, fmt.Errorf("the %s manifest in %s names no package, and uninstalling needs the name it was installed under", t.Name, dir)
			}
			args = nil
			for _, a := range cmd.UninstallArgs {
				args = append(args, strings.ReplaceAll(a, "{name}", name))
			}
		}
		step.argv = append([]string{cmd.Tool}, args...)
		steps = append(steps, step)
	}
	return steps, nil
}

// RunInstall is `rlsbl dev install` in the repository w models: it plans
// every selected member's install commands first, refusing every problem it
// finds before anything runs, then runs each command from its target's
// directory. A command that fails does not stop the others; the run fails
// once all were tried.
func RunInstall(ctx *strictcli.Context, w *workspace.Workspace, req InstallRequest) error {
	if req.Mode != Global && req.Mode != Venv {
		return fmt.Errorf("the install mode %q is neither %s nor %s", req.Mode, Global, Venv)
	}
	members, err := selectMembers(w, req)
	if err != nil {
		return err
	}
	e := ctx.Effects()
	plans := make([][]installStep, len(members))
	var problems []string
	for i, m := range members {
		if plans[i], err = planMember(e, w, m, req); err != nil {
			problems = append(problems, err.Error())
		}
	}
	if len(problems) > 0 {
		return fmt.Errorf("nothing was installed:\n  %s", strings.Join(problems, "\n  "))
	}
	action := "Installing"
	if req.Uninstall {
		action = "Uninstalling"
	}
	var failed []string
	for i, m := range members {
		if w.IsWorkspace() {
			ctx.Info(fmt.Sprintf("=== %s ===", m.Name))
		}
		for _, s := range plans[i] {
			if s.skip != "" {
				ctx.Info(fmt.Sprintf("Skipping %s: %s", s.target, s.skip))
				continue
			}
			ctx.Info(fmt.Sprintf("%s %s from %s: %s", action, s.target, s.dir, strings.Join(s.argv, " ")))
			argv := make([]interface{}, len(s.argv))
			for j, a := range s.argv {
				argv[j] = a
			}
			if _, err := e.Run(argv, strictcli.Cwd(s.dir), strictcli.Stream(true), strictcli.Timeout(installTimeout)); err != nil {
				failed = append(failed, fmt.Sprintf("%s (%s, %s): %v", s.member, s.target, s.dir, err))
			}
		}
	}
	if len(failed) > 0 {
		return fmt.Errorf("%s failed for:\n  %s", strings.ToLower(action), strings.Join(failed, "\n  "))
	}
	return nil
}
