package targets

import (
	"encoding/json"
	"fmt"

	"github.com/stricttools/rlsbl/internal/gomodule"
)

// InstallCommand is one `rlsbl dev install` command for a target.
type InstallCommand struct {
	// Tool is the program that must be on PATH.
	Tool string
	// Args are passed to Tool to install.
	Args []string
	// UninstallArgs are passed to Tool to uninstall, each with {name}
	// replaced by the package name; nil when the mode has no uninstall.
	UninstallArgs []string
	// Purpose says what the tool is for, for the refusal when it is
	// missing.
	Purpose string
}

// DevInstall are a target's install commands per mode: Global installs onto
// the machine, Venv into the project's own environment. A nil mode is one
// the target has nothing to run for; Reason, when set, says why neither has.
type DevInstall struct {
	Global *InstallCommand
	Venv   *InstallCommand
	Reason string
}

// DevInstallOf is t's install commands for the project in dir. A Go
// project installs the main packages its go pipeline declares in
// installPaths (nil when it declares none); a Go library, which has no main
// package, has nothing to install, and a Go project with main packages but
// no declaration is refused, naming them.
func DevInstallOf(r Runner, t Target, dir string, installPaths []string) (DevInstall, error) {
	switch t.Name() {
	case NPM:
		return DevInstall{
			Global: &InstallCommand{Tool: "npm", Args: []string{"link"}, UninstallArgs: []string{"unlink"}, Purpose: "for npm link"},
			Venv:   &InstallCommand{Tool: "npm", Args: []string{"install"}, Purpose: "for npm install"},
		}, nil
	case PyPI:
		return DevInstall{
			Global: &InstallCommand{Tool: "uv", Args: []string{"tool", "install", "-e", "."}, UninstallArgs: []string{"tool", "uninstall", "{name}"}, Purpose: "for an editable Python install"},
			Venv:   &InstallCommand{Tool: "uv", Args: []string{"sync", "--all-packages"}, Purpose: "for syncing the project's environment"},
		}, nil
	case Go:
		if installPaths == nil {
			mains, err := gomodule.MainPackages(r, dir)
			if err != nil {
				return DevInstall{}, err
			}
			if len(mains) == 0 {
				return DevInstall{Reason: "Go library: nothing to install (no main packages)"}, nil
			}
			dirs := make([]string, len(mains))
			for i, p := range mains {
				dirs[i] = p.Dir
			}
			example, err := json.Marshal(dirs)
			if err != nil {
				return DevInstall{}, err
			}
			return DevInstall{}, fmt.Errorf("the go pipeline in .strictmetadata/releasables/releasables.toml declares no install_paths, which `go install` needs. %s Declare e.g. install_paths = %s on the go pipeline", gomodule.DescribeMainPackages(mains), example)
		}
		paths, err := gomodule.ValidateInstallPaths(r, dir, installPaths)
		if err != nil {
			return DevInstall{}, err
		}
		// go install has no reverse, so the global mode has no uninstall;
		// Go has no per-project environment, so there is no venv mode.
		return DevInstall{Global: &InstallCommand{Tool: "go", Args: append([]string{"install"}, paths...), Purpose: "for go install"}}, nil
	}
	return DevInstall{}, fmt.Errorf("the %s target has no install commands", t.Name())
}
