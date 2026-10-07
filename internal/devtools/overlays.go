package devtools

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// syncTimeout bounds each uv command of a sync: a backstop against a hung
// process, not a budget.
const syncTimeout = 30 * time.Minute

// The overlay file, the sentinel, and why a sync refuses without
// UV_NO_SYNC=1.
const (
	overridesFile = dependencies.OverridesFile
	sentinelFile  = dependencies.SentinelFile

	fileFormatHowto = "Create " + overridesFile + " in the member's directory (the *.local-only pattern keeps it out of git) with one [[overlay]] table per local checkout:\n\n" +
		"    [[overlay]]\n" +
		"    package = \"strictcli\"          # the distribution name, as uv knows it\n" +
		"    path = \"../strictcli/python\"   # absolute, or relative to the member's directory"

	uvNoSyncRefusal = "UV_NO_SYNC is not set to 1 in the environment.\n\n" +
		"`rlsbl dev sync` refuses to run without it: any bare `uv run` syncs the environment first, which reinstalls the locked registry wheels over the " +
		"editable overlays this command installs, undoing them without a word. With UV_NO_SYNC=1, `uv run` skips that sync and the overlays stay.\n\n" +
		"Set it permanently, then run the command again:\n" +
		"    shell profile (~/.bashrc or ~/.zshrc):   export UV_NO_SYNC=1\n" +
		"    direnv (in the project's .envrc):        export UV_NO_SYNC=1\n\n" +
		"(A bare `uv sync` still reverts the overlays; run `rlsbl dev sync` again to restore them, and `rlsbl dev status` to check.)"
)

// overlay is one validated [[overlay]] entry.
type overlay struct {
	pkg string
	// path is the checkout, absolute.
	path string
	// version is the checkout's static [project].version, or empty.
	version string
}

// overlayDocument is the overlay file as the strict decode reads it: any
// other key is refused.
type overlayDocument struct {
	Overlay []overlayEntry `toml:"overlay"`
}

type overlayEntry struct {
	Package string `toml:"package,required"`
	Path    string `toml:"path,required"`
}

// insideRepository reports whether the absolute path is the repository root
// or lies under it, symbolic links resolved on both sides.
func insideRepository(root, path string) bool {
	resolve := func(p string) string {
		if r, err := filepath.EvalSymlinks(p); err == nil {
			return r
		}
		return filepath.Clean(p)
	}
	r, p := resolve(root), resolve(path)
	return p == r || strings.HasPrefix(p, r+string(filepath.Separator))
}

// scopeRefusal is the refusal of an overlay naming something inside this
// repository, and empty when it names neither. An overlay puts a sibling
// repository's checkout in front of the registry wheel this project locked,
// so it cannot name a package a workspace member builds (the member already
// is that package's source, and two editable copies leave which one the
// tests import to install order), nor a path this repository ships (the
// environment would depend on a tree that exists on no other machine).
func scopeRefusal(w *workspace.Workspace, pkg, path string) string {
	if w.IsWorkspace() {
		want := dependencies.NormalizePypiName(pkg)
		for _, m := range w.Members() {
			for _, spelling := range []string{m.Name, m.RegistryName} {
				if spelling != "" && dependencies.NormalizePypiName(spelling) == want {
					return fmt.Sprintf("[[overlay]] entry '%s' names a member of this workspace (%s). An overlay puts a sibling project's checkout in front of the registry wheel this project locked, and a member is not resolved from a registry here at all: it already is the source. Two editable copies of one package leave which one the tests import to install order. Depend on the member as a member, and delete this entry.", pkg, m.Path)
				}
			}
		}
	}
	if insideRepository(w.Root, path) {
		return fmt.Sprintf("[[overlay]] entry '%s' resolves to %s, which is inside this repository (%s). An overlay names a checkout of another repository; an editable install of a path this repository ships makes the environment depend on a tree that exists on no other machine, the hazard a committed [tool.uv.sources] path entry carries. Point the entry at the sibling checkout, or move the code into a member.", pkg, path, w.Root)
	}
	return ""
}

// staleRefusal is the refusal of an entry whose checkout is not there: the
// checkout moved, or the project stopped depending on the package. The
// release's version-skew guard reads the same file, so the message names
// the file, what it is for, and both ways out.
func staleRefusal(pkg, declared, abs string) string {
	return fmt.Sprintf("[[overlay]] entry '%s' in %s: the checkout path does not exist: %s (resolved to %s).\n\n"+
		"%s is the `rlsbl dev sync` overlay file: each [[overlay]] table names a sibling checkout to install editable over this member's locked environment. "+
		"An entry pointing at nothing is stale: the checkout moved, or this project no longer depends on '%s' at all. `"+runstate.RunInvocation+"` reads the same file, "+
		"so a stale entry blocks a release until it is resolved.\n\n"+
		"Resolve it either way:\n"+
		"  - point 'path' at the checkout's current location; or\n"+
		"  - delete the [[overlay]] table for '%s'. If it was the only one, delete %s itself: the project then runs on the locked registry wheels, "+
		"and neither `rlsbl dev sync` nor the release reads the file.",
		pkg, overridesFile, declared, abs, overridesFile, pkg, pkg, overridesFile)
}

// checkoutProject is the [project] name and version of the checkout's
// pyproject.toml.
func checkoutProject(pkg, dir string) (name, version string, err error) {
	path := filepath.Join(dir, dependencies.PyprojectFile)
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", "", fmt.Errorf("[[overlay]] entry '%s': %s is not an installable project (it has no %s)", pkg, dir, dependencies.PyprojectFile)
	}
	if err != nil {
		return "", "", err
	}
	doc, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return "", "", fmt.Errorf("[[overlay]] entry '%s': %s does not parse as TOML: %w", pkg, path, err)
	}
	project, _ := (*doc)["project"].(map[string]any)
	name, _ = project["name"].(string)
	version, _ = project["version"].(string)
	if name == "" {
		return "", "", fmt.Errorf("[[overlay]] entry '%s': %s declares no [project].name. The checkout must declare a static [project].name (PEP 621) matching the entry's 'package', or the sync's exclusion of the locked wheel cannot be verified to protect the overlay", pkg, path)
	}
	return name, version, nil
}

// loadOverlays reads and validates the overlay file in dir, the member's
// directory. Every problem is a hard error; nothing is skipped.
func loadOverlays(w *workspace.Workspace, dir string) ([]overlay, error) {
	path := filepath.Join(dir, overridesFile)
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("no %s in %s.\n\n`rlsbl dev sync` overlays local editable checkouts onto this member's environment. %s", overridesFile, dir, fileFormatHowto)
	}
	if err != nil {
		return nil, err
	}
	doc, err := tomledit.Unmarshal[overlayDocument](data)
	if err != nil {
		return nil, fmt.Errorf("%s is not a valid overlay file: %v\n\nOnly [[overlay]] tables with the keys package and path are allowed. %s", path, err, fileFormatHowto)
	}
	if len(doc.Overlay) == 0 {
		return nil, fmt.Errorf("%s declares no overlays.\n\n%s", path, fileFormatHowto)
	}
	seen := map[string]string{}
	var out []overlay
	for i, entry := range doc.Overlay {
		if entry.Package == "" {
			return nil, fmt.Errorf("[[overlay]] entry #%d in %s has an empty 'package' (the distribution name, as uv knows it)", i+1, path)
		}
		if entry.Path == "" {
			return nil, fmt.Errorf("[[overlay]] entry #%d ('%s') in %s has an empty 'path' (absolute, or relative to the member's directory)", i+1, entry.Package, path)
		}
		normalized := dependencies.NormalizePypiName(entry.Package)
		if first, ok := seen[normalized]; ok {
			return nil, fmt.Errorf("%s names the package '%s' twice ('%s' and '%s'); one environment holds one copy of it. Delete one of the entries", path, normalized, first, entry.Package)
		}
		seen[normalized] = entry.Package
		abs := entry.Path
		if !filepath.IsAbs(abs) {
			abs = filepath.Join(dir, abs)
		}
		abs = filepath.Clean(abs)
		if info, err := os.Stat(abs); err != nil || !info.IsDir() {
			return nil, errors.New(staleRefusal(entry.Package, entry.Path, abs))
		}
		name, version, err := checkoutProject(entry.Package, abs)
		if err != nil {
			return nil, err
		}
		if dependencies.NormalizePypiName(name) != normalized {
			return nil, fmt.Errorf("[[overlay]] entry '%s' does not match the checkout's [project].name '%s' in %s. The names must match (PEP 503 normalization applies), or the sync's exclusion of the locked wheel does not protect the overlay", entry.Package, name, filepath.Join(abs, dependencies.PyprojectFile))
		}
		if refusal := scopeRefusal(w, entry.Package, abs); refusal != "" {
			return nil, errors.New(refusal)
		}
		out = append(out, overlay{pkg: entry.Package, path: abs, version: version})
	}
	return out, nil
}

// renderSentinel is the sentinel recording the overlays a sync installed:
// each package with its checkout and the version it had ("" for a dynamic
// one, so every entry has every key).
func renderSentinel(overlays []overlay) string {
	var b strings.Builder
	for i, o := range overlays {
		if i > 0 {
			b.WriteString("\n")
		}
		b.WriteString("[[overlay]]\n")
		b.WriteString("package = " + tomledit.QuoteString(o.pkg) + "\n")
		b.WriteString("path = " + tomledit.QuoteString(o.path) + "\n")
		b.WriteString("version = " + tomledit.QuoteString(o.version) + "\n")
	}
	return b.String()
}

// Environment is what the dev commands read from the process environment,
// read by the caller.
type Environment struct {
	// UVNoSync is UV_NO_SYNC; a sync requires "1".
	UVNoSync string
	// UVProjectEnvironment is UV_PROJECT_ENVIRONMENT, uv's own relocation
	// of a project's environment; empty when unset.
	UVProjectEnvironment string
}

// syncMember is the member a sync in dir overlays. A workspace's root
// member holds every other member, and its environment is not one member's
// to overlay, so it is refused.
func syncMember(w *workspace.Workspace, dir string) (declarations.Member, error) {
	m, err := w.MemberAtDirectory(dir)
	if err != nil {
		return declarations.Member{}, err
	}
	if w.IsWorkspace() && m.IsRoot() {
		var paths []string
		for _, other := range w.Members() {
			if !other.IsRoot() {
				paths = append(paths, other.Path)
			}
		}
		return declarations.Member{}, fmt.Errorf("`rlsbl dev sync` overlays one member's environment, and %s is the workspace's root member, which holds every other member. Change into the directory of the member to overlay (its %s lives there) and run the command again; the members are %s", dir, overridesFile, strings.Join(paths, ", "))
	}
	return m, nil
}

// RunSync is `rlsbl dev sync` for the member whose territory dir lies in:
// one `uv sync --inexact` excluding every overlaid package, then an
// editable `uv pip install -e` of each checkout, then the sentinel that
// records them. VIRTUAL_ENV is set to the member's own environment for
// every uv command, so `uv pip`, which prefers an active VIRTUAL_ENV, and
// `uv sync`, which uses the project's, install into the same environment.
func RunSync(ctx *strictcli.Context, w *workspace.Workspace, dir string, env Environment) error {
	if env.UVNoSync != "1" {
		return errors.New(uvNoSyncRefusal)
	}
	m, err := syncMember(w, dir)
	if err != nil {
		return err
	}
	memberDir := w.MemberDir(m)
	overlays, err := loadOverlays(w, memberDir)
	if err != nil {
		return err
	}
	if _, err := exec.LookPath("uv"); err != nil {
		return errors.New("uv is not on PATH, and `rlsbl dev sync` installs the overlays with it. Install uv and run the command again")
	}
	venv, err := dependencies.ProjectEnvironment(memberDir, env.UVProjectEnvironment)
	if err != nil {
		return err
	}
	e := ctx.Effects()
	opts := []strictcli.EffectOption{strictcli.Cwd(memberDir), strictcli.EffectEnv(map[string]string{"VIRTUAL_ENV": venv}), strictcli.Stream(true), strictcli.Timeout(syncTimeout)}
	argv := []interface{}{"uv", "sync", "--inexact"}
	for _, o := range overlays {
		argv = append(argv, "--no-install-package", o.pkg)
	}
	ctx.Info(fmt.Sprintf("Syncing the environment %s, leaving out %d overlaid package(s)...", venv, len(overlays)))
	if _, err := e.Run(argv, opts...); err != nil {
		return fmt.Errorf("uv sync failed, so no overlay was installed: %w", err)
	}
	// Installed on every run, so new dependencies of a checkout come along.
	for _, o := range overlays {
		version := o.version
		if version == "" {
			version = "(dynamic version)"
		}
		ctx.Info(fmt.Sprintf("Overlaying %s %s from %s", o.pkg, version, o.path))
		if _, err := e.Run([]interface{}{"uv", "pip", "install", "-e", o.path}, opts...); err != nil {
			return fmt.Errorf("the editable install of '%s' from %s failed: %w", o.pkg, o.path, err)
		}
	}
	sentinel := filepath.Join(memberDir, sentinelFile)
	temporary := sentinel + ".rlsbl-writing"
	if _, err := e.Write(temporary, renderSentinel(overlays)); err != nil {
		return fmt.Errorf("writing %s: %w", sentinel, err)
	}
	if _, err := e.Rename(temporary, sentinel); err != nil {
		return fmt.Errorf("writing %s: %w", sentinel, err)
	}
	ctx.Info(fmt.Sprintf("Overlaid %d package(s). A bare `uv sync` reverts the overlays; run `rlsbl dev sync` again to restore them, and `rlsbl dev status` to check.", len(overlays)))
	return nil
}

// RunStatus is `rlsbl dev status` for the member whose territory dir lies
// in: each overlay the sentinel records against what the member's
// environment holds. An overlay wiped back to a registry wheel or missing
// fails the command, so scripts and pre-run guards see the drift; no
// sentinel, or one recording no overlay, is the state of a member with no
// overlays and passes.
func RunStatus(ctx *strictcli.Context, w *workspace.Workspace, dir string, env Environment) error {
	m, err := w.MemberAtDirectory(dir)
	if err != nil {
		return err
	}
	memberDir := w.MemberDir(m)
	recorded, found, err := dependencies.LoadSentinel(memberDir)
	if err != nil {
		return err
	}
	if !found {
		ctx.Out(fmt.Sprintf("No dev overlays are recorded (no %s in %s). `rlsbl dev sync` writes it after overlaying local checkouts.", sentinelFile, memberDir))
		return nil
	}
	if len(recorded) == 0 {
		ctx.Out(fmt.Sprintf("%s records no overlays.", filepath.Join(memberDir, sentinelFile)))
		return nil
	}
	// The sentinel outlives the overlay file it was written from, so the
	// refusals a sync makes are made again over what it records: reporting
	// such a state as ordinary drift would send the reader to the command
	// that refuses it.
	for _, o := range recorded {
		if refusal := scopeRefusal(w, o.Package, o.Path); refusal != "" {
			return errors.New(refusal)
		}
	}
	lines := []string{fmt.Sprintf("Dev overlays recorded in %s:", filepath.Join(memberDir, sentinelFile))}
	drifted := 0
	for _, o := range recorded {
		installed, err := dependencies.InspectInstalled(memberDir, env.UVProjectEnvironment, o.Package)
		if err != nil {
			return err
		}
		state, detail := dependencies.ClassifyOverlay(o, installed)
		marker := "ok"
		switch state {
		case dependencies.OverlayWiped:
			marker = "WIPED"
		case dependencies.OverlayMissing:
			marker = "MISSING"
		}
		if state != dependencies.OverlayHealthy {
			drifted++
		}
		lines = append(lines, fmt.Sprintf("  [%s] %s", marker, detail))
	}
	if drifted == 0 {
		lines = append(lines, "", fmt.Sprintf("All %d overlay(s) are intact.", len(recorded)))
	}
	ctx.Out(strings.Join(lines, "\n"))
	if drifted > 0 {
		return fmt.Errorf("%d of %d overlay(s) drifted. Run `rlsbl dev sync` to restore the editable installs", drifted, len(recorded))
	}
	return nil
}
