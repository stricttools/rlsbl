package dependencies

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// Verdict is what one evaluation found in one project: problems block,
// notes state what was compared.
type Verdict struct {
	Problems []string
	Notes    []string
}

// OK reports whether the evaluation found no problem.
func (v Verdict) OK() bool { return len(v.Problems) == 0 }

func (v *Verdict) merge(other Verdict) {
	v.Problems = append(v.Problems, other.Problems...)
	v.Notes = append(v.Notes, other.Notes...)
}

// WorkspacePackageNames are the package names of every member of a
// workspace (its registry name, or its name): in a workspace every sibling
// is ecosystem-internal without being listed. A standalone repository has
// none.
func WorkspacePackageNames(d *declarations.Releasables) []string {
	if d == nil || !d.IsWorkspace() {
		return nil
	}
	var names []string
	for _, m := range d.Members {
		if m.RegistryName != "" {
			names = append(names, m.RegistryName)
		} else {
			names = append(names, m.Name)
		}
	}
	return names
}

// floorLabel names a dependency section as the floor findings spell it.
func floorLabel(section string) string {
	if group, ok := strings.CutPrefix(section, string(FamilyGroups)+"."); ok {
		return "[dependency-groups]." + group
	}
	return "[project]." + section
}

// declaredPypi is one dependency's first declaration in pyproject.toml.
type declaredPypi struct {
	label       string
	requirement Requirement
}

// pypiDeclared are the first declarations of each dependency in the
// pyproject.toml of dir, keyed by normalized name, in the precedence a floor
// is reported from: [project].dependencies (what a consumer resolves), then
// each extra, then each PEP 735 group. found is false when dir holds no
// pyproject.toml.
func pypiDeclared(dir string) (map[string]declaredPypi, bool, error) {
	path := filepath.Join(dir, PyprojectFile)
	exists, err := fileExists(path)
	if err != nil || !exists {
		return nil, false, err
	}
	p, err := ReadPyproject(path)
	if err != nil {
		return nil, true, err
	}
	entries, err := p.Entries(AllFamilies)
	if err != nil {
		return nil, true, err
	}
	declared := map[string]declaredPypi{}
	for _, e := range entries {
		name := e.Requirement.Normalized()
		if _, seen := declared[name]; !seen {
			declared[name] = declaredPypi{label: floorLabel(e.Section), requirement: e.Requirement}
		}
	}
	return declared, true, nil
}

// PypiLocked are the versions the uv.lock at path resolves, keyed by
// normalized name; found is false when the file is absent. A lock that
// does not parse is an error.
func PypiLocked(path string) (map[string]string, bool, error) {
	doc, found, err := readTOML(path)
	if err != nil || !found {
		return nil, found, err
	}
	locked := map[string]string{}
	packages, _ := doc["package"].([]any)
	for _, raw := range packages {
		pkg, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		name, nameOK := pkg["name"].(string)
		version, versionOK := pkg["version"].(string)
		if nameOK && versionOK {
			locked[NormalizePypiName(name)] = version
		}
	}
	return locked, true, nil
}

// floorProblem is the finding for a dependency whose floor lags what the
// lock resolved, and "" when the floor covers it.
func floorProblem(name, constraint, locked string, reading FloorReading, floor [2]int, where, fix string) string {
	if reading == FloorNotApplicable {
		return ""
	}
	lockedVersion, ok := MajorMinor(locked)
	if !ok {
		return ""
	}
	if reading == FloorNone {
		return fmt.Sprintf("%s: %s declares no version floor, but the lock resolves %s -- a consumer can resolve an older %s than this release was built against; declare %s", name, where, locked, name, fix)
	}
	if laterThan(lockedVersion, floor) {
		return fmt.Sprintf("%s: %s declares '%s', but the lock resolves %s -- the declared floor is behind the version this release was built against; declare %s", name, where, constraint, locked, fix)
	}
	return ""
}

func passNote(ecosystem, name, constraint, version string) string {
	return fmt.Sprintf("%s: %s '%s' covers the locked %s", ecosystem, name, constraint, version)
}

func nothingPolicedNote(ecosystem, manifest string) string {
	return fmt.Sprintf("%s: %s declares none of the enforced ecosystem-internal dependencies", ecosystem, manifest)
}

// sortedIntersection are the keys of declared that names holds, sorted.
func sortedIntersection[V any](names map[string]bool, declared map[string]V) []string {
	var out []string
	for n := range names {
		if _, ok := declared[n]; ok {
			out = append(out, n)
		}
	}
	sort.Strings(out)
	return out
}

func floorsPypi(dir string, names []string) (Verdict, error) {
	declared, found, err := pypiDeclared(dir)
	if err != nil || !found {
		return Verdict{}, err
	}
	search, err := LocateUvLock(dir)
	if err != nil {
		return Verdict{}, err
	}
	if search.Location == nil {
		return Verdict{Notes: []string{"pypi: no uv.lock -- probed " + search.Probed}}, nil
	}
	locked, _, err := PypiLocked(search.Location.Path)
	if err != nil {
		return Verdict{Problems: []string{fmt.Sprintf("pypi: %s could not be read, so no floor could be compared against it (%v). Fix the lock (or run `%s`) and re-run.", search.Location.Path, err, PypiRelock)}}, nil
	}
	wanted := map[string]bool{}
	for _, n := range names {
		wanted[NormalizePypiName(n)] = true
	}
	var v Verdict
	for _, name := range sortedIntersection(wanted, declared) {
		d := declared[name]
		version, ok := locked[name]
		if !ok || d.requirement.DirectReference {
			continue
		}
		reading, floor := PypiFloor(d.requirement.Specifier)
		problem := floorProblem(name, d.requirement.Specifier, version, reading, floor, PyprojectFile+" "+d.label, fmt.Sprintf("%q", name+">="+version))
		if problem == "" {
			v.Notes = append(v.Notes, passNote("pypi", name, d.requirement.Specifier, version))
		} else {
			v.Problems = append(v.Problems, problem)
		}
	}
	if len(v.Problems) == 0 && len(v.Notes) == 0 {
		v.Notes = append(v.Notes, nothingPolicedNote("pypi", PyprojectFile))
	}
	return v, nil
}

// The npm manifest and lock.
const (
	PackageJSONFile = "package.json"
	PackageLockFile = "package-lock.json"
)

// declaredNpm is one dependency's first consumer-visible declaration.
type declaredNpm struct {
	section string
	name    string
	rng     string
}

// npmDeclared are package.json's consumer-visible ranges (dependencies,
// peerDependencies, optionalDependencies; devDependencies never reach a
// consumer), keyed by normalized name. found is false when dir holds no
// package.json.
func npmDeclared(dir string) (map[string]declaredNpm, bool, error) {
	doc, found, err := readJSON(filepath.Join(dir, PackageJSONFile))
	if err != nil || !found {
		return nil, found, err
	}
	declared := map[string]declaredNpm{}
	for _, section := range []string{"dependencies", "peerDependencies", "optionalDependencies"} {
		entries, _ := doc[section].(map[string]any)
		names := make([]string, 0, len(entries))
		for n := range entries {
			names = append(names, n)
		}
		sort.Strings(names)
		for _, n := range names {
			rng, ok := entries[n].(string)
			if !ok {
				continue
			}
			key := NormalizeNpmName(n)
			if _, seen := declared[key]; !seen {
				declared[key] = declaredNpm{section: section, name: n, rng: rng}
			}
		}
	}
	return declared, true, nil
}

// NpmLocked are the versions package-lock.json in dir resolves at the top
// level, keyed by normalized name, from the packages map of lockfile
// versions 2 and 3 or the dependencies tree of version 1; found is false
// when there is no lock.
func NpmLocked(dir string) (map[string]string, bool, error) {
	doc, found, err := readJSON(filepath.Join(dir, PackageLockFile))
	if err != nil || !found {
		return nil, found, err
	}
	locked := map[string]string{}
	if packages, ok := doc["packages"].(map[string]any); ok {
		for key, raw := range packages {
			entry, ok := raw.(map[string]any)
			name, top := strings.CutPrefix(key, "node_modules/")
			if !ok || !top || strings.Contains(name, "node_modules/") {
				continue
			}
			if link, _ := entry["link"].(bool); link {
				continue
			}
			if version, ok := entry["version"].(string); ok {
				locked[NormalizeNpmName(name)] = version
			}
		}
	}
	if len(locked) == 0 {
		if deps, ok := doc["dependencies"].(map[string]any); ok {
			for name, raw := range deps {
				entry, ok := raw.(map[string]any)
				if !ok {
					continue
				}
				if version, ok := entry["version"].(string); ok {
					locked[NormalizeNpmName(name)] = version
				}
			}
		}
	}
	return locked, true, nil
}

func floorsNpm(dir string, names []string) (Verdict, error) {
	declared, found, err := npmDeclared(dir)
	if err != nil || !found {
		return Verdict{}, err
	}
	locked, found, err := NpmLocked(dir)
	if err != nil {
		return Verdict{}, err
	}
	if !found {
		return Verdict{Notes: []string{"npm: no package-lock.json -- no locked versions to compare"}}, nil
	}
	wanted := map[string]bool{}
	for _, n := range names {
		wanted[NormalizeNpmName(n)] = true
	}
	var v Verdict
	for _, key := range sortedIntersection(wanted, declared) {
		d := declared[key]
		version, ok := locked[key]
		if !ok {
			continue
		}
		reading, floor := NpmFloor(d.rng)
		problem := floorProblem(d.name, d.rng, version, reading, floor, PackageJSONFile+" "+d.section, fmt.Sprintf("%q: %q", d.name, ">="+version))
		switch {
		case problem != "":
			v.Problems = append(v.Problems, problem)
		case reading != FloorNotApplicable:
			v.Notes = append(v.Notes, passNote("npm", d.name, d.rng, version))
		}
	}
	if len(v.Problems) == 0 && len(v.Notes) == 0 {
		v.Notes = append(v.Notes, nothingPolicedNote("npm", PackageJSONFile))
	}
	return v, nil
}

// goFloorsNote is the outcome for a Go module: a require line is the
// declared minimum and builds resolve by minimal version selection, so the
// lock can never run ahead of the floor.
const goFloorsNote = "go: go.mod require lines ARE the declared minimums and the build resolves by minimal version selection -- the lock cannot run ahead of the floor, so there is nothing to enforce"

// EvaluateFloors holds the floors the manifests in dir declare for the
// ecosystem-internal dependencies names against what each lock resolved:
// a declared dependency the lock resolves must declare a floor whose
// major.minor is not behind the locked version's. A dependency the manifest
// does not declare is transitive and not this project's floor to state.
// The caller asks only while rlsbl:dep-floors is on for the project, with
// the member's internal_dep_floors and, in a workspace, every sibling's
// package name.
func EvaluateFloors(dir string, names []string) (Verdict, error) {
	if len(names) == 0 {
		return Verdict{Notes: []string{"no ecosystem-internal dependencies to enforce"}}, nil
	}
	var v Verdict
	for _, evaluate := range []func(string, []string) (Verdict, error){floorsPypi, floorsNpm} {
		found, err := evaluate(dir, names)
		if err != nil {
			return Verdict{}, err
		}
		v.merge(found)
	}
	goMod, err := fileExists(filepath.Join(dir, "go.mod"))
	if err != nil {
		return Verdict{}, err
	}
	if goMod {
		v.Notes = append(v.Notes, goFloorsNote)
	}
	return v, nil
}
