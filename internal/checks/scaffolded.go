package checks

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/workflows"
)

// The scaffolded files family: what scaffold wrote, read back through
// internal/scaffold: no merge conflict or template placeholder left in it,
// every member's .gitignore holding the lines scaffold merges in, and no
// publish workflow where the declarations publish nothing.
func scaffoldedChecks() []check {
	return []check{
		errorCheck("publish-mode-workflow", checkPublishModeWorkflow),
		repositoryWide(errorCheck("scaffold-conflicts", checkScaffoldConflicts)),
		errorCheck("scaffold-unreplaced-vars", checkScaffoldUnreplacedVars),
		warnCheck("scaffold-gitignore-stale", checkScaffoldGitignoreStale),
	}
}

// workflowFiles are the repository-relative paths of the workflow files
// (*.yml, *.yaml) in the member's .github/workflows, sorted.
func workflowFiles(c *Context, m declarations.Member) []string {
	dir := filepath.Join(c.Workspace().MemberDir(m), filepath.FromSlash(workflows.Dir))
	entries, err := os.ReadDir(dir)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var out []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !(strings.HasSuffix(name, ".yml") || strings.HasSuffix(name, ".yaml")) {
			continue
		}
		out = append(out, declarations.Join(declarations.Join(m.Path, workflows.Dir), name))
	}
	sort.Strings(out)
	return out
}

// readText reads a repository-relative file as text; found is false when
// it does not exist, is a directory, or is not UTF-8 text.
func readText(c *Context, rel string) (text string, found bool) {
	abs := filepath.Join(c.Root(), filepath.FromSlash(rel))
	info, err := os.Stat(abs)
	if errors.Is(err, os.ErrNotExist) {
		return "", false
	}
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if info.IsDir() {
		return "", false
	}
	data, err := os.ReadFile(abs)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if !utf8.Valid(data) {
		return "", false
	}
	return string(data), true
}

// publishesToARegistry reports whether a workflow publishes: its file name
// says so, or it runs on a published release.
func publishesToARegistry(name, text string) bool {
	return strings.Contains(strings.ToLower(name), "publish") || (strings.Contains(text, "release:") && strings.Contains(text, "published"))
}

func checkPublishModeWorkflow(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	m := c.Member()
	if mode := c.Workspace().PublishModeOf(m); mode != declarations.PublishNone {
		return r.Passed(fmt.Sprintf("the member %q publishes (publish_mode %q)", m.Name, mode))
	}
	fix := fmt.Sprintf("set publish_mode = \"ci\" on the releasable %q in %s", m.Releasable, declarations.ReleasablesFile)
	if !m.Versioned() {
		fix = fmt.Sprintf("version the member under a releasable that publishes, in %s", declarations.ReleasablesFile)
	}
	var problems []string
	for _, rel := range workflowFiles(c, m) {
		text, ok := readText(c, rel)
		if !ok || workflows.IsGenerated(text) {
			continue
		}
		if publishesToARegistry(filepath.Base(rel), text) {
			problems = append(problems, fmt.Sprintf("%s publishes, but the member %q publishes nothing (publish_mode \"none\"): delete it with `saferm delete --on-error abort --description \"the member publishes nothing\" %s` and commit, or %s", rel, m.Name, rel, fix))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d publish workflow(s) of a member that publishes nothing", len(problems)), fmt.Sprintf("no publish workflow in the member %q, which publishes nothing", m.Name))
}

// scaffoldState is the repository's scaffold state; a state that cannot be
// read leaves the check unanswered.
func scaffoldState(c *Context) scaffold.State {
	state, _, err := scaffold.ReadState(c.Root())
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return state
}

func checkScaffoldConflicts(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	candidates := map[string]bool{}
	for p := range scaffoldState(c).Files {
		candidates[p] = true
	}
	for _, m := range c.Workspace().Members() {
		for _, rel := range workflowFiles(c, m) {
			candidates[rel] = true
		}
	}
	paths := make([]string, 0, len(candidates))
	for p := range candidates {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	var problems []string
	for _, rel := range paths {
		text, ok := readText(c, rel)
		if !ok {
			continue
		}
		if regions := scaffold.ConflictRegions(text); len(regions) > 0 {
			problems = append(problems, scaffold.DescribeConflicts(rel, regions)+": resolve each marked region (keep the lines that should stay, delete the markers) and commit the file")
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d scaffolded file(s) with unresolved merge conflict markers", len(problems)), fmt.Sprintf("no merge conflict marker in the %d scaffolded and workflow file(s)", len(paths)))
}

// scaffoldPlaceholder is a placeholder of scaffold's template grammar: a
// variable ({{name}}, {{npm.nodeMatrix}}), a conditional's opening or
// closing, or an action pin. A match right after a '$' is GitHub's own
// ${{ ... }} expression, which scaffoldPlaceholders passes over.
var scaffoldPlaceholder = regexp.MustCompile(`\{\{(?:\w+(?:\.\w+)*|#if\s+\w+(?:\.\w+)*|/if|actionVersion\s+"[^"]*"|action\s+"[^"]*")\}\}`)

// scaffoldPlaceholders are the distinct placeholders text carries, sorted.
func scaffoldPlaceholders(text string) []string {
	seen := map[string]bool{}
	var found []string
	for _, loc := range scaffoldPlaceholder.FindAllStringIndex(text, -1) {
		if loc[0] > 0 && text[loc[0]-1] == '$' {
			continue
		}
		p := text[loc[0]:loc[1]]
		if !seen[p] {
			seen[p] = true
			found = append(found, p)
		}
	}
	sort.Strings(found)
	return found
}

// goreleaserFiles are the names goreleaser reads its configuration from.
var goreleaserFiles = []string{".goreleaser.yml", ".goreleaser.yaml"}

func checkScaffoldUnreplacedVars(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	m := c.Member()
	candidates := map[string]bool{}
	for _, rel := range workflowFiles(c, m) {
		candidates[rel] = true
	}
	for _, name := range goreleaserFiles {
		candidates[declarations.Join(m.Path, name)] = true
	}
	for p := range scaffoldState(c).Files {
		if owner, ok := c.Workspace().OwnerOf(p); ok && owner.Name == m.Name {
			candidates[p] = true
		}
	}
	paths := make([]string, 0, len(candidates))
	for p := range candidates {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	var problems []string
	for _, rel := range paths {
		text, ok := readText(c, rel)
		if !ok {
			continue
		}
		if found := scaffoldPlaceholders(text); len(found) > 0 {
			problems = append(problems, fmt.Sprintf("%s carries the unreplaced template placeholder(s) %s, which scaffold leaves only in a file it did not finish: run `rlsbl scaffold` in %s", rel, strings.Join(found, ", "), m.Path))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d file(s) with unreplaced template placeholders", len(problems)), "no unreplaced template placeholder")
}

func checkScaffoldGitignoreStale(c *Context, r *strictcli.WarnReporter) strictcli.CheckOutcome {
	entries, err := scaffold.GitignoreEntries()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var problems []string
	checked := 0
	for _, m := range c.Members() {
		if m.IsRoot() {
			// scaffold does not scaffold a workspace's root member.
			continue
		}
		checked++
		rel := declarations.Join(m.Path, ".gitignore")
		text, ok := readText(c, rel)
		if !ok {
			problems = append(problems, fmt.Sprintf("%s: %s does not exist; run `rlsbl scaffold` in %s, which writes it", m.Name, rel, m.Path))
			continue
		}
		have := map[string]bool{}
		for _, line := range strings.Split(text, "\n") {
			have[strings.TrimSpace(line)] = true
		}
		var missing []string
		for _, e := range entries {
			if !have[e] {
				missing = append(missing, e)
			}
		}
		if len(missing) > 0 {
			problems = append(problems, fmt.Sprintf("%s: %s lacks %s; run `rlsbl scaffold` in %s, which merges them in", m.Name, rel, strings.Join(missing, ", "), m.Path))
		}
	}
	return reportWarnings(r, problems, fmt.Sprintf("%d member(s) with a stale .gitignore", len(problems)), fmt.Sprintf("the .gitignore of each of the %d scaffolded member(s) holds every line scaffold merges in", checked))
}
