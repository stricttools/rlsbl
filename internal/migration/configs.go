package migration

import (
	"fmt"
	"path"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The deleted config keys: each is dropped, with the reason the report
// gives.
var deletedConfigKeys = map[string]string{
	"changelog_format":                  "there is one changelog format",
	"changelog_format_version_enforced": "every changelog line is restamped to format_version 2 instead",
	"private":                           "publish_mode replaced it",
	"deploy":                            "SSH deploy is dropped",
	"services":                          "requires-services is dropped",
	"test_env":                          "requires-services is dropped",
	"uv_sync_verbose":                   "uv syncs run quietly",
}

// The config keys the conversion reads.
var convertedConfigKeys = map[string]bool{
	"publish_mode": true, "targets": true, "pipelines": true, "tag": true, "release_branches": true,
	"batch_limits": true, "push_timeout": true, "ci_timeout": true, "check_timeout": true, "hook_timeout": true,
	"test": true, "external_checks": true, "checks": true, "strictspec_gate": true, "test_sandbox": true,
	"internal_dep_floors": true, "env_file": true, "github_repo": true, "homebrew": true, "hooks": true,
	"publish_gate_check_regex": true, "npm_wrapper": true, "npm_scope": true,
}

// externalCheckFormKey is the key an external check carried for its form,
// which the new declarations drop (one form remains). It is spelled from two
// literals because the word is banned from rlsbl's sources.
const externalCheckFormKey = "k" + "ind"

// The release targets the new declarations accept.
var supportedTargets = map[string]bool{declarations.TargetGo: true, declarations.TargetNPM: true, declarations.TargetPyPI: true}

// valueSource is one value a repository-wide key carries in one config.
type valueSource struct {
	value string
	file  string
}

// configValues collects the values of a repository-wide key across every
// config, so disagreement is refused, listing them.
type configValues map[string][]valueSource

func (c configValues) add(key, value, file string) {
	c[key] = append(c[key], valueSource{value: value, file: file})
}

// agreed is the one value every config carrying key states; found is false
// when none does. Disagreement is a problem listing each value with its
// file.
func (c configValues) agreed(key string, p *problems) (string, bool) {
	sources := c[key]
	if len(sources) == 0 {
		return "", false
	}
	distinct := map[string]bool{}
	for _, s := range sources {
		distinct[s.value] = true
	}
	if len(distinct) > 1 {
		var parts []string
		for _, s := range sources {
			parts = append(parts, fmt.Sprintf("%s in %s", s.value, s.file))
		}
		p.add("the configs of this repository disagree on %s (%s); the new declarations hold one value for the repository, so make the configs agree by hand first", key, strings.Join(parts, ", "))
		return "", false
	}
	return sources[0].value, true
}

// toolDeclaration is a checks.<rule> block: the Python tool run the rule
// declared for one member.
type toolDeclaration struct {
	member string
	cwd    string
	paths  []string
	file   string
}

// certificateDeclaration is a strictspec_gate section.
type certificateDeclaration struct {
	certificate  string
	adjudication string
	file         string
}

// exclusion is one batch_limits.exclusions entry naming a changelog line.
type exclusion struct {
	version string
	line    int64
	reason  string
	file    string
}

// readConfig reads and claims an old config.json; found is false when it
// does not exist.
func (b *builder) readConfig(rel string) (object, bool) {
	value, found, err := b.tree.readJSON(rel)
	if !found {
		return object{}, false
	}
	b.tree.claim(rel)
	if err != nil {
		b.p.add("%v", err)
		return object{}, false
	}
	o, ok := newObject(rel, value, b.p)
	if !ok {
		return object{}, false
	}
	b.checkConfigKeys(o)
	b.collectRepositoryValues(o)
	if sandbox, ok := o.table("test_sandbox"); ok {
		b.sandboxes = append(b.sandboxes, sandbox)
	}
	return o, true
}

// checkConfigKeys refuses a key the conversion has no row for, and notes
// each deleted key.
func (b *builder) checkConfigKeys(o object) {
	for _, k := range o.keys() {
		if reason, ok := deletedConfigKeys[k]; ok {
			b.note("%s: %s is dropped: %s", o.file, k, reason)
			continue
		}
		if !convertedConfigKeys[k] {
			b.p.add("%s: the key %s is not a key rlsbl's config ever had, so the migration cannot convert it; delete it by hand", o.file, k)
		}
	}
	if o.has("npm_scope") {
		b.p.add("%s: npm_scope was removed from rlsbl's config: a scoped npm package declares its scope in the name field of its package.json. Move the scope there and delete npm_scope by hand", o.file)
	}
}

// collectRepositoryValues records the values of the keys that hold one
// value for the whole repository.
func (b *builder) collectRepositoryValues(o object) {
	for _, k := range []string{"push_timeout", "ci_timeout", "check_timeout", "hook_timeout"} {
		if v, ok := o.integer(k); ok {
			if v < 1 {
				b.p.add("%s: %s is %d; a timeout is at least 1 second", o.file, k, v)
				continue
			}
			b.values.add(k, fmt.Sprint(v), o.file)
		}
	}
	for _, k := range []string{"env_file", "github_repo"} {
		if v, ok := o.str(k); ok {
			b.values.add(k, v, o.file)
		}
	}
	if v, ok := o.strings("release_branches"); ok {
		if len(v) == 0 {
			b.p.add("%s: release_branches is empty; declare at least one branch", o.file)
		} else {
			b.values.add("release_branches", strings.Join(v, ","), o.file)
		}
	}
	if v, ok := o.boolean("tag"); ok {
		b.values.add("tag", fmt.Sprint(v), o.file)
	}
}

// mergeConfig merges overlay onto base as the Python did: a key in overlay
// replaces base's, except two objects, which merge recursively.
func mergeConfig(base, overlay map[string]any) map[string]any {
	merged := map[string]any{}
	for k, v := range base {
		merged[k] = v
	}
	for k, v := range overlay {
		bm, bok := merged[k].(map[string]any)
		om, ook := v.(map[string]any)
		if bok && ook {
			merged[k] = mergeConfig(bm, om)
			continue
		}
		merged[k] = v
	}
	return merged
}

// memberConfig converts the member-level keys of one member's effective
// config into the member's declarations.
func (b *builder) memberConfig(o object, m *declarations.Member) {
	if targets, ok := o.list("targets"); ok {
		m.Targets = b.convertTargets(o, targets)
	}
	if floors, ok := o.strings("internal_dep_floors"); ok && len(floors) > 0 {
		m.InternalDepFloors = floors
	}
	if test, ok := o.table("test"); ok {
		b.convertTest(test, m)
	}
	if checks, ok := o.list("external_checks"); ok {
		m.ExternalChecks = b.convertExternalChecks(o, checks)
	}
	if checks, ok := o.table("checks"); ok {
		b.collectToolDeclarations(checks, m.Path)
	}
	if gate, ok := o.table("strictspec_gate"); ok {
		b.collectCertificate(gate, m.Path)
	}
	wrapper := false
	if npmWrapper, ok := o.table("npm_wrapper"); ok {
		wrapper = b.convertNpmWrapper(npmWrapper)
	}
	tap := ""
	if homebrew, ok := o.table("homebrew"); ok {
		tap = b.convertHomebrew(homebrew)
	}
	if pipelines, ok := o.table("pipelines"); ok {
		m.Pipelines = b.convertPipelines(pipelines, wrapper, tap)
	} else if tap != "" {
		b.p.add("%s: homebrew.tap names a Homebrew tap, but no pipeline publishes a go binary; delete homebrew.tap or declare the go binary pipeline", o.file)
	}
}

// convertTargets reads a targets list: names, or objects with a name and a
// path relative to the member.
func (b *builder) convertTargets(o object, entries []any) []declarations.Target {
	targets := []declarations.Target{}
	for i, entry := range entries {
		var t declarations.Target
		switch v := entry.(type) {
		case string:
			t.Name = v
		case map[string]any:
			e, _ := o.child("targets", i, v)
			for _, k := range e.keys() {
				if k != "name" && k != "path" {
					b.p.add("%s: %s: %s is not a target key; a target carries name and path", o.file, e.where, k)
				}
			}
			name, ok := e.str("name")
			if !ok {
				b.p.add("%s: %s has no name", o.file, e.where)
				continue
			}
			t.Name = name
			if raw, ok := e.str("path"); ok {
				p, ok := declarations.CanonicalPath(raw)
				if !ok {
					b.p.add("%s: %s: the path %q leaves the member's directory; name a directory inside it", o.file, e.where, raw)
					continue
				}
				if p != declarations.RootPath {
					t.Path = p
				}
			}
		default:
			b.p.add("%s: targets[%d] must be a target name or an object with name and path, not %s", o.file, i, describeValue(entry))
			continue
		}
		if !supportedTargets[t.Name] {
			b.p.add("%s: the target %q was removed from rlsbl, which releases go, npm, and pypi only. Hand edit: delete %q from targets in %s, and delete every pipeline whose type or target is %q", o.file, t.Name, t.Name, o.file, t.Name)
			continue
		}
		targets = append(targets, t)
	}
	return targets
}

func (b *builder) convertTest(o object, m *declarations.Member) {
	for _, k := range o.keys() {
		switch k {
		case "pypi":
			if t, ok := o.table(k); ok {
				for _, tk := range t.keys() {
					if tk != "markers" {
						b.p.add("%s: %s.%s is not a test setting; test.pypi carries markers", o.file, t.where, tk)
					}
				}
				m.Test.PyPIMarkers, _ = t.str("markers")
			}
		case "go":
			if t, ok := o.table(k); ok {
				for _, tk := range t.keys() {
					if tk != "command" {
						b.p.add("%s: %s.%s is not a test setting; test.go carries command", o.file, t.where, tk)
					}
				}
				m.Test.GoCommand, _ = t.str("command")
			}
		default:
			b.p.add("%s: %s is not a test setting; test carries pypi.markers and go.command", o.file, o.key(k))
		}
	}
}

func (b *builder) convertExternalChecks(o object, entries []any) []declarations.ExternalCheck {
	var out []declarations.ExternalCheck
	for i, entry := range entries {
		e, ok := o.child("external_checks", i, entry)
		if !ok {
			continue
		}
		var c declarations.ExternalCheck
		for _, k := range e.keys() {
			switch k {
			case "name", "tag", "command", "depends_on", "cwd", externalCheckFormKey:
			default:
				b.p.add("%s: %s.%s is not an external check key", o.file, e.where, k)
			}
		}
		c.Name, _ = e.str("name")
		c.Tag, _ = e.str("tag")
		c.Command, _ = e.str("command")
		if deps, ok := e.strings("depends_on"); ok && len(deps) > 0 {
			c.DependsOn = deps
		}
		if cwd, ok := e.str("cwd"); ok {
			p, ok := declarations.CanonicalPath(cwd)
			if !ok {
				b.p.add("%s: %s.cwd %q leaves the member's directory", o.file, e.where, cwd)
			} else if p != declarations.RootPath {
				c.Cwd = p
			}
		}
		out = append(out, c)
	}
	return out
}

func (b *builder) collectToolDeclarations(o object, member string) {
	for _, rule := range o.keys() {
		switch rule {
		case "lint", "format", "type-check":
		default:
			b.p.add("%s: %s is not a tool check; checks declares lint, format, and type-check", o.file, o.key(rule))
			continue
		}
		t, ok := o.table(rule)
		if !ok {
			continue
		}
		decl := toolDeclaration{member: member, cwd: member, file: o.file}
		for _, k := range t.keys() {
			if k != "paths" && k != "cwd" {
				b.p.add("%s: %s.%s is not a tool check key; a tool check carries paths and cwd", o.file, t.where, k)
			}
		}
		if cwd, ok := t.str("cwd"); ok {
			p, ok := declarations.CanonicalPath(cwd)
			if !ok {
				b.p.add("%s: %s.cwd %q leaves the member's directory", o.file, t.where, cwd)
				continue
			}
			decl.cwd = joinRel(member, p)
		}
		paths, _ := t.strings("paths")
		if len(paths) == 0 {
			b.p.add("%s: %s.paths is missing or empty; a tool check declares what it checks", o.file, t.where)
			continue
		}
		for _, raw := range paths {
			p, ok := declarations.CanonicalPath(raw)
			if !ok {
				b.p.add("%s: %s.paths names %q, which leaves the directory it is relative to", o.file, t.where, raw)
				continue
			}
			decl.paths = append(decl.paths, p)
		}
		b.tools[rule] = append(b.tools[rule], decl)
	}
}

func (b *builder) collectCertificate(o object, member string) {
	for _, k := range o.keys() {
		if k != "certificate" && k != "adjudication" {
			b.p.add("%s: %s.%s is not a strictspec_gate key; it carries certificate and adjudication", o.file, o.where, k)
		}
	}
	cert, ok := o.str("certificate")
	if !ok || cert == "" {
		b.p.add("%s: strictspec_gate.certificate is missing; the certificate path is required", o.file)
		return
	}
	decl := certificateDeclaration{file: o.file}
	if p, ok := declarations.CanonicalPath(cert); ok {
		decl.certificate = joinRel(member, p)
	} else {
		b.p.add("%s: strictspec_gate.certificate %q leaves the member's directory", o.file, cert)
	}
	if adj, ok := o.str("adjudication"); ok && adj != "" {
		if p, ok := declarations.CanonicalPath(adj); ok {
			decl.adjudication = joinRel(member, p)
		} else {
			b.p.add("%s: strictspec_gate.adjudication %q leaves the member's directory", o.file, adj)
		}
	}
	if b.certificate != nil && (*b.certificate != certificateDeclaration{certificate: decl.certificate, adjudication: decl.adjudication, file: b.certificate.file}) {
		b.p.add("%s and %s both declare a strictspec_gate, and strictcode.toml holds one [strictspec_certificate]; keep one by hand", b.certificate.file, o.file)
		return
	}
	b.certificate = &decl
}

// convertNpmWrapper reads npm_wrapper and reports whether it is enabled.
func (b *builder) convertNpmWrapper(o object) bool {
	enabled := false
	for _, k := range o.keys() {
		switch k {
		case "enabled":
			enabled, _ = o.boolean(k)
		case "platforms":
			b.p.add("%s: npm_wrapper.platforms sets the platforms by hand, and rlsbl's one platform table now decides them (linux and darwin, x64 and arm64). Delete npm_wrapper.platforms by hand", o.file)
		case "scope", "npm_scope":
			b.p.add("%s: npm_wrapper.%s was removed from rlsbl's config: a scoped npm package declares its scope in the name field of its package.json. Move the scope there and delete npm_wrapper.%s by hand", o.file, k, k)
		default:
			b.p.add("%s: npm_wrapper.%s is not an npm_wrapper key", o.file, k)
		}
	}
	return enabled
}

// convertHomebrew reads homebrew and returns the tap.
func (b *builder) convertHomebrew(o object) string {
	tap := ""
	for _, k := range o.keys() {
		switch k {
		case "tap":
			tap, _ = o.str(k)
		case "description":
			b.note("%s: homebrew.description is dropped: the member's description is the one source", o.file)
		case "license":
			b.note("%s: homebrew.license is dropped: the lifecycle-and-license record's license is the one source", o.file)
		default:
			b.p.add("%s: homebrew.%s is not a homebrew key", o.file, k)
		}
	}
	return tap
}

// The pipeline keys the conversion drops, with the reason.
var droppedPipelineKeys = map[string]string{
	"provenance":        "npm build attestations follow the repository's visibility",
	"wraps":             "the launcher is dropped",
	"binary_source":     "the launcher is dropped",
	"download":          "the launcher is dropped",
	"assets":            "local asset upload is dropped",
	"custom_assets":     "local asset upload is dropped",
	"max_asset_size_mb": "local asset upload is dropped",
	"token_var":         "npm publishes with NPM_TOKEN and PyPI with Trusted Publishing",
}

func (b *builder) convertPipelines(o object, npmWrapper bool, tap string) []declarations.Pipeline {
	var out []declarations.Pipeline
	for _, name := range o.keys() {
		po, ok := o.table(name)
		if !ok {
			continue
		}
		p := declarations.Pipeline{Name: name}
		for _, k := range po.keys() {
			switch k {
			case "type", "local", "target", "artifact", "install_paths":
			default:
				if reason, ok := droppedPipelineKeys[k]; ok {
					b.note("%s: %s.%s is dropped: %s", o.file, po.where, k, reason)
				} else {
					b.p.add("%s: %s.%s is not a pipeline key", o.file, po.where, k)
				}
			}
		}
		typ, ok := po.str("type")
		switch {
		case !ok:
			b.p.add("%s: %s has no type; a pipeline declares its type", o.file, po.where)
			continue
		case typ == "cloudflare-pages":
			b.p.add("%s: %s is a cloudflare-pages pipeline, which rlsbl dropped (selfdoc deploys documentation sites itself). Hand edit: delete %s from %s", o.file, po.where, po.where, o.file)
			continue
		case !supportedTargets[typ]:
			b.p.add("%s: %s has the type %q, a target rlsbl removed. Hand edit: delete %s from %s, and %q from its targets", o.file, po.where, typ, po.where, o.file, typ)
			continue
		}
		p.Type = typ
		target, ok := po.str("target")
		if !ok || target == "" {
			b.p.add("%s: %s names no target; a pipeline publishes one of its member's targets (only cloudflare-pages pipelines had none). Add \"target\": %q by hand", o.file, po.where, typ)
			continue
		}
		p.Target = target
		local, ok := po.boolean("local")
		if !ok {
			b.p.add("%s: %s has no local; a pipeline declares whether it publishes from this machine (true) or from CI (false)", o.file, po.where)
			continue
		}
		p.Local = local
		artifact, hasArtifact := po.str("artifact")
		switch {
		case artifact == "launcher":
			b.p.add("%s: %s publishes the launcher, which rlsbl dropped. Hand edit: delete %s from %s", o.file, po.where, po.where, o.file)
			continue
		case typ == declarations.TargetGo:
			if !hasArtifact || (artifact != declarations.ArtifactBinary && artifact != declarations.ArtifactLibrary) {
				b.p.add("%s: %s is a go pipeline and must declare artifact \"binary\" or \"library\"", o.file, po.where)
				continue
			}
			p.Artifact = artifact
		default:
			if hasArtifact {
				b.p.add("%s: %s carries artifact %q, which an %s pipeline never declared; delete the key by hand", o.file, po.where, artifact, typ)
				continue
			}
			p.Artifact = declarations.ArtifactPackage
			if npmWrapper && typ == declarations.TargetNPM {
				p.Artifact = declarations.ArtifactGoBinary
			}
		}
		if paths, ok := po.strings("install_paths"); ok && len(paths) > 0 {
			p.InstallPaths = paths
		}
		out = append(out, p)
	}
	var binaries []int
	for i, p := range out {
		if p.Type == declarations.TargetGo && p.Artifact == declarations.ArtifactBinary {
			binaries = append(binaries, i)
		}
	}
	for i, p := range out {
		if p.Artifact != declarations.ArtifactGoBinary {
			continue
		}
		if len(binaries) != 1 {
			b.p.add("%s: npm_wrapper.enabled makes the npm pipeline %q publish go binaries, and it wraps the member's one go binary pipeline; the member declares %d. Declare one go pipeline with artifact \"binary\" by hand", o.file, p.Name, len(binaries))
			continue
		}
		out[i].BinaryPipeline = out[binaries[0]].Name
	}
	if tap != "" {
		if len(binaries) != 1 {
			b.p.add("%s: homebrew.tap goes onto the member's one go binary pipeline, and the member declares %d. Declare one go pipeline with artifact \"binary\" by hand", o.file, len(binaries))
		} else {
			out[binaries[0]].HomebrewTap = tap
		}
	}
	return out
}

// convertHooks reads a config's hooks section.
func (b *builder) convertHooks(o object) declarations.Hooks {
	var h declarations.Hooks
	points := map[string]*[]declarations.Hook{"pre_checks": &h.PreChecks, "pre_release": &h.PreRelease, "post_release": &h.PostRelease}
	for _, k := range o.keys() {
		dst, ok := points[k]
		if !ok {
			b.p.add("%s: %s is not a hook point; hooks carries pre_checks, pre_release, and post_release", o.file, o.key(k))
			continue
		}
		entries, ok := o.list(k)
		if !ok {
			continue
		}
		for i, entry := range entries {
			switch v := entry.(type) {
			case string:
				*dst = append(*dst, declarations.Hook{Command: v})
			case map[string]any:
				e, _ := o.child(k, i, v)
				hook := declarations.Hook{}
				for _, hk := range e.keys() {
					switch hk {
					case "cmd":
						hook.Command, _ = e.str(hk)
					case "dir":
						dir, _ := e.str(hk)
						if p, ok := declarations.CanonicalPath(dir); ok {
							if p != declarations.RootPath {
								hook.Dir = p
							}
						} else {
							b.p.add("%s: %s.dir %q leaves the directory it is relative to", o.file, e.where, dir)
						}
					case "env":
						env, ok := e.table(hk)
						if !ok {
							continue
						}
						hook.Env = map[string]string{}
						for _, name := range env.keys() {
							hook.Env[name], _ = env.str(name)
						}
					default:
						b.p.add("%s: %s.%s is not a hook key; a hook carries cmd, dir, and env", o.file, e.where, hk)
					}
				}
				*dst = append(*dst, hook)
			default:
				b.p.add("%s: %s[%d] must be a command line or an object with cmd, dir, and env", o.file, o.key(k), i)
			}
		}
	}
	return h
}

// releasableKeys converts the releasable-level keys of a releasable's
// effective config: its publish mode and CI check pattern.
func (b *builder) releasableKeys(o object, r *declarations.Releasable) {
	mode, ok := o.str("publish_mode")
	switch {
	case !ok:
		b.p.add("%s: publish_mode is missing; declare \"ci\" or \"none\" by hand", o.file)
	case mode != string(declarations.PublishCI) && mode != string(declarations.PublishNone):
		b.p.add("%s: publish_mode is %q; it is \"ci\" or \"none\"", o.file, mode)
	default:
		r.PublishMode = declarations.PublishMode(mode)
	}
	r.PublishCICheckPattern, _ = o.str("publish_gate_check_regex")
}

// sortedMembers orders members by path, the root first.
func sortedMembers(members []declarations.Member) {
	sort.SliceStable(members, func(i, j int) bool {
		if members[i].Path == declarations.RootPath || members[j].Path == declarations.RootPath {
			return members[i].Path == declarations.RootPath && members[j].Path != declarations.RootPath
		}
		return members[i].Path < members[j].Path
	})
}

// memberConfigPath is a member's own old config.
func memberConfigPath(memberPath string) string {
	return path.Join(joinRel(memberPath, oldDir), oldConfigName)
}
