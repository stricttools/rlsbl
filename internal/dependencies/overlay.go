package dependencies

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// The two local-only files that decide, together, whether a project's
// environment runs on local checkouts of sibling projects or on registry
// wheels. Both are gitignored by the *.local-only pattern.
const (
	// OverridesFile is the overlay declaration `rlsbl dev sync` reads.
	OverridesFile = "dev-sources.toml.local-only"
	// SentinelFile is what `rlsbl dev sync` writes after installing the
	// overlays, read to detect a bare `uv sync` reinstalling the registry
	// wheel over one.
	SentinelFile = "dev-overlays-state.toml.local-only"
)

// overlayFix is the fix every overlay-mode refusal names.
const overlayFix = "Run `rlsbl dev sync` (with UV_NO_SYNC=1 exported) to bring the two into agreement, or delete both files to run against registry wheels."

// Overlay is one package installed from a local checkout instead of the
// registry.
type Overlay struct {
	Package string
	// Path is the checkout, absolute with symlinks resolved.
	Path string
	// Version is the version the sentinel recorded, or empty.
	Version string
}

// overlayEntries reads the [[overlay]] tables of one of the two files in
// dir; found is false when the file does not exist. A file that does not
// parse, and an entry that is not a table naming a package and a path, are
// refused.
func overlayEntries(dir, name string) ([]Overlay, bool, error) {
	path := filepath.Join(dir, name)
	doc, found, err := readTOML(path)
	if err != nil {
		return nil, true, fmt.Errorf("%v. %s is regenerable local state: %s", err, name, overlayFix)
	}
	if !found {
		return nil, false, nil
	}
	raw, present := doc["overlay"]
	if !present {
		return nil, true, nil
	}
	list, ok := raw.([]any)
	if !ok {
		return nil, true, fmt.Errorf("'overlay' in %s must be an array of tables. %s", path, overlayFix)
	}
	var out []Overlay
	for i, item := range list {
		t, ok := item.(map[string]any)
		if !ok {
			return nil, true, fmt.Errorf("%s: [[overlay]] entry #%d is not a table. %s", path, i+1, overlayFix)
		}
		pkg, _ := t["package"].(string)
		where, _ := t["path"].(string)
		if pkg == "" || where == "" {
			return nil, true, fmt.Errorf("%s: [[overlay]] entry #%d lacks 'package' or 'path'. %s", path, i+1, overlayFix)
		}
		if !filepath.IsAbs(where) {
			where = filepath.Join(dir, where)
		}
		version, _ := t["version"].(string)
		out = append(out, Overlay{Package: pkg, Path: resolved(where), Version: version})
	}
	return out, true, nil
}

// LoadSentinel reads the sentinel in dir; found is false when it does not
// exist, which is the honest state of a checkout no overlay was ever
// installed in (a fresh CI checkout). A sentinel that exists and cannot be
// read is an error, never "no overlays": that would hide a wiped overlay.
func LoadSentinel(dir string) ([]Overlay, bool, error) {
	return overlayEntries(dir, SentinelFile)
}

func packagesOf(overlays []Overlay) []string {
	names := make([]string, len(overlays))
	for i, o := range overlays {
		names[i] = o.Package
	}
	sort.Strings(names)
	return names
}

func byPackage(overlays []Overlay) map[string]Overlay {
	m := map[string]Overlay{}
	for _, o := range overlays {
		m[o.Package] = o
	}
	return m
}

// ActiveOverlays are the overlays the environment of the project in dir
// runs on, sorted by package, or nil in registry mode (neither file
// present: CI, and every machine with no overlays). The mode is decided by
// both files, as the sandboxed test runner decides it, and any disagreement
// between them (one without the other, different packages, a package
// synced from another checkout than declared, a declared checkout that is
// gone) is refused: a command picking either answer would wipe the overlays
// or test against a checkout nobody declared.
func ActiveOverlays(dir string) ([]Overlay, error) {
	declared, _, err := overlayEntries(dir, OverridesFile)
	if err != nil {
		return nil, err
	}
	synced, _, err := overlayEntries(dir, SentinelFile)
	if err != nil {
		return nil, err
	}
	switch {
	case len(declared) == 0 && len(synced) == 0:
		return nil, nil
	case len(declared) == 0:
		return nil, fmt.Errorf("%s records overlays (%s) but %s declares none, so what the environment holds matches no declaration. %s", SentinelFile, strings.Join(packagesOf(synced), ", "), OverridesFile, overlayFix)
	case len(synced) == 0:
		return nil, fmt.Errorf("%s declares overlays (%s) but %s does not: they were declared but never installed. %s", OverridesFile, strings.Join(packagesOf(declared), ", "), SentinelFile, overlayFix)
	}
	d, s := byPackage(declared), byPackage(synced)
	if strings.Join(sortedKeys(d), ",") != strings.Join(sortedKeys(s), ",") {
		return nil, fmt.Errorf("%s declares %s but %s records %s. %s", OverridesFile, strings.Join(sortedKeys(d), ", "), SentinelFile, strings.Join(sortedKeys(s), ", "), overlayFix)
	}
	var out []Overlay
	for _, pkg := range sortedKeys(d) {
		if d[pkg].Path != s[pkg].Path {
			return nil, fmt.Errorf("overlay '%s' is declared at %s but was synced from %s. %s", pkg, d[pkg].Path, s[pkg].Path, overlayFix)
		}
		info, err := os.Stat(d[pkg].Path)
		if err != nil || !info.IsDir() {
			return nil, fmt.Errorf("overlay '%s': the checkout %s does not exist. %s", pkg, d[pkg].Path, overlayFix)
		}
		out = append(out, Overlay{Package: pkg, Path: d[pkg].Path})
	}
	return out, nil
}

// CollectActiveOverlays are the active overlays of every project directory,
// merged: a workspace's members share one environment, so a sync at its
// root keeps every member's overlays. Two projects overlaying one package
// from different checkouts are refused, since one environment cannot hold
// both.
func CollectActiveOverlays(dirs []string) ([]Overlay, error) {
	merged := map[string]Overlay{}
	for _, dir := range dirs {
		overlays, err := ActiveOverlays(dir)
		if err != nil {
			return nil, err
		}
		for _, o := range overlays {
			if existing, ok := merged[o.Package]; ok && existing.Path != o.Path {
				return nil, fmt.Errorf("workspace projects overlay '%s' from two different checkouts (%s and %s); one shared environment cannot hold both. Reconcile the %s files and run `rlsbl dev sync` again.", o.Package, existing.Path, o.Path, OverridesFile)
			}
			merged[o.Package] = o
		}
	}
	var out []Overlay
	for _, pkg := range sortedKeys(merged) {
		out = append(out, merged[pkg])
	}
	return out, nil
}

// ProjectEnvironment is the environment uv manages for the project in dir:
// the .venv of the uv workspace root that claims it (a member shares the
// root's environment and has none of its own), or of dir itself. uvProject
// is UV_PROJECT_ENVIRONMENT as the caller read it (empty when unset), uv's
// own relocation of the environment, which a relative value resolves
// against that root.
func ProjectEnvironment(dir, uvProject string) (string, error) {
	base, err := filepath.Abs(dir)
	if err != nil {
		return "", err
	}
	if root, claimed, err := FindUvWorkspaceRoot(base); err != nil {
		return "", err
	} else if claimed {
		base = root
	}
	if uvProject != "" {
		if filepath.IsAbs(uvProject) {
			return uvProject, nil
		}
		return filepath.Join(base, uvProject), nil
	}
	return filepath.Join(base, ".venv"), nil
}

// Installed is how one package is installed in an environment.
type Installed struct {
	// Found is false when the environment holds no distribution of it.
	Found bool
	// Editable is set for an editable install; Path is then the checkout
	// its direct_url.json points at.
	Editable bool
	Path     string
	Version  string
	// Environment is the environment inspected.
	Environment string
}

// sitePackages are the site-packages directories of an environment, one per
// Python version present.
func sitePackages(env string) ([]string, error) {
	var out []string
	for _, pattern := range []string{filepath.Join(env, "lib", "python*", "site-packages"), filepath.Join(env, "Lib", "site-packages")} {
		matches, err := filepath.Glob(pattern)
		if err != nil {
			return nil, err
		}
		for _, m := range matches {
			if info, err := os.Stat(m); err == nil && info.IsDir() {
				out = append(out, m)
			}
		}
	}
	return out, nil
}

// distMetadata reads the Name and Version headers of a dist-info METADATA
// file.
func distMetadata(path string) (name, version string, err error) {
	f, err := os.Open(path)
	if err != nil {
		return "", "", err
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			break
		}
		if v, ok := strings.CutPrefix(line, "Name:"); ok {
			name = strings.TrimSpace(v)
		}
		if v, ok := strings.CutPrefix(line, "Version:"); ok {
			version = strings.TrimSpace(v)
		}
	}
	return name, version, scanner.Err()
}

// directURL reads a distribution's PEP 610 direct_url.json: a registry wheel
// has none; an editable install records dir_info.editable and a file:// URL
// of its checkout. It is the only evidence of editability, since an
// editable install leaves no package directory in site-packages.
func directURL(distInfo string) (editable bool, path string, err error) {
	data, err := os.ReadFile(filepath.Join(distInfo, "direct_url.json"))
	if errors.Is(err, os.ErrNotExist) {
		return false, "", nil
	}
	if err != nil {
		return false, "", err
	}
	var doc struct {
		URL     string `json:"url"`
		DirInfo struct {
			Editable bool `json:"editable"`
		} `json:"dir_info"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return false, "", fmt.Errorf("%s does not parse: %w", filepath.Join(distInfo, "direct_url.json"), err)
	}
	if strings.HasPrefix(doc.URL, "file:") {
		u, err := url.Parse(doc.URL)
		if err != nil {
			return false, "", fmt.Errorf("%s: %w", filepath.Join(distInfo, "direct_url.json"), err)
		}
		path = u.Path
	}
	return doc.DirInfo.Editable, path, nil
}

// InspectInstalled is how pkg is installed in the environment of the
// project in dir (see ProjectEnvironment), read from the dist-info
// directories of its site-packages.
func InspectInstalled(dir, uvProject, pkg string) (Installed, error) {
	env, err := ProjectEnvironment(dir, uvProject)
	if err != nil {
		return Installed{}, err
	}
	sites, err := sitePackages(env)
	if err != nil {
		return Installed{}, err
	}
	want := NormalizePypiName(pkg)
	for _, site := range sites {
		infos, err := filepath.Glob(filepath.Join(site, "*.dist-info"))
		if err != nil {
			return Installed{}, err
		}
		sort.Strings(infos)
		for _, info := range infos {
			name, version, err := distMetadata(filepath.Join(info, "METADATA"))
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			if err != nil {
				return Installed{}, fmt.Errorf("reading %s: %w", filepath.Join(info, "METADATA"), err)
			}
			if name == "" || NormalizePypiName(name) != want {
				continue
			}
			editable, path, err := directURL(info)
			if err != nil {
				return Installed{}, err
			}
			return Installed{Found: true, Editable: editable, Path: path, Version: version, Environment: env}, nil
		}
	}
	return Installed{Environment: env}, nil
}

// OverlayState is an overlay's health.
type OverlayState string

// The overlay states.
const (
	OverlayHealthy OverlayState = "healthy"
	OverlayWiped   OverlayState = "wiped"
	OverlayMissing OverlayState = "missing"
)

// ClassifyOverlay compares a sentinel entry with what the environment
// holds, and returns its state with a line naming the package and the fix.
func ClassifyOverlay(entry Overlay, installed Installed) (OverlayState, string) {
	if !installed.Found {
		where := ""
		if installed.Environment != "" {
			where = " (" + installed.Environment + ")"
		}
		return OverlayMissing, fmt.Sprintf("%s: declared as an editable overlay of %s but not installed in the project environment%s at all -- run `rlsbl dev sync`", entry.Package, entry.Path, where)
	}
	if !installed.Editable {
		version := installed.Version
		if version == "" {
			version = "unknown version"
		}
		return OverlayWiped, fmt.Sprintf("%s: overlay wiped -- now a registry install (%s), no longer editable at %s. A bare `uv sync`/`uv run` reinstalled the locked wheel; run `rlsbl dev sync` to restore the overlay", entry.Package, version, entry.Path)
	}
	if installed.Path == "" || resolved(installed.Path) != resolved(entry.Path) {
		return OverlayWiped, fmt.Sprintf("%s: editable install points at %s, not the declared overlay path %s -- run `rlsbl dev sync`", entry.Package, installed.Path, entry.Path)
	}
	version := installed.Version
	if version == "" {
		version = "dynamic"
	}
	return OverlayHealthy, fmt.Sprintf("%s: editable at %s (version %s)", entry.Package, entry.Path, version)
}
