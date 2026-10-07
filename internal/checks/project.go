package checks

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/targets"
)

// The project family: what the manifests of the member a run answers for
// say, the declarations, and the repository state every release refuses.
func projectChecks() []check {
	return []check{
		warnCheck("lock", checkLock),
		errorCheck("version-consistency", checkVersionConsistency),
		warnCheck("name-consistency", checkNameConsistency),
		warnCheck("license-consistency", checkLicenseConsistency),
		warnCheck("description-consistency", checkDescriptionConsistency),
		{name: "declarations-valid", errorRun: checkDeclarationsValid, reportsLoadErrors: true},
		errorCheck("license-file", checkLicenseFile),
		errorCheck("npm-private-mismatch", checkNpmPrivateMismatch),
		errorCheck("target-version-readable", checkTargetVersionReadable),
		errorCheck("dunder-version-missing", checkDunderVersionMissing),
		errorCheck("selfdoc-version-drift", checkSelfdocVersionDrift),
		errorCheck("stash-free", checkStashFree),
		errorCheck("cross-repo-path-sources", checkCrossRepoPathSources),
		errorCheck("go-module-identity", checkGoModuleIdentity),
	}
}

// reportErrors finishes an error check: passed when there is no problem,
// otherwise every problem as an error and found.
func reportErrors(r *strictcli.ErrorReporter, problems []string, found, passed string) strictcli.CheckOutcome {
	if len(problems) == 0 {
		return r.Passed(orPassed(passed))
	}
	for _, p := range problems {
		r.Error(p)
	}
	return r.Found(found)
}

// reportWarnings finishes a warning check the same way.
func reportWarnings(r *strictcli.WarnReporter, problems []string, found, passed string) strictcli.CheckOutcome {
	if len(problems) == 0 {
		return r.Passed(orPassed(passed))
	}
	for _, p := range problems {
		r.Warn(p)
	}
	return r.Found(found)
}

// orPassed is a pass message, never empty: strictcli refuses an empty one.
func orPassed(message string) string {
	if strings.TrimSpace(message) == "" {
		return "passed"
	}
	return message
}

// firstLine is s up to its first newline.
func firstLine(s string) string {
	line, _, _ := strings.Cut(s, "\n")
	return line
}

// memberTarget is one target of a member with the target's directory.
type memberTarget struct {
	target targets.Target
	// dir is absolute.
	dir string
}

// targetsOf are the member's targets, declared or detected, each with its
// directory. A declaration naming a directory that does not exist leaves
// the check unanswered.
func targetsOf(c *Context, m declarations.Member) []memberTarget {
	declared, err := targets.MemberTargets(c.Root(), m)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	out := make([]memberTarget, 0, len(declared))
	for _, t := range declared {
		target, err := targets.Get(t.Name)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		out = append(out, memberTarget{target: target, dir: filepath.Join(c.Root(), filepath.FromSlash(m.TargetDir(t)))})
	}
	return out
}

func pathExists(path string) (bool, error) {
	_, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

func checkLock(c *Context, r *strictcli.WarnReporter) strictcli.CheckOutcome {
	stale, err := runstate.IsStale(c.Root())
	if err != nil {
		panic(unanswered(err.Error()))
	}
	present, err := pathExists(filepath.Join(c.Root(), filepath.FromSlash(runstate.LockPath)))
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if present && !stale {
		return r.Passed("an rlsbl process holds the release lock, so the release state is a live run's")
	}
	interrupted, err := runstate.InProgressReleasables(c.Root())
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var problems []string
	for _, name := range interrupted {
		problems = append(problems, fmt.Sprintf("the release of %q was interrupted: %s exists and no rlsbl process holds the release lock. Finish it with `rlsbl release resume`, or record its version as never released with `rlsbl release abandon`", name, runstate.InProgressPath(name)))
	}
	return reportWarnings(r, problems, fmt.Sprintf("%d interrupted release(s)", len(problems)), "no release is left interrupted")
}

// selfdocVersion is the version selfdoc.json in dir declares; found is false
// without the file or without a version in it.
func selfdocVersion(dir string) (version string, found bool, err error) {
	path := filepath.Join(dir, "selfdoc.json")
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	var doc struct {
		Version *string `json:"version"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return "", false, fmt.Errorf("%s is not JSON: %w", path, err)
	}
	if doc.Version == nil {
		return "", false, nil
	}
	return *doc.Version, true, nil
}

func checkVersionConsistency(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	m := c.Member()
	ts := targetsOf(c, m)
	var problems []string
	rel, versioned := c.Releasable()
	if c.Declarations().IsWorkspace() && versioned {
		want, err := c.Workspace().ReadReleasableVersion(rel.Name)
		if err != nil {
			return reportErrors(r, []string{err.Error()}, "the releasable's version file cannot be read", "")
		}
		if c.Workspace().PublishModeOf(m) == declarations.PublishNone {
			return r.Passed(fmt.Sprintf("%s, from the version file of %q, which publishes nothing, so its manifests carry no version a release writes", want, rel.Name))
		}
		var mismatches []string
		for _, t := range ts {
			got, err := t.target.ReadVersion(t.dir)
			if err != nil {
				problems = append(problems, fmt.Sprintf("%s: %v", t.target.Name(), err))
				continue
			}
			if got != want {
				mismatches = append(mismatches, fmt.Sprintf("%s=%s", t.target.Name(), got))
			}
		}
		if len(mismatches) > 0 {
			problems = append(problems, fmt.Sprintf("the manifests of the member %q disagree with %s, the version file of the releasable %q, which holds %s: %s", m.Name, declarations.VersionFile(rel.Name), rel.Name, want, strings.Join(mismatches, ", ")))
		}
		return reportErrors(r, problems, fmt.Sprintf("the manifests of %q disagree with the releasable's version %s", m.Name, want), fmt.Sprintf("%s, from the releasable's version file, and every manifest of %q agrees", want, m.Name))
	}
	if len(ts) == 0 {
		return r.Skipped(fmt.Sprintf("the member %q has no target", m.Name))
	}
	type reading struct{ source, version string }
	var readings []reading
	for _, t := range ts {
		got, err := t.target.ReadVersion(t.dir)
		if err != nil {
			problems = append(problems, fmt.Sprintf("%s: %v", t.target.Name(), err))
			continue
		}
		readings = append(readings, reading{t.target.Name(), got.String()})
	}
	if v, found, err := selfdocVersion(c.Workspace().MemberDir(m)); err != nil {
		problems = append(problems, err.Error())
	} else if found {
		readings = append(readings, reading{"selfdoc.json", v})
	}
	distinct := map[string]bool{}
	parts := make([]string, len(readings))
	for i, rd := range readings {
		distinct[rd.version] = true
		parts[i] = rd.source + "=" + rd.version
	}
	if len(distinct) > 1 {
		problems = append(problems, "version mismatch: "+strings.Join(parts, ", "))
	}
	if len(problems) > 0 {
		return reportErrors(r, problems, fmt.Sprintf("the versions of %q disagree or cannot be read", m.Name), "")
	}
	return r.Passed(fmt.Sprintf("%s across %d version file(s)", readings[0].version, len(readings)))
}

func checkNameConsistency(c *Context, r *strictcli.WarnReporter) strictcli.CheckOutcome {
	m := c.Member()
	ts := targetsOf(c, m)
	if len(ts) == 0 {
		return r.Skipped(fmt.Sprintf("the member %q has no target", m.Name))
	}
	var named, normalized, missing []string
	for _, t := range ts {
		name, found, err := t.target.ReadName(t.dir)
		if err != nil {
			panic(unanswered(fmt.Sprintf("%s: %v", t.target.Name(), err)))
		}
		if !found {
			missing = append(missing, t.target.Name())
			continue
		}
		named = append(named, t.target.Name()+"="+name)
		normalized = append(normalized, t.target.NormalizePackageName(name))
	}
	if len(named) == 0 {
		return r.Skipped(fmt.Sprintf("no target of %q declares a package name", m.Name))
	}
	note := ""
	if len(missing) > 0 {
		note = fmt.Sprintf(" (no name from: %s)", strings.Join(missing, ", "))
	}
	for _, n := range normalized[1:] {
		if n != normalized[0] {
			message := "name mismatch: " + strings.Join(named, ", ") + note
			return reportWarnings(r, []string{message}, message, "")
		}
	}
	return r.Passed(fmt.Sprintf("%s across %d target(s)%s", strings.SplitN(named[0], "=", 2)[1], len(ts), note))
}

// metadataField reads one metadata field of every target of the member that
// states it, as target=value pairs.
func metadataField(c *Context, field func(targets.Metadata) string) (pairs [][2]string) {
	for _, t := range targetsOf(c, c.Member()) {
		meta, err := t.target.ReadMetadata(t.dir)
		if err != nil {
			panic(unanswered(fmt.Sprintf("%s: %v", t.target.Name(), err)))
		}
		if v := field(meta); v != "" {
			pairs = append(pairs, [2]string{t.target.Name(), v})
		}
	}
	return pairs
}

func joinPairs(pairs [][2]string) string {
	parts := make([]string, len(pairs))
	for i, p := range pairs {
		parts[i] = p[0] + "=" + p[1]
	}
	return strings.Join(parts, ", ")
}

// licenseMatches reports whether a manifest's license states the record's:
// equal ignoring case, and npm's UNLICENSED for a proprietary license.
func licenseMatches(manifest, record string) bool {
	if strings.EqualFold(manifest, record) {
		return true
	}
	return record == "proprietary" && manifest == "UNLICENSED"
}

func checkLicenseConsistency(c *Context, r *strictcli.WarnReporter) strictcli.CheckOutcome {
	licenses := metadataField(c, func(m targets.Metadata) string { return m.License })
	var problems []string
	for _, p := range licenses[min(1, len(licenses)):] {
		if !strings.EqualFold(p[1], licenses[0][1]) {
			problems = append(problems, "license mismatch: "+joinPairs(licenses))
			break
		}
	}
	record := c.Record()
	rel, versioned := c.Releasable()
	note := "the lifecycle-and-license record is absent, so the manifests are compared with each other only"
	switch {
	case !versioned:
		note = fmt.Sprintf("the member %q is versioned under no releasable, so no record license applies", c.Member().Name)
	case record.Present():
		period, ok := record.LicenseOn(rel.Name, c.Now())
		if !ok {
			problems = append(problems, fmt.Sprintf("the lifecycle-and-license record holds no license of the releasable %q in effect on %s, so the manifests' licenses cannot be held to it: declare it with `rlsbl transition license --subject %s --license <SPDX identifier> --reason <why>`", rel.Name, c.Now().Format("2006-01-02"), rel.Name))
			break
		}
		note = fmt.Sprintf("the record licenses %q %s", rel.Name, period.License)
		for _, p := range licenses {
			if !licenseMatches(p[1], period.License) {
				problems = append(problems, fmt.Sprintf("%s declares the license %s, but the lifecycle-and-license record licenses the releasable %q %s on %s: correct the manifest", p[0], p[1], rel.Name, period.License, c.Now().Format("2006-01-02")))
			}
		}
	}
	if len(problems) > 0 {
		return reportWarnings(r, problems, fmt.Sprintf("%d license disagreement(s)", len(problems)), "")
	}
	if len(licenses) == 0 {
		return r.Passed("no target declares a license; " + note)
	}
	return r.Passed(fmt.Sprintf("%s across %d target(s); %s", licenses[0][1], len(licenses), note))
}

func checkDescriptionConsistency(c *Context, r *strictcli.WarnReporter) strictcli.CheckOutcome {
	descriptions := metadataField(c, func(m targets.Metadata) string { return m.Description })
	for _, p := range descriptions[min(1, len(descriptions)):] {
		if p[1] != descriptions[0][1] {
			message := "description mismatch: " + joinPairs(descriptions)
			return reportWarnings(r, []string{message}, message, "")
		}
	}
	if len(descriptions) == 0 {
		return r.Passed("no target declares a description")
	}
	return r.Passed(fmt.Sprintf("%q across %d target(s)", descriptions[0][1], len(descriptions)))
}

func checkDeclarationsValid(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	if c.declErr != nil {
		var de *declarations.Error
		if errors.As(c.declErr, &de) {
			problems := make([]string, len(de.Problems))
			for i, p := range de.Problems {
				problems[i] = de.File + ": " + p
			}
			return reportErrors(r, problems, fmt.Sprintf("%s is refused", de.File), "")
		}
		return reportErrors(r, []string{c.declErr.Error()}, "the release declarations cannot be read", "")
	}
	if c.optsErr != nil {
		var oe *options.RefusedError
		if errors.As(c.optsErr, &oe) {
			return reportErrors(r, oe.Lines, fmt.Sprintf("the options in %s are refused", oe.Dir), "")
		}
		return reportErrors(r, []string{c.optsErr.Error()}, "the options cannot be read", "")
	}
	var problems []string
	_, found, err := declarations.LoadTestRunner(c.Root())
	if err != nil {
		problems = append(problems, err.Error())
	}
	settings, err := c.Options().SettingsProblems(c.Declarations(), found)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	problems = append(problems, settings...)
	for _, rel := range c.Declarations().Releasables {
		if rel.DeployCommand == nil {
			continue
		}
		if c.recErr != nil {
			problems = append(problems, fmt.Sprintf("the releasable %q declares deploy_command, which only a proprietary releasable may, and the lifecycle-and-license record that says whether it is cannot be read: %v", rel.Name, c.recErr))
			continue
		}
		if err := publishrules.DeployAllowed(c.record, rel, c.Now()); err != nil {
			problems = append(problems, err.Error())
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d declaration problem(s)", len(problems)), "the release declarations, the test runner's settings, and the adoption declarations are valid")
}

// templateVariable is an unreplaced rlsbl template variable.
var templateVariable = regexp.MustCompile(`\{\{\w+(?:\.\w+)*\}\}`)

func checkLicenseFile(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	dir := c.Workspace().MemberDir(c.Member())
	path := filepath.Join(dir, "LICENSE")
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return reportErrors(r, []string{fmt.Sprintf("%s has no LICENSE file; `rlsbl scaffold` writes it from the license the lifecycle-and-license record holds", dir)}, "LICENSE file not found", "")
	}
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return reportErrors(r, []string{path + " is empty"}, "LICENSE file is empty", "")
	}
	if vars := templateVariable.FindAllString(string(data), -1); len(vars) > 0 {
		message := fmt.Sprintf("%s carries unreplaced template variable(s): %s", path, strings.Join(vars, ", "))
		return reportErrors(r, []string{message}, message, "")
	}
	return r.Passed("LICENSE file valid")
}

func checkNpmPrivateMismatch(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	m := c.Member()
	mode := c.Workspace().PublishModeOf(m)
	checked := 0
	var problems []string
	for _, t := range targetsOf(c, m) {
		if t.target.Name() != targets.NPM {
			continue
		}
		checked++
		path := filepath.Join(t.dir, targets.PackageJSON)
		data, err := os.ReadFile(path)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		var doc struct {
			Private any `json:"private"`
		}
		if err := json.Unmarshal(data, &doc); err != nil {
			panic(unanswered(fmt.Sprintf("%s is not JSON: %v", path, err)))
		}
		if doc.Private == true && mode != declarations.PublishNone {
			problems = append(problems, fmt.Sprintf("%s has \"private\": true, which npm refuses to publish, but the member %q is versioned under a releasable whose publish_mode is %q: delete \"private\" from the manifest, or set publish_mode = \"none\"", path, m.Name, mode))
		}
	}
	if checked == 0 {
		return r.Skipped("no npm target")
	}
	return reportErrors(r, problems, "package.json is private while its releasable publishes", "the npm private flag agrees with publish_mode")
}

func checkTargetVersionReadable(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	ts := targetsOf(c, c.Member())
	if len(ts) == 0 {
		return r.Skipped(fmt.Sprintf("the member %q has no target", c.Member().Name))
	}
	var problems []string
	for _, t := range ts {
		if _, err := t.target.ReadVersion(t.dir); err != nil {
			problems = append(problems, fmt.Sprintf("%s: %v", t.target.Name(), err))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d target(s) cannot read their version", len(problems)), fmt.Sprintf("all %d target(s) read their version", len(ts)))
}

// dunderAssignment is a top-level __version__ assignment.
var dunderAssignment = regexp.MustCompile(`^__version__\s*(:\s*[A-Za-z_][\w.]*\s*)?=`)

// versionConstant is a top-level string assignment whose name holds
// "version" and whose value looks like a version.
var versionConstant = regexp.MustCompile(`^([A-Za-z_]\w*)\s*(?::\s*[A-Za-z_][\w.]*\s*)?=\s*(?:"([^"\\\n]*)"|'([^'\\\n]*)')\s*(?:#.*)?$`)

var versionDigits = regexp.MustCompile(`\d+\.\d+`)

func checkDunderVersionMissing(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	checked := 0
	var problems []string
	for _, t := range targetsOf(c, c.Member()) {
		if t.target.Name() != targets.PyPI {
			continue
		}
		checked++
		root, found, err := targets.PythonPackageRoot(t.dir)
		if err != nil {
			problems = append(problems, err.Error())
			continue
		}
		if !found {
			continue
		}
		init := filepath.Join(t.dir, filepath.FromSlash(root), "__init__.py")
		data, err := os.ReadFile(init)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if finding := dunderFinding(string(data), init); finding != "" {
			problems = append(problems, finding)
		}
	}
	if checked == 0 {
		return r.Skipped("no pypi target")
	}
	return reportErrors(r, problems, "a version constant is not named __version__", "every package's version constant is __version__, or there is none")
}

// dunderFinding names a top-level version constant of a package's
// __init__.py that is not __version__, when the file declares no
// __version__; empty otherwise. The release writes only __version__, so a
// constant by another name would go stale silently.
func dunderFinding(text, path string) string {
	var candidate string
	scanner := bufio.NewScanner(strings.NewReader(text))
	scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for scanner.Scan() {
		line := scanner.Text()
		if dunderAssignment.MatchString(line) {
			return ""
		}
		match := versionConstant.FindStringSubmatch(line)
		if match == nil || candidate != "" {
			continue
		}
		value := match[2] + match[3]
		if strings.Contains(strings.ToLower(match[1]), "version") && versionDigits.MatchString(value) {
			candidate = fmt.Sprintf("%s = %q is in %s, which declares no __version__: the release writes the version only into __version__, so rename it to __version__", match[1], value, path)
		}
	}
	return candidate
}

func checkSelfdocVersionDrift(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	m := c.Member()
	path := filepath.Join(c.Workspace().MemberDir(m), "selfdoc.json")
	declared, found, err := selfdocVersion(c.Workspace().MemberDir(m))
	if err != nil {
		return reportErrors(r, []string{err.Error()}, "selfdoc.json cannot be read", "")
	}
	if !found {
		return r.Skipped("no selfdoc.json declaring a version")
	}
	var want, source string
	if rel, ok := c.Releasable(); ok && c.Declarations().IsWorkspace() {
		v, err := c.Workspace().ReadReleasableVersion(rel.Name)
		if err != nil {
			return reportErrors(r, []string{err.Error()}, "the releasable's version file cannot be read", "")
		}
		want, source = v.String(), declarations.VersionFile(rel.Name)
	} else {
		ts := targetsOf(c, m)
		if len(ts) == 0 {
			return r.Skipped(fmt.Sprintf("the member %q has no target to compare selfdoc.json with", m.Name))
		}
		v, err := ts[0].target.ReadVersion(ts[0].dir)
		if err != nil {
			return reportErrors(r, []string{err.Error()}, "the primary target's version cannot be read", "")
		}
		want, source = v.String(), "the "+ts[0].target.Name()+" target"
	}
	if declared != want {
		message := fmt.Sprintf("%s declares version %s, but %s holds %s: set it to %s (a release writes it; `selfdoc gen` reads it)", path, declared, source, want, want)
		return reportErrors(r, []string{message}, "selfdoc.json's version drifted", "")
	}
	return r.Passed(fmt.Sprintf("selfdoc.json's version matches (%s)", declared))
}

func checkStashFree(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	entries, err := c.Repo().StashEntries()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return reportErrors(r, entries, fmt.Sprintf("%d stash entry/entries: inspect each with `git stash show -p`, commit the work where it belongs, then `git stash drop` it", len(entries)), "no stash entries")
}

func checkCrossRepoPathSources(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	checked := 0
	var problems []string
	for _, t := range targetsOf(c, c.Member()) {
		if t.target.Name() != targets.PyPI {
			continue
		}
		checked++
		found, err := crossRepoPathSources(filepath.Join(t.dir, targets.Pyproject), c.Root())
		if err != nil {
			panic(unanswered(err.Error()))
		}
		problems = append(problems, found...)
	}
	if checked == 0 {
		return r.Skipped("no pypi target")
	}
	return reportErrors(r, problems, fmt.Sprintf("%d [tool.uv.sources] path source(s) resolve outside the repository: depend on the registry release, and keep a local override in dev-sources.toml.local-only", len(problems)), "no path source resolves outside the repository")
}

// crossRepoPathSources names every [tool.uv.sources] path entry of the
// pyproject.toml at path whose path resolves outside boundary.
func crossRepoPathSources(path, boundary string) ([]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	doc, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	tool, _ := (*doc)["tool"].(map[string]any)
	uv, _ := tool["uv"].(map[string]any)
	sources, _ := uv["sources"].(map[string]any)
	resolvedBoundary, err := filepath.EvalSymlinks(boundary)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, name := range sortedNames(sources) {
		elements := []any{sources[name]}
		if list, ok := sources[name].([]any); ok {
			elements = list
		}
		for _, element := range elements {
			table, ok := element.(map[string]any)
			if !ok {
				continue
			}
			raw, ok := table["path"]
			if !ok {
				continue
			}
			declared, ok := raw.(string)
			if !ok {
				out = append(out, fmt.Sprintf("%s: the path of the source %q is not a string", path, name))
				continue
			}
			resolved := declared
			if !filepath.IsAbs(resolved) {
				resolved = filepath.Join(filepath.Dir(path), resolved)
			}
			if target, err := filepath.EvalSymlinks(resolved); err == nil {
				resolved = target
			}
			resolved = filepath.Clean(resolved)
			if resolved != resolvedBoundary && !strings.HasPrefix(resolved, resolvedBoundary+string(filepath.Separator)) {
				out = append(out, fmt.Sprintf("%s: %s: path = %q resolves to %s, outside the repository", path, name, declared, resolved))
			}
		}
	}
	return out, nil
}

func sortedNames(m map[string]any) []string {
	names := make([]string, 0, len(m))
	for n := range m {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

func checkGoModuleIdentity(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	dirs := goModuleDirs(c)
	if len(dirs) == 0 {
		return r.Skipped(noGoTarget)
	}
	origin := ""
	configured, err := c.Repo().RemoteConfigured("origin")
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if configured {
		if origin, err = c.Repo().RemoteURL("origin"); err != nil {
			panic(unanswered(err.Error()))
		}
	}
	verdict, err := gomodule.EvaluateIdentity(c.Root(), dirs, origin)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if verdict.SkipReason != "" {
		return r.Skipped(verdict.SkipReason)
	}
	passed := strings.Join(verdict.Notes, "; ")
	if passed == "" {
		passed = "every module path matches origin"
	}
	return reportErrors(r, verdict.Problems, fmt.Sprintf("%d module path mismatch(es)", len(verdict.Problems)), passed)
}
