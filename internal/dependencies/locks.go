package dependencies

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/gomodule"
)

// directReferenceSpec stands for the specifier of a requirement the lock
// resolves from a source, on both sides of the comparison: uv records the
// source and no specifier, so only presence is comparable.
const directReferenceSpec = "(direct reference)"

// sourceKeys are the [tool.uv.sources] keys that make uv resolve a
// requirement from a source instead of a specifier. index is not one: it
// selects which registry a version comes from, and the specifier stays.
var sourceKeys = []string{"workspace", "path", "git", "url"}

// specSet is a set of normalized specifiers.
type specSet map[string]bool

func (s specSet) equal(other specSet) bool {
	if len(s) != len(other) {
		return false
	}
	for k := range s {
		if !other[k] {
			return false
		}
	}
	return true
}

func (s specSet) render() string {
	var out []string
	for k := range s {
		if k == "" {
			k = "(no constraint)"
		}
		out = append(out, k)
	}
	sort.Strings(out)
	return strings.Join(out, " / ")
}

// requirementSets maps a normalized name to its specifiers.
type requirementSets map[string]specSet

func (r requirementSets) add(name, spec string) {
	if r[name] == nil {
		r[name] = specSet{}
	}
	r[name][spec] = true
}

// uvSourcesTable is the [tool.uv.sources] table of a decoded manifest.
func uvSourcesTable(doc map[string]any) map[string]any {
	t, _ := table(doc, "tool", "uv", "sources")
	return t
}

// sourceBackedNames are the normalized names whose [tool.uv.sources] entry
// erases the specifier from the lock. Later tables override earlier ones,
// uv's own precedence: a uv workspace root's sources apply to every member
// unless the member declares its own entry for the name.
func sourceBackedNames(tables ...map[string]any) map[string]bool {
	merged := map[string]any{}
	for _, t := range tables {
		for k, v := range t {
			merged[k] = v
		}
	}
	found := map[string]bool{}
	for name, spec := range merged {
		for _, element := range sourceElements(spec) {
			t, ok := element.(map[string]any)
			if !ok {
				continue
			}
			backed := false
			for _, k := range sourceKeys {
				if _, ok := t[k]; ok {
					backed = true
				}
			}
			if backed {
				found[NormalizePypiName(name)] = true
				break
			}
		}
	}
	return found
}

func addRequirement(sets requirementSets, entry any, sourced map[string]bool) {
	text, ok := entry.(string)
	if !ok {
		// A PEP 735 include-group: the included group's own entries are
		// compared under their own name.
		return
	}
	req, ok := ParseRequirement(text)
	if !ok {
		return
	}
	name := req.Normalized()
	if req.DirectReference || sourced[name] {
		sets.add(name, directReferenceSpec)
		return
	}
	sets.add(name, NormalizeSpecifier(req.Specifier))
}

// declaredPypiRequirements are the runtime requirements (dependencies and
// every extra, which uv folds into requires-dist) and the dev requirements
// per group (PEP 735 groups, and the legacy [tool.uv].dev-dependencies uv
// records as the dev group) of a decoded manifest.
func declaredPypiRequirements(doc map[string]any, sourced map[string]bool) (requirementSets, map[string]requirementSets) {
	runtime := requirementSets{}
	project, _ := doc["project"].(map[string]any)
	deps, _ := project["dependencies"].([]any)
	for _, e := range deps {
		addRequirement(runtime, e, sourced)
	}
	optional, _ := project["optional-dependencies"].(map[string]any)
	for _, raw := range optional {
		entries, _ := raw.([]any)
		for _, e := range entries {
			addRequirement(runtime, e, sourced)
		}
	}
	dev := map[string]requirementSets{}
	groups, _ := doc["dependency-groups"].(map[string]any)
	for group, raw := range groups {
		bucket := requirementSets{}
		dev[group] = bucket
		entries, _ := raw.([]any)
		for _, e := range entries {
			addRequirement(bucket, e, sourced)
		}
	}
	if uv, ok := table(doc, "tool", "uv"); ok {
		if legacy, _ := uv["dev-dependencies"].([]any); len(legacy) > 0 {
			bucket := dev["dev"]
			if bucket == nil {
				bucket = requirementSets{}
				dev["dev"] = bucket
			}
			for _, e := range legacy {
				addRequirement(bucket, e, sourced)
			}
		}
	}
	return runtime, dev
}

// lockedPypiRequirements reads a lock's list of requires-dist entries.
func lockedPypiRequirements(raw any) requirementSets {
	out := requirementSets{}
	entries, _ := raw.([]any)
	for _, e := range entries {
		entry, ok := e.(map[string]any)
		if !ok {
			continue
		}
		name, ok := entry["name"].(string)
		if !ok {
			continue
		}
		spec := ""
		for _, k := range []string{"url", "path", "directory", "git", "editable"} {
			if _, ok := entry[k]; ok {
				spec = directReferenceSpec
			}
		}
		if spec == "" {
			s, _ := entry["specifier"].(string)
			spec = NormalizeSpecifier(s)
		}
		out.add(NormalizePypiName(name), spec)
	}
	return out
}

// projectLockEntry is the lock's package entry for the project at relpath
// (relative to the lock): uv records a workspace member, and a standalone
// project, as an editable or virtual source naming its directory, so it is
// found by path, never by a name the lock may not have caught up with.
func projectLockEntry(lock map[string]any, relpath string) (map[string]any, bool) {
	wanted := strings.TrimRight(filepath.ToSlash(relpath), "/")
	packages, _ := lock["package"].([]any)
	for _, raw := range packages {
		pkg, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		source, ok := pkg["source"].(map[string]any)
		if !ok {
			continue
		}
		for _, k := range []string{"editable", "virtual"} {
			if v, ok := source[k].(string); ok && strings.TrimRight(v, "/") == wanted {
				return pkg, true
			}
		}
	}
	return nil, false
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func compareRequirementSets(declared, locked requirementSets, where string) []string {
	var problems []string
	for _, name := range sortedKeys(declared) {
		if _, ok := locked[name]; !ok {
			problems = append(problems, fmt.Sprintf("pypi: %s declares %s, which uv.lock does not record -- the lock predates that requirement. Run `%s`.", where, name, PypiRelock))
		}
	}
	for _, name := range sortedKeys(locked) {
		if _, ok := declared[name]; !ok {
			problems = append(problems, fmt.Sprintf("pypi: uv.lock still records %s under %s, which the manifest no longer declares -- the lock predates that removal. Run `%s`.", name, where, PypiRelock))
		}
	}
	for _, name := range sortedKeys(declared) {
		l, ok := locked[name]
		if ok && !declared[name].equal(l) {
			problems = append(problems, fmt.Sprintf("pypi: %s constrains %s as %s, but uv.lock resolved it from %s -- the constraint changed after the lock was written. Run `%s`.", where, name, declared[name].render(), l.render(), PypiRelock))
		}
	}
	return problems
}

func locksPypi(dir string) (Verdict, error) {
	doc, found, err := readTOML(filepath.Join(dir, PyprojectFile))
	if err != nil || !found {
		return Verdict{}, err
	}
	_, hasProject := doc["project"]
	_, hasTool := doc["tool"]
	if !hasProject && !hasTool {
		return Verdict{}, nil
	}
	search, err := LocateUvLock(dir)
	if err != nil {
		return Verdict{}, err
	}
	if search.Location == nil {
		return Verdict{Notes: []string{"pypi: no uv.lock -- probed " + search.Probed}}, nil
	}
	lockPath := search.Location.Path
	lock, _, err := readTOML(lockPath)
	if err != nil {
		return Verdict{Problems: []string{fmt.Sprintf("pypi: %s could not be read, so it cannot be compared against %s (%v). Run `%s`.", lockPath, PyprojectFile, err, PypiRelock)}}, nil
	}
	relpath := relative(resolved(filepath.Dir(lockPath)), resolved(dir))
	entry, ok := projectLockEntry(lock, relpath)
	if !ok {
		return Verdict{Problems: []string{fmt.Sprintf("pypi: %s has no package entry for this project (expected an editable or virtual source at '%s'), so the lock does not resolve this manifest at all. Run `%s`.", lockPath, filepath.ToSlash(relpath), PypiRelock)}}, nil
	}
	var problems []string
	project, _ := doc["project"].(map[string]any)
	declaredVersion, declaredOK := project["version"].(string)
	lockedVersion, lockedOK := entry["version"].(string)
	if declaredOK && lockedOK && declaredVersion != lockedVersion {
		problems = append(problems, fmt.Sprintf("pypi: %s declares version %s but uv.lock records %s for this project -- the lock predates the version change. Run `%s`.", PyprojectFile, declaredVersion, lockedVersion, PypiRelock))
	}
	// uv omits [package.metadata] for a project that requires nothing, so
	// an absent table is an empty one; a present one that is not a table is
	// a malformed lock.
	metadata := map[string]any{}
	if raw, present := entry["metadata"]; present {
		t, ok := raw.(map[string]any)
		if !ok {
			problems = append(problems, fmt.Sprintf("pypi: this project's uv.lock entry has a metadata section that is not a table, so the requirements it was resolved from cannot be compared. Run `%s`.", PypiRelock))
			return Verdict{Problems: problems}, nil
		}
		metadata = t
	}
	var inherited map[string]any
	if root, claimed, err := FindUvWorkspaceRoot(dir); err != nil {
		return Verdict{}, err
	} else if claimed {
		rootDoc, _, err := readTOML(filepath.Join(root, PyprojectFile))
		if err != nil {
			return Verdict{}, err
		}
		inherited = uvSourcesTable(rootDoc)
	}
	sourced := sourceBackedNames(inherited, uvSourcesTable(doc))
	declaredRuntime, declaredDev := declaredPypiRequirements(doc, sourced)
	problems = append(problems, compareRequirementSets(declaredRuntime, lockedPypiRequirements(metadata["requires-dist"]), PyprojectFile)...)
	lockedDev := map[string]requirementSets{}
	if raw, ok := metadata["requires-dev"].(map[string]any); ok {
		for group, entries := range raw {
			lockedDev[group] = lockedPypiRequirements(entries)
		}
	}
	groups := map[string]bool{}
	for g := range declaredDev {
		groups[g] = true
	}
	for g := range lockedDev {
		groups[g] = true
	}
	for _, group := range sortedKeys(groups) {
		d, l := declaredDev[group], lockedDev[group]
		if d == nil {
			d = requirementSets{}
		}
		if l == nil {
			l = requirementSets{}
		}
		problems = append(problems, compareRequirementSets(d, l, fmt.Sprintf("dependency group '%s'", group))...)
	}
	if len(problems) > 0 {
		return Verdict{Problems: problems}, nil
	}
	return Verdict{Notes: []string{fmt.Sprintf("pypi: %s resolves %s", relative(dir, lockPath), PyprojectFile)}}, nil
}

// npmSections are the package.json maps the lock's root entry records.
var npmSections = []string{"dependencies", "devDependencies", "optionalDependencies", "peerDependencies"}

func npmSection(doc map[string]any, section string) map[string]string {
	out := map[string]string{}
	entries, _ := doc[section].(map[string]any)
	for name, raw := range entries {
		if rng, ok := raw.(string); ok {
			out[NormalizeNpmName(name)] = rng
		}
	}
	return out
}

func locksNpm(dir string) (Verdict, error) {
	manifest, found, err := readJSON(filepath.Join(dir, PackageJSONFile))
	if err != nil || !found {
		return Verdict{}, err
	}
	lockPath := filepath.Join(dir, PackageLockFile)
	lock, found, err := readJSON(lockPath)
	if err != nil {
		return Verdict{Problems: []string{fmt.Sprintf("npm: %s could not be read, so it cannot be compared against %s (%v). Run `%s`.", lockPath, PackageJSONFile, err, NpmRelock)}}, nil
	}
	if !found {
		return Verdict{Notes: []string{"npm: no package-lock.json -- nothing to compare"}}, nil
	}
	var problems []string
	packages, _ := lock["packages"].(map[string]any)
	if rootEntry, ok := packages[""].(map[string]any); ok {
		for _, field := range []string{"name", "version"} {
			declared, ok := manifest[field].(string)
			if !ok {
				continue
			}
			locked, _ := rootEntry[field].(string)
			if declared != locked {
				problems = append(problems, fmt.Sprintf("npm: package.json declares %s %q but package-lock.json records %q for the root package -- the lock predates that change. Run `%s`.", field, declared, locked, NpmRelock))
			}
		}
		for _, section := range npmSections {
			declared := npmSection(manifest, section)
			locked := npmSection(rootEntry, section)
			for _, name := range sortedKeys(declared) {
				if _, ok := locked[name]; !ok {
					problems = append(problems, fmt.Sprintf("npm: package.json declares %s in %s, which package-lock.json's root entry does not record -- the lock predates that dependency. Run `%s`.", name, section, NpmRelock))
				}
			}
			for _, name := range sortedKeys(locked) {
				if _, ok := declared[name]; !ok {
					problems = append(problems, fmt.Sprintf("npm: package-lock.json still records %s in %s, which package.json no longer declares -- the lock predates that removal. Run `%s`.", name, section, NpmRelock))
				}
			}
			for _, name := range sortedKeys(declared) {
				l, ok := locked[name]
				if ok && NormalizeSpecifier(declared[name]) != NormalizeSpecifier(l) {
					problems = append(problems, fmt.Sprintf("npm: package.json constrains %s as %q in %s but package-lock.json records %q -- the range changed after the lock was written. Run `%s`.", name, declared[name], section, l, NpmRelock))
				}
			}
		}
		if len(problems) > 0 {
			return Verdict{Problems: problems}, nil
		}
		return Verdict{Notes: []string{"npm: package-lock.json resolves package.json"}}, nil
	}
	// lockfileVersion 1 records no root requirement map: only presence is
	// comparable, and the note says so.
	lockedNames := map[string]bool{}
	if deps, ok := lock["dependencies"].(map[string]any); ok {
		for name := range deps {
			lockedNames[NormalizeNpmName(name)] = true
		}
	}
	for _, section := range []string{"dependencies", "optionalDependencies"} {
		for _, name := range sortedKeys(npmSection(manifest, section)) {
			if !lockedNames[name] {
				problems = append(problems, fmt.Sprintf("npm: package.json declares %s in %s, which package-lock.json does not resolve -- the lock predates that dependency. Run `%s`.", name, section, NpmRelock))
			}
		}
	}
	return Verdict{
		Problems: problems,
		Notes:    []string{fmt.Sprintf("npm: lockfileVersion %v records no root requirement map, so only the presence of each declared dependency was compared", lock["lockfileVersion"])},
	}, nil
}

// goSumKeys are the module versions a go.sum-format file records.
func goSumKeys(path string) (map[string]bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}
	keys := map[string]bool{}
	for _, line := range strings.Split(string(data), "\n") {
		parts := strings.Fields(line)
		if len(parts) >= 3 {
			keys[parts[0]+" "+strings.TrimSuffix(parts[1], "/go.mod")] = true
		}
	}
	return keys, nil
}

// goWorkSum is the go.work.sum of the go workspace above dir, and false when
// there is none. The search stops at the root of the repository holding dir
// (the first directory with a .git), so a go.work above it, such as the
// working tree's around the release checkout, is never this repository's.
func goWorkSum(dir string) (string, bool, error) {
	current := resolved(dir)
	for {
		work, err := fileExists(filepath.Join(current, gomodule.WorkFileName))
		if err != nil {
			return "", false, err
		}
		if work {
			sum := filepath.Join(current, "go.work.sum")
			found, err := fileExists(sum)
			return sum, found, err
		}
		if _, err := os.Lstat(filepath.Join(current, ".git")); err == nil {
			return "", false, nil
		}
		parent := filepath.Dir(current)
		if parent == current {
			return "", false, nil
		}
		current = parent
	}
}

func locksGo(dir string) (Verdict, error) {
	f, found, err := gomodule.Read(dir)
	if err != nil || !found {
		return Verdict{}, err
	}
	requires, replaces := gomodule.Directives(f)
	local := map[string]bool{}
	for _, r := range replaces {
		if r.IsLocal() {
			local[r.OldPath] = true
		}
	}
	wanted := map[string]bool{}
	for _, r := range requires {
		if !local[r.Path] {
			wanted[r.Path+" "+r.Version] = true
		}
	}
	if len(wanted) == 0 {
		return Verdict{Notes: []string{"go: go.mod requires no external module -- no sums owed"}}, nil
	}
	sumPath := filepath.Join(dir, "go.sum")
	exists, err := fileExists(sumPath)
	if err != nil {
		return Verdict{}, err
	}
	if !exists {
		return Verdict{Problems: []string{fmt.Sprintf("go: go.mod requires %d module(s) but there is no go.sum, so nothing verifies what a build downloads. Run `%s`.", len(wanted), GoRelock)}}, nil
	}
	keys, err := goSumKeys(sumPath)
	if err != nil {
		return Verdict{}, err
	}
	if work, found, err := goWorkSum(dir); err != nil {
		return Verdict{}, err
	} else if found {
		extra, err := goSumKeys(work)
		if err != nil {
			return Verdict{}, err
		}
		for k := range extra {
			keys[k] = true
		}
	}
	var missing []string
	for k := range wanted {
		if !keys[k] {
			missing = append(missing, k)
		}
	}
	sort.Strings(missing)
	if len(missing) > 0 {
		shown := missing
		more := ""
		if len(missing) > 5 {
			shown = missing[:5]
			more = fmt.Sprintf(", and %d more", len(missing)-5)
		}
		return Verdict{Problems: []string{fmt.Sprintf("go: %d required module(s) have no go.sum entry (%s%s), so go.sum no longer covers go.mod. Run `%s`.", len(missing), strings.Join(shown, ", "), more, GoRelock)}}, nil
	}
	return Verdict{Notes: []string{fmt.Sprintf("go: go.sum covers all %d required module(s)", len(wanted))}}, nil
}

// LocksVerdict is EvaluateLocks' answer: a verdict, or the reason there was
// nothing to compare.
type LocksVerdict struct {
	Verdict
	// SkipReason is set when dir holds no manifest of any ecosystem.
	SkipReason string
}

// EvaluateLocks compares each lockfile in dir against the manifest it
// resolves, structurally and offline: the project's own uv.lock entry (its
// version, requires-dist, and requires-dev) against pyproject.toml, the
// root entry of package-lock.json (name, version, and the four dependency
// maps) against package.json, and go.sum against every module go.mod
// requires. A requirement the lock never saw, or one it still carries that
// the manifest dropped, is a stale lock. An absent lockfile is a note:
// whether a project must commit one is not this question.
func EvaluateLocks(dir string) (LocksVerdict, error) {
	var v Verdict
	for _, evaluate := range []func(string) (Verdict, error){locksPypi, locksNpm, locksGo} {
		found, err := evaluate(dir)
		if err != nil {
			return LocksVerdict{}, err
		}
		v.merge(found)
	}
	if len(v.Problems) == 0 && len(v.Notes) == 0 {
		return LocksVerdict{SkipReason: "no pyproject.toml, package.json or go.mod to compare"}, nil
	}
	return LocksVerdict{Verdict: v}, nil
}
