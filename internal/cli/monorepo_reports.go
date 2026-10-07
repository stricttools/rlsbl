package cli

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/monorepo"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/workspace"
)

const monorepoStatusHelp = "Report on every releasable and member of the workspace. Each releasable's row holds its version file's version " +
	"(empty before its first release), its latest release (annotated when this checkout does not contain it), its changelog coverage (the " +
	"commits since the nearest release this checkout contains that touch its members or its records, covered/needing an entry, with the exempt " +
	"ones counted apart, as `rlsbl status` counts them; a fork leaves out its upstream's history), and its members. Each member's row holds its " +
	"targets, its version (its first target's, or its releasable's version file when that releasable publishes nothing, since nothing bumps its " +
	"manifests), its releasable, its flags, and how many members it depends on and how many depend on it. A manifest the dependency graph cannot " +
	"read is refused."

const monorepoOutdatedHelp = "List every dependency between members (from their pyproject.toml and package.json, and their depends_on), each with " +
	"the version the depended-on member's first target has now. A versioned dependency's status is ok when its constraint admits that version, " +
	"outdated when it excludes it, and versioned when the constraint is one this evaluation does not read (several clauses, an exclusion); a path, " +
	"npm workspace, or explicit dependency pins no version and its status is its form."

const monorepoGraphHelp = "Render the members' dependency graph as Graphviz DOT (--format dot: dev-only members gray, members nothing depends on " +
	"green, development edges dashed, peer edges dotted, explicit edges bold) or as an indented tree (--format tree: each member with its " +
	"dependencies below it, labelled [dev], [lib], and [leaf]). --root narrows it to a member and the members it depends on, --reverse to a member " +
	"and the members depending on it, both at most --depth steps away when it is passed. --output writes the rendering to a file instead of " +
	"printing it. The framework's --json prints the graph as a document: the members in topological order (each after the members it depends on) " +
	"with their versions, targets, releasables, flags, dependencies, and dependents, and the edges in the same order. A cycle, a manifest that " +
	"cannot be read, and a version that cannot be read are refused."

const monorepoImpactHelp = "Report what a change reaches through the members' dependency graph: the members it touches, the members depending on " +
	"them directly, and every member depending on them at most --depth steps away (any distance when it is not passed), the ones to test and to " +
	"consider releasing. The change is named by arguments, each a member's name or a path relative to the repository root that exists here (the " +
	"member owning it; a path that is one of rlsbl's own records is refused), or by --since <revision>, the files the commits since it changed. " +
	"An argument that is neither a member nor a path, and one that names a member and lies in another, are refused."

const monorepoCheckNamesHelp = "Check the name of every member that is not dev-only against one --target, with the verdicts of check-name. A " +
	"member's registry_name is checked as declared; any other member's name is checked with --prefix before it and --suffix after it. npm and " +
	"PyPI are asked through their package-level pages, waiting --delay milliseconds between names; go is judged offline. Exits 0 when every name " +
	"is available, 2 when any check ended in an error, and 1 otherwise, as check-name does."

func registerMonorepoReports(r *commandSet) {
	r.add(command{
		path:    []string{"monorepo", "status"},
		help:    monorepoStatusHelp,
		effect:  readOnly,
		payload: monorepoStatusPayloadSchema(),
		render:  renderMonorepoStatus,
		run:     runMonorepoStatus,
	})
	r.add(command{
		path:    []string{"monorepo", "outdated"},
		help:    monorepoOutdatedHelp,
		effect:  readOnly,
		payload: outdatedPayloadSchema(),
		render:  renderOutdated,
		run:     runMonorepoOutdated,
	})
	r.add(command{
		path:   []string{"monorepo", "graph"},
		help:   monorepoGraphHelp,
		effect: mutating,
		flags: []strictcli.Flag{
			strictcli.StringFlag("format", "The rendering", strictcli.Required(),
				strictcli.Choices(
					strictcli.Ch(monorepo.FormatDot, "Graphviz DOT"),
					strictcli.Ch(monorepo.FormatTree, "an indented text tree"),
				)),
			strictcli.StringFlag("output", "A file to write the rendering to instead of printing it", strictcli.Optional()),
			strictcli.StringFlag("root", "Narrow the graph to this member and the members it depends on", strictcli.Optional()),
			strictcli.StringFlag("reverse", "Narrow the graph to this member and the members depending on it", strictcli.Optional()),
			strictcli.IntFlag("depth", "With --root or --reverse, the most steps away a member may be (any distance when not passed)", strictcli.Optional()),
		},
		payload: graphPayloadSchema(),
		render:  renderGraph,
		run:     runMonorepoGraph,
	})
	r.add(command{
		path:   []string{"monorepo", "impact"},
		help:   monorepoImpactHelp,
		effect: readOnly,
		args: []strictcli.Arg{
			strictcli.NewArg("subjects", "Members or paths relative to the repository root that changed", strictcli.ArgOptional(), strictcli.Variadic()),
		},
		flags: []strictcli.Flag{
			strictcli.StringFlag("since", "A revision: the change is every file the commits since it changed", strictcli.Optional()),
			strictcli.IntFlag("depth", "The most steps away a transitive dependent may be (any distance when not passed)", strictcli.Optional()),
		},
		payload: impactPayloadSchema(),
		render:  renderImpact,
		run:     runMonorepoImpact,
	})
	r.add(command{
		path:   []string{"monorepo", "check-names"},
		help:   monorepoCheckNamesHelp,
		effect: readOnly,
		flags: []strictcli.Flag{
			strictcli.StringFlag("target", "The registry or rule set to check the names against", strictcli.Required(),
				strictcli.Choices(
					strictcli.Ch("npm", "the npm registry"),
					strictcli.Ch("pypi", "the Python Package Index"),
					strictcli.Ch("go", goChoiceHelp),
				)),
			strictcli.StringFlag("prefix", "Text put before each member's name (not before a registry_name)", strictcli.Optional()),
			strictcli.StringFlag("suffix", "Text put after each member's name (not after a registry_name)", strictcli.Optional()),
			strictcli.IntFlag("delay", "Milliseconds to wait between consecutive registry requests (the offline go check never waits)", strictcli.Default(200)),
		},
		payload: checkNamesPayloadSchema(),
		render:  renderCheckNames,
		run:     runMonorepoCheckNames,
	})
}

// loadMonorepoWorkspace loads the workspace holding the working directory for
// the monorepo command named command, with a git handle on it.
func loadMonorepoWorkspace(ctx *strictcli.Context, command string) (string, git.Repo, *workspace.Workspace, error) {
	dir, err := os.Getwd()
	if err != nil {
		return "", git.Repo{}, nil, err
	}
	ws, err := monorepo.Load(dir, command)
	if err != nil {
		return "", git.Repo{}, nil, err
	}
	repo, err := git.Open(ctx.Effects(), ws.Root)
	if err != nil {
		return "", git.Repo{}, nil, err
	}
	return dir, repo, ws, nil
}

func runMonorepoStatus(ctx *strictcli.Context, kw map[string]any) (any, error) {
	_, repo, ws, err := loadMonorepoWorkspace(ctx, "status")
	if err != nil {
		return nil, err
	}
	fork, err := forkHistory(ctx.Effects(), ws.Root)
	if err != nil {
		return nil, err
	}
	return monorepo.ReadStatus(repo, ws, fork)
}

func monorepoStatusPayloadSchema() map[string]any {
	coverage := objectSchema(map[string]any{"covered": integerSchema(), "total": integerSchema(), "exempted": integerSchema()})
	return objectSchema(map[string]any{
		"releasables": arraySchema(objectSchema(map[string]any{
			"name":     stringSchema(),
			"version":  nullableStringSchema(),
			"latest":   stringSchema(),
			"coverage": coverage,
			"members":  stringArraySchema(),
		})),
		"members": arraySchema(objectSchema(map[string]any{
			"name":           stringSchema(),
			"path":           stringSchema(),
			"targets":        stringArraySchema(),
			"version":        nullableStringSchema(),
			"version_source": stringSchema(),
			"releasable":     nullableStringSchema(),
			"library":        booleanSchema(),
			"dev_only":       booleanSchema(),
			"dependencies":   integerSchema(),
			"dependents":     integerSchema(),
		})),
	})
}

func orNone(s *string) string {
	if s == nil {
		return "-"
	}
	return *s
}

func renderMonorepoStatus(payload any) string {
	p, err := decodePayload[monorepo.Status](payload)
	if err != nil {
		return renderJSON(payload)
	}
	var rows [][]string
	for _, r := range p.Releasables {
		coverage := fmt.Sprintf("%d/%d", r.Coverage.Covered, r.Coverage.Total)
		if r.Coverage.Exempted > 0 {
			coverage += fmt.Sprintf(" (%d exempted)", r.Coverage.Exempted)
		}
		rows = append(rows, []string{r.Name, orNone(r.Version), r.Latest, coverage, strings.Join(r.Members, ", ")})
	}
	out := "No releasable is declared."
	if len(rows) > 0 {
		out = renderTable([]string{"Releasable", "Version", "Released", "Coverage", "Members"}, rows)
	}
	rows = nil
	for _, m := range p.Members {
		targets := strings.Join(m.Targets, ", ")
		if targets == "" {
			targets = "none"
		}
		version := orNone(m.Version)
		if m.Version != nil && m.VersionSource == monorepo.VersionFromVersionFile {
			version += " (version file)"
		}
		var flags []string
		if m.Library {
			flags = append(flags, "library")
		}
		if m.DevOnly {
			flags = append(flags, "dev-only")
		}
		rows = append(rows, []string{m.Name, m.Path, targets, version, orNone(m.Releasable), strings.Join(flags, ", "), fmt.Sprint(m.Dependencies), fmt.Sprint(m.Dependents)})
	}
	return out + "\n\n" + renderTable([]string{"Member", "Path", "Targets", "Version", "Releasable", "Flags", "Deps", "Rdeps"}, rows)
}

// outdatedPayload is monorepo outdated's payload.
type outdatedPayload struct {
	Dependencies []monorepo.Dependency `json:"dependencies"`
}

func runMonorepoOutdated(ctx *strictcli.Context, kw map[string]any) (any, error) {
	_, _, ws, err := loadMonorepoWorkspace(ctx, "outdated")
	if err != nil {
		return nil, err
	}
	deps, err := monorepo.Outdated(ws)
	if err != nil {
		return nil, err
	}
	return outdatedPayload{Dependencies: deps}, nil
}

func outdatedPayloadSchema() map[string]any {
	return objectSchema(map[string]any{
		"dependencies": arraySchema(objectSchema(map[string]any{
			"member":     stringSchema(),
			"dependency": stringSchema(),
			"form":       stringSchema(),
			"constraint": stringSchema(),
			"current":    nullableStringSchema(),
			"status":     stringSchema(),
		})),
	})
}

func renderOutdated(payload any) string {
	p, err := decodePayload[outdatedPayload](payload)
	if err != nil {
		return renderJSON(payload)
	}
	if len(p.Dependencies) == 0 {
		return "No member depends on another member."
	}
	var rows [][]string
	for _, d := range p.Dependencies {
		constraint := d.Constraint
		if constraint == "" {
			constraint = "(" + d.Form + ")"
		}
		rows = append(rows, []string{d.Member, d.Dependency, constraint, orNone(d.Current), d.Status})
	}
	return renderTable([]string{"Member", "Dependency", "Constraint", "Current", "Status"}, rows)
}

// graphPayload is monorepo graph's payload: the graph, the rendering asked
// for, and the file it was written to (null when it was printed).
type graphPayload struct {
	monorepo.Graph
	Format string  `json:"format"`
	Output *string `json:"output"`
	// DryRun is whether the write to Output was previewed and not made.
	DryRun bool `json:"dry_run"`
}

// depthOf is an optional --depth: -1 (any distance) when not passed, and a
// negative depth refused.
func depthOf(kw map[string]any) (int, error) {
	depth, set := strictcli.GetOpt[int](kw, "depth")
	if !set {
		return -1, nil
	}
	if depth < 0 {
		return 0, fmt.Errorf("--depth must not be negative, got %d", depth)
	}
	return depth, nil
}

func runMonorepoGraph(ctx *strictcli.Context, kw map[string]any) (any, error) {
	dir, _, ws, err := loadMonorepoWorkspace(ctx, "graph")
	if err != nil {
		return nil, err
	}
	depth, err := depthOf(kw)
	if err != nil {
		return nil, err
	}
	g, err := monorepo.ReadGraph(ws, monorepo.GraphRequest{Root: optionalString(kw, "root"), Reverse: optionalString(kw, "reverse"), Depth: depth})
	if err != nil {
		return nil, err
	}
	p := graphPayload{Graph: g, Format: strictcli.Get[string](kw, "format"), DryRun: ctx.DryRun()}
	if output := optionalString(kw, "output"); output != "" {
		text, err := g.Render(p.Format)
		if err != nil {
			return nil, err
		}
		path := output
		if !filepath.IsAbs(path) {
			path = filepath.Join(dir, path)
		}
		if _, err := ctx.Effects().Write(path, text+"\n"); err != nil {
			return nil, fmt.Errorf("writing %s: %w", output, err)
		}
		p.Output = &output
	}
	return p, nil
}

func graphPayloadSchema() map[string]any {
	return objectSchema(map[string]any{
		"order": stringArraySchema(),
		"members": arraySchema(objectSchema(map[string]any{
			"name":               stringSchema(),
			"path":               stringSchema(),
			"version":            nullableStringSchema(),
			"targets":            stringArraySchema(),
			"releasable":         nullableStringSchema(),
			"dev_only":           booleanSchema(),
			"library":            booleanSchema(),
			"dependencies":       stringArraySchema(),
			"dependents":         stringArraySchema(),
			"runtime_dependents": booleanSchema(),
			"leaf":               booleanSchema(),
		})),
		"edges": arraySchema(objectSchema(map[string]any{
			"from":       stringSchema(),
			"to":         stringSchema(),
			"form":       stringSchema(),
			"constraint": stringSchema(),
			"scope":      stringSchema(),
		})),
		"format":  map[string]any{"type": "string", "enum": []any{monorepo.FormatDot, monorepo.FormatTree}},
		"output":  nullableStringSchema(),
		"dry_run": booleanSchema(),
	})
}

func renderGraph(payload any) string {
	p, err := decodePayload[graphPayload](payload)
	if err != nil {
		return renderJSON(payload)
	}
	if p.Output != nil && p.DryRun {
		return "Would write the graph to " + *p.Output
	}
	if p.Output != nil {
		return "Wrote the graph to " + *p.Output
	}
	text, err := p.Graph.Render(p.Format)
	if err != nil {
		return renderJSON(payload)
	}
	return text
}

func runMonorepoImpact(ctx *strictcli.Context, kw map[string]any) (any, error) {
	_, repo, ws, err := loadMonorepoWorkspace(ctx, "impact")
	if err != nil {
		return nil, err
	}
	depth, err := depthOf(kw)
	if err != nil {
		return nil, err
	}
	subjects, err := optionalStrings(kw, "subjects")
	if err != nil {
		return nil, err
	}
	return monorepo.ReadImpact(repo, ws, monorepo.ImpactRequest{Subjects: subjects, Since: optionalString(kw, "since"), Depth: depth})
}

func impactPayloadSchema() map[string]any {
	return objectSchema(map[string]any{
		"changed":               stringArraySchema(),
		"direct_dependents":     stringArraySchema(),
		"transitive_dependents": stringArraySchema(),
	})
}

func renderImpact(payload any) string {
	p, err := decodePayload[monorepo.Impact](payload)
	if err != nil {
		return renderJSON(payload)
	}
	if len(p.Changed) == 0 {
		return "The change touches no member."
	}
	list := func(title string, names []string) []string {
		lines := []string{fmt.Sprintf("%s (%d):", title, len(names))}
		if len(names) == 0 {
			return append(lines, "  (none)")
		}
		for _, n := range names {
			lines = append(lines, "  "+n)
		}
		return lines
	}
	lines := []string{"Impact of a change to: " + strings.Join(p.Changed, ", "), ""}
	lines = append(lines, list("Direct dependents", p.DirectDependents)...)
	lines = append(lines, "")
	lines = append(lines, list("Transitive dependents", p.TransitiveDependents)...)
	lines = append(lines, "")
	scope := "(none)"
	if len(p.TransitiveDependents) > 0 {
		scope = strings.Join(p.TransitiveDependents, ", ")
	}
	lines = append(lines, "Test scope: "+scope, "Release candidates: "+scope)
	return strings.Join(lines, "\n")
}

// memberNameResult is one member's verdict in monorepo check-names'
// payload.
type memberNameResult struct {
	Member      string     `json:"member"`
	CheckedName string     `json:"checked_name"`
	Result      nameResult `json:"result"`
}

// checkNamesPayload is monorepo check-names' payload.
type checkNamesPayload struct {
	DelayMS int                `json:"delay_ms"`
	Results []memberNameResult `json:"results"`
}

func runMonorepoCheckNames(ctx *strictcli.Context, kw map[string]any) (any, error) {
	_, _, ws, err := loadMonorepoWorkspace(ctx, "check-names")
	if err != nil {
		return nil, err
	}
	delay := strictcli.Get[int](kw, "delay")
	if delay < 0 {
		return nil, fmt.Errorf("--delay must not be negative, got %d", delay)
	}
	client, err := registry.New(registry.Reads(ctx.Effects()))
	if err != nil {
		return nil, err
	}
	checks, err := monorepo.CheckNames(client, ws, registry.Ecosystem(strictcli.Get[string](kw, "target")), optionalString(kw, "prefix"), optionalString(kw, "suffix"), time.Duration(delay)*time.Millisecond)
	if err != nil {
		return nil, err
	}
	payload := checkNamesPayload{DelayMS: delay, Results: []memberNameResult{}}
	worst := 0
	for _, c := range checks {
		payload.Results = append(payload.Results, memberNameResult{Member: c.Member, CheckedName: c.Name, Result: payloadResult(c.Result)})
		worst = max(worst, c.Result.ExitCode())
	}
	if worst != 0 {
		return payload, &exitStatus{code: worst}
	}
	return payload, nil
}

func checkNamesPayloadSchema() map[string]any {
	return objectSchema(map[string]any{
		"delay_ms": integerSchema(),
		"results": arraySchema(objectSchema(map[string]any{
			"member":       stringSchema(),
			"checked_name": stringSchema(),
			"result":       checkNameResultSchema(),
		})),
	})
}

func renderCheckNames(payload any) string {
	p, err := decodePayload[checkNamesPayload](payload)
	if err != nil {
		return renderJSON(payload)
	}
	if len(p.Results) == 0 {
		return "No member publishes a name: every member is dev-only."
	}
	var rows [][]string
	var statuses []string
	offline := true
	for _, r := range p.Results {
		rows = append(rows, []string{r.Member, r.CheckedName, r.Result.Status})
		statuses = append(statuses, r.Result.Status)
		offline = offline && r.Result.Target == "go"
	}
	out := renderTable([]string{"Member", "Checked name", "Status"}, rows) + "\n\n" + summaryLine(statuses)
	if !offline {
		out += fmt.Sprintf("\nChecked with %dms delay between names.", p.DelayMS)
		if p.DelayMS == 200 {
			out += " Increase --delay if rate limited."
		}
	}
	return out
}
