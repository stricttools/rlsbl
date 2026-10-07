package migration

import (
	"encoding/json"
	"fmt"
	"path"
	"sort"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/releaserecord"
)

// registryName is one name a subject published under.
type registryName struct{ registry, name string }

// addRetiredRegistryNames records the registry names of every subject whose
// release history is closed and that released from CI: a registry name,
// once held, is never dropped, and a retired subject's members are gone
// from the tree, so its names are read at the release commit of each of
// its releases, from the old layout as it stood there. The rule is the
// live releasables' (see addRegistryNames): a pipeline's npm or pypi
// package name, and the module path of a go library or local go pipeline,
// of a subject whose config said publish_mode "ci". A subject that
// released but whose archives record no release commit is refused, since
// nothing then states its names.
func (b *builder) addRetiredRegistryNames(rec *lifecycle.Record, seen map[string]bool, must func(error)) {
	for _, subject := range sortedKeys(b.history.closed) {
		if b.retired[subject] == "" {
			continue
		}
		released, readable := 0, 0
		for _, a := range b.plannedArchives(releaserecord.RetiredArchiveDir(subject)) {
			if !a.Fate.Released() {
				continue
			}
			released++
			if a.ReleaseCommit.Commit == "" {
				continue
			}
			readable++
			names, err := b.namesAtReleaseCommit(subject, a.ReleaseCommit.Commit)
			if err != nil {
				b.p.add("reading the registry names the retired subject %q published %s under, at its release commit %s: %v", subject, a.Version, a.ReleaseCommit.Commit, err)
				continue
			}
			for _, n := range names {
				k := n.registry + " " + n.name
				if seen[k] {
					continue
				}
				seen[k] = true
				must(rec.AddRegistryName(n.registry, n.name, subject, b.req.Now))
			}
		}
		if released > 0 && readable == 0 {
			b.p.add("the retired subject %q released, and a registry name, once held, is never dropped, but none of its archives under %s records a release commit to read the names it published under from. Restore a release commit to one of them with `rlsbl release backfill` (the Python rlsbl 0.131.0), then migrate", subject, b.retired[subject]+"/releases")
		}
	}
}

// namesAtReleaseCommit is what subject published under at commit, read
// from the old layout there: a workspace's members versioned under it, or
// a standalone project's root.
func (b *builder) namesAtReleaseCommit(subject, commit string) ([]registryName, error) {
	type member struct {
		path   string
		config map[string]any
	}
	var members []member
	wsText, isWorkspace, err := b.repo.FileAt(commit, oldWorkspaceFile)
	if err != nil {
		return nil, err
	}
	if isWorkspace {
		doc, err := tomledit.Unmarshal[map[string]any]([]byte(wsText))
		if err != nil {
			return nil, fmt.Errorf("%s: %w", oldWorkspaceFile, err)
		}
		relCfg, err := b.jsonAt(commit, oldWorkspaceDir+"/releasables/"+subject+"/"+oldConfigName)
		if err != nil {
			return nil, err
		}
		projects, _ := (*doc)["projects"].([]any)
		for _, p := range projects {
			po, _ := p.(map[string]any)
			if r, _ := po["releasable"].(string); r != subject {
				continue
			}
			dir, _ := po["path"].(string)
			own, err := b.jsonAt(commit, joinRel(dir, oldDir, oldConfigName))
			if err != nil {
				return nil, err
			}
			merged := mergeConfig(relCfg, own)
			if targets, ok := relCfg["targets"]; ok {
				merged["targets"] = targets
			}
			members = append(members, member{path: joinRel(dir), config: merged})
		}
	} else {
		cfg, err := b.jsonAt(commit, oldDir+"/"+oldConfigName)
		if err != nil {
			return nil, err
		}
		members = append(members, member{path: declarations.RootPath, config: cfg})
	}
	var out []registryName
	for _, m := range members {
		if mode, _ := m.config["publish_mode"].(string); mode != string(declarations.PublishCI) {
			continue
		}
		pipelines, _ := m.config["pipelines"].(map[string]any)
		names := make([]string, 0, len(pipelines))
		for name := range pipelines {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			p, _ := pipelines[name].(map[string]any)
			typ, _ := p["type"].(string)
			target, _ := p["target"].(string)
			dir := joinRel(m.path, targetPath(m.config, target))
			var n string
			switch typ {
			case declarations.TargetNPM:
				pkg, err := b.jsonAt(commit, joinRel(dir, "package.json"))
				if err != nil {
					return nil, err
				}
				n, _ = pkg["name"].(string)
			case declarations.TargetPyPI:
				text, found, err := b.repo.FileAt(commit, joinRel(dir, "pyproject.toml"))
				if err != nil || !found {
					if err != nil {
						return nil, err
					}
					continue
				}
				doc, err := tomledit.Unmarshal[map[string]any]([]byte(text))
				if err != nil {
					return nil, fmt.Errorf("%s: %w", joinRel(dir, "pyproject.toml"), err)
				}
				project, _ := (*doc)["project"].(map[string]any)
				n, _ = project["name"].(string)
			case declarations.TargetGo:
				artifact, _ := p["artifact"].(string)
				local, _ := p["local"].(bool)
				if artifact != declarations.ArtifactLibrary && !local {
					continue
				}
				rel := joinRel(dir, gomodule.FileName)
				text, found, err := b.repo.FileAt(commit, rel)
				if err != nil || !found {
					if err != nil {
						return nil, err
					}
					continue
				}
				f, err := gomodule.Parse(rel, []byte(text))
				if err != nil {
					return nil, err
				}
				if f.Module != nil {
					n = f.Module.Mod.Path
				}
			}
			if n != "" {
				out = append(out, registryName{registry: typ, name: n})
			}
		}
	}
	return out, nil
}

// targetPath is the path the config's targets list gives the target name,
// relative to its member; "" when it gives none.
func targetPath(cfg map[string]any, name string) string {
	targets, _ := cfg["targets"].([]any)
	for _, t := range targets {
		if o, ok := t.(map[string]any); ok && o["name"] == name {
			p, _ := o["path"].(string)
			return path.Clean(p)
		}
	}
	return ""
}

// jsonAt is the JSON object at rel in commit, empty when commit does not
// hold rel.
func (b *builder) jsonAt(commit, rel string) (map[string]any, error) {
	text, found, err := b.repo.FileAt(commit, rel)
	if err != nil || !found {
		return map[string]any{}, err
	}
	out := map[string]any{}
	if err := json.Unmarshal([]byte(text), &out); err != nil {
		return nil, fmt.Errorf("%s: %w", rel, err)
	}
	return out, nil
}
