package cli

import (
	"fmt"
	"os"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/devtools"
	"github.com/stricttools/rlsbl/internal/workspace"
)

const devGroupHelp = "Developer commands for working on rlsbl projects locally: install a project the way its release targets install it " +
	"(pypi with `uv tool install -e`, npm with `npm link`, go with `go install`), and overlay editable checkouts of sibling projects onto a " +
	"member's locked Python environment."

const devInstallHelp = "Install the project for local development by running each of its targets' own install command from the target's " +
	"directory. --target names the install mode: global installs onto the machine (pypi with `uv tool install -e .`, npm with `npm link`, go " +
	"with `go install` of the main packages the go pipeline's install_paths declares), venv into the project's own environment (pypi with " +
	"`uv sync --all-packages`, npm with `npm install`); a target with nothing to run in the chosen mode is skipped with the reason. " +
	"--uninstall reverses a global install where the target has a reverse (npm with `npm unlink`, pypi with `uv tool uninstall <package " +
	"name>`). In a workspace, --all installs every member that is not dev-only, --include only the members it names (a dev-only member " +
	"included), and --exclude every member that is not dev-only but the ones it names; a standalone repository takes none of them. Every " +
	"command is planned before any runs, and a missing program, a Go module with main packages but no install_paths, or a member name the " +
	"workspace does not declare refuses the run before anything is installed. A command that fails does not stop the others, and the run " +
	"exits 1 once all were tried."

const devSyncHelp = "Overlay editable checkouts of sibling projects onto the locked environment of the member whose directory holds the " +
	"working directory. Reads the member's dev-sources.toml.local-only, one [[overlay]] table (package, path) per checkout, and refuses an " +
	"unknown key, a checkout that does not exist or whose [project].name differs from the package, a package a workspace member builds, " +
	"and a checkout inside this repository. Then runs one `uv sync --inexact` leaving out every overlaid package and one `uv pip install " +
	"-e <checkout>` per overlay, with VIRTUAL_ENV set to the member's environment, and records the overlays in " +
	"dev-overlays-state.toml.local-only, which `rlsbl dev status` and the dev-overlay-drift check read. Requires UV_NO_SYNC=1 in the " +
	"environment, so a bare `uv run` does not sync the overlays away. A workspace's root member is refused: run it from a member's directory."

const devStatusHelp = "Report the dev overlays of the member whose directory holds the working directory: each package " +
	"dev-overlays-state.toml.local-only records, with its checkout and version, against what the member's environment holds (editable at " +
	"the recorded checkout, WIPED back to a registry wheel, or MISSING). Exits 1 when any overlay drifted, so scripts and pre-run guards see " +
	"a wipe by a bare `uv sync` or `uv run`; exits 0 when every overlay is intact or none is recorded. A recorded overlay `rlsbl dev sync` " +
	"would refuse (a workspace member's package, a checkout inside this repository) is refused here too."

func registerDev(r *commandSet) {
	r.group([]string{"dev"}, devGroupHelp)
	r.add(command{
		path:   []string{"dev", "install"},
		help:   devInstallHelp,
		effect: mutating,
		flags: []strictcli.Flag{
			strictcli.StringFlag("target", "The install mode; there is none by default", strictcli.Required(),
				strictcli.Choices(
					strictcli.Ch(string(devtools.Global), "install onto the machine (uv tool install -e, npm link, go install)"),
					strictcli.Ch(string(devtools.Venv), "install into the project's own environment (uv sync, npm install)"),
				)),
			strictcli.BoolFlag("all", "In a workspace, install every member that is not dev-only", strictcli.Optional()),
			strictcli.StringFlag("include", "In a workspace, the comma-separated names of the only members to install", strictcli.Optional()),
			strictcli.StringFlag("exclude", "In a workspace, the comma-separated names of members to leave out", strictcli.Optional()),
			strictcli.BoolFlag("uninstall", "Reverse a previous install where the target has a reverse (installs when not passed)", strictcli.Optional()),
		},
		run: runDevInstall,
	})
	r.add(command{
		path:   []string{"dev", "sync"},
		help:   devSyncHelp,
		effect: mutating,
		run:    runDevSync,
	})
	r.add(command{
		path:   []string{"dev", "status"},
		help:   devStatusHelp,
		effect: readOnly,
		run:    runDevStatus,
	})
}

// memberNames reads a comma-separated list of member names; an empty name
// in it is refused.
func memberNames(kw map[string]any, flag string) ([]string, error) {
	value, given := strictcli.GetOpt[string](kw, flag)
	if !given {
		return nil, nil
	}
	var names []string
	for _, n := range strings.Split(value, ",") {
		n = strings.TrimSpace(n)
		if n == "" {
			return nil, fmt.Errorf("--%s %q holds an empty member name; write the names separated by single commas", flag, value)
		}
		names = append(names, n)
	}
	return names, nil
}

// workingWorkspace is the working directory and the model of the
// repository holding it.
func workingWorkspace() (string, *workspace.Workspace, error) {
	dir, root, err := workingRepository()
	if err != nil {
		return "", nil, err
	}
	w, err := workspace.Load(root)
	return dir, w, err
}

func runDevInstall(ctx *strictcli.Context, kw map[string]any) (any, error) {
	req := devtools.InstallRequest{Mode: devtools.InstallMode(strictcli.Get[string](kw, "target"))}
	req.All, _ = strictcli.GetOpt[bool](kw, "all")
	req.Uninstall, _ = strictcli.GetOpt[bool](kw, "uninstall")
	var err error
	if req.Include, err = memberNames(kw, "include"); err != nil {
		return nil, err
	}
	if req.Exclude, err = memberNames(kw, "exclude"); err != nil {
		return nil, err
	}
	_, w, err := workingWorkspace()
	if err != nil {
		return nil, err
	}
	return nil, devtools.RunInstall(ctx, w, req)
}

// devEnvironment is what the dev commands read from the environment: uv's
// own variables, which decide where uv installs.
func devEnvironment() devtools.Environment {
	return devtools.Environment{UVNoSync: os.Getenv("UV_NO_SYNC"), UVProjectEnvironment: os.Getenv("UV_PROJECT_ENVIRONMENT")}
}

func runDevSync(ctx *strictcli.Context, _ map[string]any) (any, error) {
	dir, w, err := workingWorkspace()
	if err != nil {
		return nil, err
	}
	return nil, devtools.RunSync(ctx, w, dir, devEnvironment())
}

func runDevStatus(ctx *strictcli.Context, _ map[string]any) (any, error) {
	dir, w, err := workingWorkspace()
	if err != nil {
		return nil, err
	}
	return nil, devtools.RunStatus(ctx, w, dir, devEnvironment())
}
