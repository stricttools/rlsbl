package checks

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/targets"
)

// The nested-member family: a member's tools leave the members nested
// inside its directory alone: its test runner does not collect them, its
// upload does not carry their files, and a nested member declares no uv
// workspace source uv would refuse.
func nestedChecks() []check {
	return []check{
		errorCheck("nested-member-runner-exclusion", checkNestedMemberRunnerExclusion),
		errorCheck("nested-member-upload-contents", checkNestedMemberUploadContents),
		errorCheck("nested-member-uv-sources", checkNestedMemberUVSources),
	}
}

// nestedDirs are the absolute directories of the members nested in m.
func nestedDirs(c *Context, m declarations.Member) []string {
	var out []string
	for _, p := range c.Workspace().NestedMemberPaths(m) {
		out = append(out, filepath.Join(c.Root(), filepath.FromSlash(p)))
	}
	return out
}

func checkNestedMemberRunnerExclusion(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	var problems []string
	for _, m := range c.Workspace().Members() {
		nested := nestedDirs(c, m)
		if len(nested) == 0 {
			continue
		}
		seen := map[string]bool{}
		for _, t := range targetsOf(c, m) {
			if t.target.Facts().ScratchTestExclusion != targets.ScratchPytestNorecursedirs || seen[t.dir] {
				continue
			}
			seen[t.dir] = true
			file, missing, owed, err := targets.MissingNestedExclusions(t.dir, targets.NestedPathsInside(t.dir, nested))
			if err != nil {
				panic(unanswered(err.Error()))
			}
			if !owed {
				continue
			}
			rel := dirLabel(c, file)
			problems = append(problems, fmt.Sprintf("%s: its %s test runner would collect the members nested inside it; %s must exclude %s. %s pytest resolves --ignore against the directory it starts in: run it from %s, as rlsbl and CI do.", m.Name, t.target.Name(), rel, strings.Join(missing, ", "), runnerExclusionFix(m, rel, missing), m.Path))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d member(s) whose test runner would collect a nested member", len(problems)), "every member's test runner excludes the members nested inside it")
}

// runnerExclusionFix says how the missing options get into the runner's
// configuration file rel. Scaffold writes them into a member's
// pyproject.toml; it does not scaffold the root member, and it writes no
// pytest.ini.
func runnerExclusionFix(m declarations.Member, rel string, missing []string) string {
	if !m.IsRoot() && filepath.Base(rel) == targets.Pyproject {
		return fmt.Sprintf("Run `rlsbl scaffold` in %s, which writes them.", m.Path)
	}
	where := "addopts under [tool.pytest.ini_options]"
	if filepath.Base(rel) != targets.Pyproject {
		where = "addopts under [pytest]"
	}
	return fmt.Sprintf("Write them by hand (`rlsbl scaffold` writes them only into a pyproject.toml of a member other than the root): add %s to %s in %s.", strings.Join(missing, " "), where, rel)
}

func checkNestedMemberUploadContents(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	var parents []declarations.Member
	for _, m := range c.Workspace().Members() {
		if len(c.Workspace().NestedMemberPaths(m)) > 0 {
			parents = append(parents, m)
		}
	}
	uploads, problems := uploadsOf(c, parents)
	for _, u := range uploads {
		for _, f := range u.listing.Files {
			p := declarations.Join(u.dir, f)
			owner, ok := c.Workspace().OwnerOf(p)
			if !ok || owner.Name == u.member.Name {
				continue
			}
			problems = append(problems, fmt.Sprintf("%s: %s would ship %s, which the member %q owns. %s", u.member.Name, u.listing.Label, p, owner.Name, u.listing.Fix))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d file(s) a member's upload would take from a nested member", len(problems)), "no npm package or Go module zip carries a nested member's files")
}

// uvWorkspaceSources are the names [tool.uv.sources] of the pyproject.toml
// at path declares as { workspace = true }, sorted; none when it does not
// exist.
func uvWorkspaceSources(path string) ([]string, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
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
	var names []string
	for name, spec := range sources {
		if table, ok := spec.(map[string]any); ok && table["workspace"] == true {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names, nil
}

func checkNestedMemberUVSources(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	nested := map[string]bool{}
	for _, m := range c.Workspace().Members() {
		if m.IsRoot() {
			continue
		}
		for _, p := range c.Workspace().NestedMemberPaths(m) {
			nested[p] = true
		}
	}
	var problems []string
	for _, m := range c.Workspace().Members() {
		if !nested[m.Path] {
			continue
		}
		dir := c.Workspace().MemberDir(m)
		names, err := uvWorkspaceSources(filepath.Join(dir, targets.Pyproject))
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if len(names) == 0 {
			continue
		}
		uvRoot, found, err := dependencies.FindUvWorkspaceRoot(dir)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if !found {
			uvRoot = c.Root()
		}
		problems = append(problems, fmt.Sprintf("%s: %s declares %s as `{ workspace = true }` in [tool.uv.sources], but uv refuses a workspace source declared in a member nested inside another member (\"references a workspace in `tool.uv.sources` ... but is not a workspace member\"). Move the entries to [tool.uv.sources] in %s, where uv resolves them for every member.", m.Name, declarations.Join(m.Path, targets.Pyproject), strings.Join(names, ", "), dirLabel(c, filepath.Join(uvRoot, targets.Pyproject))))
	}
	return reportErrors(r, problems, fmt.Sprintf("%d nested member(s) declaring uv workspace sources", len(problems)), "no nested member declares a uv workspace source of its own")
}
