package rewrite

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// DepFloorsReason is the reason of the rlsbl:dep-floors entry the
// conversion writes.
const DepFloorsReason = "rlsbl rewrite uv-path-sources floored this project's internal dependencies at their locked versions, and dep-floors polices those floors"

// Conversion is one dependency's pending conversion.
type Conversion struct {
	// Name is the name as declared; Normalized is its PEP 503 form.
	Name       string
	Normalized string
	// Locked is the version the lock resolves, the floor.
	Locked string
	// Source is the local source type of its [tool.uv.sources] entry, or
	// empty when only a direct reference sources it.
	Source dependencies.SourceType
	// Sections are the manifest sections declaring it.
	Sections []string
	// DependencyEntries and SourceEntries are what the apply rewrites: the
	// dependency-array entries naming it, and its sources entry (0 or 1).
	DependencyEntries int
	SourceEntries     int
}

// Occurrences is everything the apply rewrites for the dependency.
func (c Conversion) Occurrences() int { return c.DependencyEntries + c.SourceEntries }

// configStep and optionStep are the data of the declaration and option
// items.
type (
	configStep struct{ additions []string }
	optionStep struct{ scope string }
)

// PathSources converts the path- and workspace-sourced dependencies of one
// member's pyproject.toml into registry floors at the locked versions.
type PathSources struct {
	// Root is the repository root, absolute.
	Root string
	// Dir is the directory the command runs in, absolute: the member whose
	// territory it lies in is converted.
	Dir string
	// Registry is rlsbl's options registry.
	Registry *options.Registry

	decls      *declarations.Releasables
	member     declarations.Member
	projectDir string
	pyproject  string
}

// counts are the dependency-array entries and local source entries of
// normalized in the manifest.
func counts(p *dependencies.Pyproject, normalized string) (deps, sources int, sections []string, source dependencies.SourceType, err error) {
	found, err := p.EntriesNaming(map[string]bool{normalized: true}, dependencies.AllFamilies)
	if err != nil {
		return 0, 0, nil, "", err
	}
	for _, e := range found {
		sections = append(sections, e.Section)
	}
	local, err := p.UvLocalSources()
	if err != nil {
		return 0, 0, nil, "", err
	}
	for _, s := range local {
		if dependencies.NormalizePypiName(s.Name) == normalized {
			sources++
			if source == "" {
				source = s.Type
			}
		}
	}
	return len(found), sources, sections, source, nil
}

// locate finds the member and the manifest.
func (c *PathSources) locate() error {
	d, err := declarations.Load(c.Root)
	if err != nil {
		return err
	}
	w, err := workspace.New(c.Root, d)
	if err != nil {
		return err
	}
	member, err := w.MemberAtDirectory(c.Dir)
	if err != nil {
		return err
	}
	c.decls, c.member = d, member
	c.projectDir = w.MemberDir(member)
	for _, t := range member.Targets {
		if t.Name == targets.PyPI {
			c.projectDir = filepath.Join(c.Root, filepath.FromSlash(member.TargetDir(t)))
		}
	}
	c.pyproject = filepath.Join(c.projectDir, dependencies.PyprojectFile)
	if _, err := os.Stat(c.pyproject); err != nil {
		return fmt.Errorf("no %s at %s: this command rewrites a Python project's manifest, and the member %q declares none", dependencies.PyprojectFile, c.projectDir, member.Name)
	}
	return nil
}

// conversions are every path- or workspace-sourced dependency with its
// locked version: a [tool.uv.sources] path or workspace entry (whose
// dependency entry is usually a bare name), or a direct reference in a
// dependency array.
func (c *PathSources) conversions(p *dependencies.Pyproject) ([]Conversion, *dependencies.LockLocation, error) {
	search, err := dependencies.LocateUvLock(c.projectDir)
	if err != nil {
		return nil, nil, err
	}
	if search.Location == nil {
		return nil, nil, fmt.Errorf("no uv.lock for %s: the floor is the version the lock resolves, so there is nothing to floor at. Probed %s. Run `uv lock` and run again.", c.projectDir, search.Probed)
	}
	locked, _, err := dependencies.PypiLocked(search.Location.Path)
	if err != nil {
		return nil, nil, fmt.Errorf("%v: the version to floor at cannot be determined. Fix the lock (or run `uv lock`) and run again.", err)
	}
	names := map[string]string{}
	local, err := p.UvLocalSources()
	if err != nil {
		return nil, nil, err
	}
	for _, s := range local {
		names[dependencies.NormalizePypiName(s.Name)] = s.Name
	}
	refs, err := p.DirectReferences(dependencies.AllFamilies)
	if err != nil {
		return nil, nil, err
	}
	for _, r := range refs {
		if _, ok := names[r.Requirement.Normalized()]; !ok {
			names[r.Requirement.Normalized()] = r.Requirement.Name
		}
	}
	normalized := make([]string, 0, len(names))
	for n := range names {
		normalized = append(normalized, n)
	}
	sort.Strings(normalized)
	var out []Conversion
	for _, n := range normalized {
		version, ok := locked[n]
		if !ok {
			return nil, nil, fmt.Errorf("%s: %s does not resolve this package, so there is no locked version to floor at. Run `uv lock` and run again.", names[n], search.Location.Label(c.projectDir))
		}
		deps, sources, sections, source, err := counts(p, n)
		if err != nil {
			return nil, nil, err
		}
		out = append(out, Conversion{Name: names[n], Normalized: n, Locked: version, Source: source, Sections: sections, DependencyEntries: deps, SourceEntries: sources})
	}
	return out, search.Location, nil
}

// probePublished refuses unless PyPI's project document lists the locked
// version. The document lists every published version, so the probe never
// names a version that may be unpublished. An answer that is not a
// definitive listing refuses too: "could not ask" is no evidence of
// publication.
func probePublished(client registry.Client, conv Conversion) error {
	project, found, err := client.PypiProject(conv.Name)
	if err != nil {
		return fmt.Errorf("%s %s: could not determine whether it is published (%v). A floor is not written on an unanswered probe: fix the connection to PyPI and run again.", conv.Name, conv.Locked, err)
	}
	if !found || !project.Has(conv.Locked) {
		return fmt.Errorf("%s %s is not published on PyPI, so a '%s>=%s' floor would be unsatisfiable for every consumer. Release %s first, then run this command again.", conv.Name, conv.Locked, conv.Name, conv.Locked, conv.Name)
	}
	return nil
}

// observe builds the plan: one item per dependency, then the declaration
// item and the option item.
func (c *PathSources) observe(o previewapply.Observer) (previewapply.Preview, error) {
	if err := c.locate(); err != nil {
		return previewapply.Preview{}, err
	}
	p, err := dependencies.ReadPyproject(c.pyproject)
	if err != nil {
		return previewapply.Preview{}, err
	}
	convs, lock, err := c.conversions(p)
	if err != nil {
		return previewapply.Preview{}, err
	}
	if len(convs) == 0 {
		return previewapply.Single(previewapply.Item{
			Key:     "(project)",
			State:   "nothing_to_convert",
			Summary: "no [tool.uv.sources] path or workspace entry and no direct reference in any dependency section.",
		}), nil
	}
	client, err := registry.New(o)
	if err != nil {
		return previewapply.Preview{}, err
	}
	var items []previewapply.Item
	var converted []string
	for _, conv := range convs {
		if err := probePublished(client, conv); err != nil {
			return previewapply.Preview{}, err
		}
		facts := []string{"locked version: " + conv.Locked, lock.Describe(c.projectDir)}
		if conv.Source != "" {
			facts = append(facts, fmt.Sprintf("[tool.uv.sources].%s: %s source", conv.Name, conv.Source))
		}
		for _, s := range conv.Sections {
			facts = append(facts, fmt.Sprintf("[%s]: declares this dependency", s))
		}
		facts = append(facts, fmt.Sprintf("published on PyPI: yes (%s)", conv.Locked))
		var actions []string
		if conv.DependencyEntries > 0 {
			actions = append(actions, fmt.Sprintf("apply would rewrite %s to '%s>=%s'.", counted(conv.DependencyEntries, "dependency entry", "dependency entries"), conv.Name, conv.Locked))
		}
		if conv.SourceEntries > 0 {
			actions = append(actions, fmt.Sprintf("apply would delete [tool.uv.sources].%s.", conv.Name))
		}
		items = append(items, previewapply.Item{
			Key:     conv.Name,
			State:   "convert",
			Summary: fmt.Sprintf("%s -> %s>=%s", counted(conv.Occurrences(), "entry", "entries"), conv.Name, conv.Locked),
			Facts:   facts,
			Actions: actions,
			Data:    conv,
		})
		converted = append(converted, conv.Name)
	}
	items = append(items, c.declarationItem(converted))
	option, err := c.optionItem()
	if err != nil {
		return previewapply.Preview{}, err
	}
	items = append(items, option)
	return previewapply.NewPreview(items...)
}

// declarationItem adds the converted names to the member's
// internal_dep_floors, so dep-floors polices the floors the conversion
// writes.
func (c *PathSources) declarationItem(converted []string) previewapply.Item {
	declared := map[string]bool{}
	for _, n := range c.member.InternalDepFloors {
		declared[n] = true
	}
	var additions []string
	for _, n := range converted {
		if !declared[n] {
			additions = append(additions, n)
		}
	}
	sort.Strings(additions)
	present := "no"
	if len(c.member.InternalDepFloors) > 0 {
		present = "yes"
	}
	item := previewapply.Item{
		Key:     declarations.ReleasablesFile,
		State:   "floors_already_declared",
		Summary: fmt.Sprintf("the member %q's internal_dep_floors already names every converted dependency", c.member.Name),
		Facts:   []string{fmt.Sprintf("internal_dep_floors declared on the member %q: %s", c.member.Name, present)},
		Data:    configStep{},
	}
	if len(additions) > 0 {
		item.State = "declare_floors"
		item.Summary = fmt.Sprintf("the member %q's internal_dep_floors would gain %s", c.member.Name, strings.Join(additions, ", "))
		item.Actions = []string{fmt.Sprintf("apply would set internal_dep_floors to %s.", strings.Join(union(c.member.InternalDepFloors, additions), ", "))}
		item.Data = configStep{additions: additions}
	}
	return item
}

// union is a and b without repeats, sorted.
func union(a, b []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, x := range append(append([]string(nil), a...), b...) {
		if !seen[x] {
			seen[x] = true
			out = append(out, x)
		}
	}
	sort.Strings(out)
	return out
}

// optionItem switches rlsbl:dep-floors on for the member, or records that
// it is on already.
func (c *PathSources) optionItem() (previewapply.Item, error) {
	decl, ok := c.Registry.Declaration(options.DepFloors)
	if !ok {
		return previewapply.Item{}, fmt.Errorf("%s%s is not an option this rlsbl build's registry declares, a defect of the build: `rlsbl options registry` lists what it declares", options.Prefix, options.DepFloors)
	}
	key := options.Dir + "/" + decl.Subject + ".toml"
	loaded, err := options.Load(c.Registry, c.Root, c.decls)
	if err != nil {
		return previewapply.Item{}, err
	}
	v, err := loaded.Value(options.DepFloors, c.member.Path)
	if err != nil {
		return previewapply.Item{}, err
	}
	fact := fmt.Sprintf("%s%s: %s (%s)", options.Prefix, options.DepFloors, v.Value, v.Source)
	if v.Value != options.Off {
		return previewapply.Item{Key: key, State: "dep_floors_already_on", Summary: fmt.Sprintf("%s%s is already %s", options.Prefix, options.DepFloors, v.Value), Facts: []string{fact}}, nil
	}
	scope := options.MemberScope(c.decls, c.member.Path)
	scoped := ""
	if scope != "" {
		scoped = fmt.Sprintf(", scope %q", scope)
	}
	return previewapply.Item{
		Key:     key,
		State:   "switch_on_dep_floors",
		Summary: fmt.Sprintf("%s%s would be switched on", options.Prefix, options.DepFloors),
		Facts:   []string{fact},
		Actions: []string{fmt.Sprintf("apply would write the entry %s%s (current error, ideal error%s).", options.Prefix, options.DepFloors, scoped)},
		Data:    optionStep{scope: scope},
	}, nil
}

// apply performs one item.
func (c *PathSources) apply(ctx *strictcli.Context, e *strictcli.Effects, it previewapply.Item) error {
	switch step := it.Data.(type) {
	case Conversion:
		p, err := dependencies.ReadPyproject(c.pyproject)
		if err != nil {
			return err
		}
		deps, sources, _, _, err := counts(p, step.Normalized)
		if err != nil {
			return err
		}
		if err := previewapply.CountMoved(step.Name, step.Occurrences(), deps+sources); err != nil {
			return err
		}
		if _, err := p.FloorEntries(map[string]string{step.Normalized: step.Locked}, dependencies.AllFamilies); err != nil {
			return err
		}
		if sources > 0 {
			local, err := p.UvLocalSources()
			if err != nil {
				return err
			}
			var names []string
			for _, s := range local {
				if dependencies.NormalizePypiName(s.Name) == step.Normalized {
					names = append(names, s.Name)
				}
			}
			if _, err := p.RemoveUvSources(names); err != nil {
				return err
			}
		}
		if err := replaceFile(e, c.pyproject, p.Bytes()); err != nil {
			return err
		}
		ctx.Info(fmt.Sprintf("  %s: floored at >=%s (%s)", step.Name, step.Locked, counted(deps+sources, "entry", "entries")))
	case configStep:
		if len(step.additions) == 0 {
			return nil
		}
		data, err := os.ReadFile(filepath.Join(c.Root, filepath.FromSlash(declarations.ReleasablesFile)))
		if err != nil {
			return err
		}
		ed, err := declarations.NewEditor(data)
		if err != nil {
			return err
		}
		floors := union(c.member.InternalDepFloors, step.additions)
		if err := ed.SetInternalDepFloors(c.member.Path, floors); err != nil {
			return err
		}
		edited, _, err := ed.Result()
		if err != nil {
			return err
		}
		if _, err := declarations.Write(e, c.Root, edited); err != nil {
			return err
		}
		ctx.Info(fmt.Sprintf("  internal_dep_floors of %q: added %s", c.member.Name, strings.Join(step.additions, ", ")))
	case optionStep:
		result, err := options.Set(e, c.Registry, c.Root, c.decls, options.SetRequest{
			ID: options.Prefix + options.DepFloors, Current: "error", Ideal: "error", Reason: DepFloorsReason, Scope: step.scope,
		})
		if err != nil {
			return err
		}
		ctx.Info(fmt.Sprintf("  %s%s: %s in %s", options.Prefix, options.DepFloors, result.Action, result.File))
	}
	return nil
}

// RunUvPathSources is `rlsbl rewrite uv-path-sources`: convert the path- and
// workspace-sourced dependencies of the member whose territory dir lies in,
// in the repository rooted at root, previewed under --dry-run.
func RunUvPathSources(ctx *strictcli.Context, root, dir string, reg *options.Registry) error {
	c := &PathSources{Root: root, Dir: dir, Registry: reg}
	preview, err := previewapply.Reconcile(ctx, previewapply.Reconciler{
		Observe: c.observe,
		Apply: func(e *strictcli.Effects, it previewapply.Item) error {
			return c.apply(ctx, e, it)
		},
		ShowKeys: true,
	})
	if err != nil || ctx.DryRun() {
		return err
	}
	converted := 0
	for _, s := range preview.States() {
		if s == "convert" {
			converted++
		}
	}
	if converted == 0 {
		ctx.Info("Nothing to convert: no path or workspace sources declared.")
	} else {
		ctx.Info(fmt.Sprintf("Converted %s.", counted(converted, "path-sourced dependency", "path-sourced dependencies")))
	}
	return nil
}
