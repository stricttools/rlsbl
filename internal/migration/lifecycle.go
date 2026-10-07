package migration

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// ReadLicenses reads a --licenses file: a TOML document mapping each
// releasable name to an SPDX identifier or "proprietary".
func ReadLicenses(file string) (map[string]string, error) {
	data, err := os.ReadFile(file)
	if err != nil {
		return nil, fmt.Errorf("reading the licenses file: %w", err)
	}
	doc, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return nil, fmt.Errorf("the licenses file %s is not valid TOML: %w", file, err)
	}
	out := map[string]string{}
	var problems []string
	for _, name := range sortedKeys(*doc) {
		v, ok := (*doc)[name].(string)
		if !ok || strings.TrimSpace(v) == "" || strings.TrimSpace(v) != v {
			problems = append(problems, fmt.Sprintf("%s = %s: a license is an SPDX identifier or \"proprietary\", written as a string without surrounding whitespace", name, describeValue((*doc)[name])))
			continue
		}
		out[name] = v
	}
	if len(problems) > 0 {
		return nil, fmt.Errorf("the licenses file %s is refused:\n  %s", file, strings.Join(problems, "\n  "))
	}
	return out, nil
}

// recordWriter captures what the lifecycle library writes, so the record
// joins the plan instead of reaching the disk.
type recordWriter struct {
	root   string
	writes map[string][]byte
}

func (w *recordWriter) WriteFile(p string, data []byte) error {
	rel, err := filepath.Rel(w.root, p)
	if err != nil {
		return err
	}
	w.writes[filepath.ToSlash(rel)] = append([]byte(nil), data...)
	return nil
}

func (w *recordWriter) MkdirAll(string) error { return nil }

// licenseOf decides each releasable's license: the --licenses entry, or the
// one license its members' npm and pypi manifests agree on. A license is
// never read from a LICENSE file.
func (b *builder) licenseOf(d *declarations.Releasables) map[string]string {
	licenses := map[string]string{}
	for name := range b.req.Licenses {
		if _, ok := d.Releasable(name); !ok {
			b.p.add("the licenses file %s names %q, which is no releasable of this repository (the releasables are %s); correct the file", b.req.LicensesFile, name, strings.Join(releasableNames(d), ", "))
		}
	}
	var missing []string
	for _, r := range d.Releasables {
		found := map[string][]string{}
		for _, m := range d.MembersOf(r.Name) {
			ts, err := targets.MemberTargets(b.root, m)
			if err != nil {
				b.p.add("%v", err)
				continue
			}
			for _, t := range ts {
				target, err := targets.Get(t.Name)
				if err != nil {
					continue
				}
				dir := joinRel(m.Path, t.Path)
				meta, err := target.ReadMetadata(filepath.Join(b.root, filepath.FromSlash(dir)))
				if err != nil {
					b.p.add("reading the license of %s's %s manifest: %v", m.Name, t.Name, err)
					continue
				}
				if meta.License != "" && meta.License != "UNLICENSED" {
					found[meta.License] = append(found[meta.License], fmt.Sprintf("%s manifest in %s", t.Name, dir))
				}
			}
		}
		declared, isDeclared := b.req.Licenses[r.Name]
		switch {
		case isDeclared:
			if declared != lifecycle.ProprietaryLicense {
				for license, where := range found {
					if license != declared {
						b.p.add("the licenses file %s declares %s for %q, but its %s says %s; make them agree", b.req.LicensesFile, declared, r.Name, strings.Join(where, " and "), license)
					}
				}
			}
			licenses[r.Name] = declared
		case len(found) == 1:
			for license := range found {
				licenses[r.Name] = license
			}
		case len(found) > 1:
			var parts []string
			for _, license := range sortedKeys(found) {
				parts = append(parts, fmt.Sprintf("%s (%s)", license, strings.Join(found[license], ", ")))
			}
			b.p.add("the manifests of %q disagree on its license: %s. Declare it in a --licenses file, and make the manifests agree", r.Name, strings.Join(parts, "; "))
		default:
			missing = append(missing, r.Name)
		}
	}
	if len(missing) > 0 {
		b.p.add("no license can be read from the manifests of %s (a license is never inferred from a LICENSE file). Pass --licenses <file>, a TOML file mapping each to an SPDX identifier or \"proprietary\", for example: %s = \"MIT\"", strings.Join(missing, ", "), missing[0])
	}
	return licenses
}

func releasableNames(d *declarations.Releasables) []string {
	var out []string
	for _, r := range d.Releasables {
		out = append(out, r.Name)
	}
	return out
}

// githubRepository is the repository GitHub knows this one as, and its
// origin as the confidential-name index keys it.
func (b *builder) githubRepository(d *declarations.Releasables) (github.Repository, string, error) {
	origin := ""
	configured, err := b.repo.RemoteConfigured("origin")
	if err != nil {
		return github.Repository{}, "", err
	}
	if configured {
		if origin, err = b.repo.RemoteURL("origin"); err != nil {
			return github.Repository{}, "", err
		}
	}
	repo, err := github.ResolveRepository(d.GitHubRepository, origin)
	if err != nil {
		return github.Repository{}, "", fmt.Errorf("the migration reads GitHub's visibility of the repository, and cannot name it: %w. Add the origin remote, then migrate", err)
	}
	if origin == "" {
		origin = "https://github.com/" + repo.String()
	}
	return repo, origin, nil
}

// convertLifecycle composes the lifecycle-and-license record: one active
// lifecycle period and one license period per releasable from the migration
// date, a retired period per closed release history, the releasable-name
// identities with their renames, the identity transitions, the registry
// names, and the unversioned tags. It also decides the confidential-name
// index entry.
func (b *builder) convertLifecycle(d *declarations.Releasables) {
	if ok, err := exists(b.root, lifecycle.RecordFile); err != nil {
		b.p.add("%v", err)
		return
	} else if ok {
		b.p.add("%s already exists, and the migration writes the record whole from the old layout; delete it through saferm (a dry run lists what replaces it), then migrate", lifecycle.RecordFile)
		return
	}
	licenses := b.licenseOf(d)
	repo, origin, err := b.githubRepository(d)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	gh, err := github.New(b.e)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	visibility, askErr := publishrules.GitHubVisibility(gh, repo).Visibility()
	if askErr != nil {
		b.p.add("the migration reads GitHub's visibility of %s and could not: %v", repo, askErr)
		return
	}
	proprietary := false
	for _, l := range licenses {
		if l == lifecycle.ProprietaryLicense {
			proprietary = true
		}
	}
	if visibility == lifecycle.VisibilityPrivate && !proprietary {
		b.p.add("GitHub reports %s private, and no releasable of it is declared proprietary: a repository is confidential only through a proprietary license period, and a private repository without one is not designed for. Declare the proprietary releasables in a --licenses file (for example %s = \"proprietary\"), or make the repository public, then migrate", repo, d.Releasables[0].Name)
		return
	}
	if len(licenses) != len(d.Releasables) {
		return
	}
	now := b.req.Now
	rec := &lifecycle.Record{}
	must := func(err error) {
		if err != nil {
			b.p.add("composing %s: %v", lifecycle.RecordFile, err)
		}
	}
	reasonActive := "active when rlsbl's records moved to the .strictmetadata layout"
	for _, r := range d.Releasables {
		must(rec.OpenPeriod(lifecycle.TableLifecycle, r.Name, string(lifecycle.StatusActive), now, reasonActive))
		reason := "read from the package manifests when rlsbl's records moved to the .strictmetadata layout"
		if _, ok := b.req.Licenses[r.Name]; ok {
			reason = "declared by the owner when rlsbl's records moved to the .strictmetadata layout"
		}
		must(rec.OpenPeriod(lifecycle.TableLicenses, r.Name, licenses[r.Name], now, reason))
	}
	for _, subject := range sortedKeys(b.history.closed) {
		reason := b.history.closedReason[subject]
		if strings.TrimSpace(reason) == "" {
			reason = "release history closed"
		}
		must(rec.OpenPeriod(lifecycle.TableLifecycle, subject, string(lifecycle.StatusRetired), b.history.closedAt[subject], reason))
	}
	b.addIdentities(rec, d, must)
	b.addRegistryNames(rec, d, must)
	for _, t := range b.history.unversioned {
		must(rec.AddUnversionedTag(t.tag, t.reason, t.at))
	}
	if len(b.p.list) > 0 {
		return
	}
	var declared []string
	for _, r := range d.Releasables {
		declared = append(declared, r.Name)
	}
	for _, m := range d.Members {
		declared = append(declared, m.Name)
	}
	if err := rec.Validate(now, declared); err != nil {
		b.p.add("the composed lifecycle-and-license record is refused by its own validation: %v", err)
		return
	}
	w := &recordWriter{root: b.root, writes: map[string][]byte{}}
	if err := rec.Write(w, b.root); err != nil {
		b.p.add("composing %s: %v", lifecycle.RecordFile, err)
		return
	}
	for _, rel := range sortedKeys(w.writes) {
		change := "the lifecycle-and-license record"
		if rel == lifecycle.ManifestFile {
			change = "the directory's ownership manifest"
		}
		b.addWrite(write{path: rel, sources: b.history.sources, change: change, data: w.writes[rel]})
	}
	names, err := rec.ConfidentialNames(now, repo.Name)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	if len(names) == 0 {
		b.plan.index = &indexEntry{origin: origin, remove: true}
	} else {
		b.plan.index = &indexEntry{origin: origin, names: names}
	}
}

// firstCommitDate is the committer date of the oldest commit HEAD reaches,
// the earliest any identity of the repository can have begun.
func (b *builder) firstCommitDate() (time.Time, error) {
	commits, err := b.repo.Commits([]string{"HEAD"}, nil)
	if err != nil {
		return time.Time{}, err
	}
	if len(commits) == 0 {
		return time.Time{}, errors.New("HEAD reaches no commit")
	}
	var first time.Time
	for _, c := range []string{commits[0], commits[len(commits)-1]} {
		at, err := b.repo.CommitterDate(c)
		if err != nil {
			return time.Time{}, err
		}
		if first.IsZero() || at.Before(first) {
			first = at
		}
	}
	return first, nil
}

// tagGlob is the tag namespace of releasable r spelled with name.
func tagGlob(r declarations.Releasable, name string) []string {
	scheme, err := workspace.SchemeOf(declarations.Releasable{Name: name, TagFormat: r.TagFormat})
	if err != nil {
		return []string{}
	}
	return []string{scheme.ListGlob()}
}

// addIdentities records each releasable's releasable-name identities (its
// renames chained in the order recorded) and the identity transitions.
func (b *builder) addIdentities(rec *lifecycle.Record, d *declarations.Releasables, must func(error)) {
	first, err := b.firstCommitDate()
	if err != nil {
		b.p.add("reading the repository's first commit, the start of its identities: %v", err)
		return
	}
	reason := "recorded when rlsbl's records moved to the .strictmetadata layout"
	renamedTo := map[string]rename{}
	for _, r := range b.history.renames {
		if _, ok := renamedTo[r.to]; ok {
			b.p.add("the transition record renames two releasables to %q; delete the wrong line by hand", r.to)
			return
		}
		renamedTo[r.to] = r
	}
	for _, r := range d.Releasables {
		var chain []rename
		for name, seen := r.Name, map[string]bool{}; ; {
			prev, ok := renamedTo[name]
			if !ok || seen[name] {
				break
			}
			seen[name] = true
			chain = append([]rename{prev}, chain...)
			name = prev.from
		}
		from := first
		for _, step := range chain {
			why := step.reason
			if strings.TrimSpace(why) == "" {
				why = reason
			}
			must(rec.AddIdentity(lifecycle.Identity{Subject: r.Name, Facet: lifecycle.FacetReleasableName, Value: step.from, TagPatterns: tagGlob(r, step.from), Period: lifecycle.Period{From: from, Until: step.at}, Reason: why}))
			from = step.at
		}
		must(rec.AddIdentity(lifecycle.Identity{Subject: r.Name, Facet: lifecycle.FacetReleasableName, Value: r.Name, TagPatterns: tagGlob(r, r.Name), Period: lifecycle.Period{From: from}, Reason: reason}))
	}
	b.addIdentityTransitions(rec, d, first, must)
}

// addIdentityTransitions converts each identity transition: released, the
// old identity ends and the new one starts on the committer date of the
// release commit of its effective version; not yet released, the new one is
// pending on that version.
func (b *builder) addIdentityTransitions(rec *lifecycle.Record, d *declarations.Releasables, first time.Time, must func(error)) {
	type key struct{ subject, facet string }
	groups := map[key][]identityChange{}
	var order []key
	for _, c := range b.history.identities {
		k := key{c.subject, c.facet}
		if _, ok := groups[k]; !ok {
			order = append(order, k)
		}
		groups[k] = append(groups[k], c)
	}
	for _, k := range order {
		changes := groups[k]
		sort.SliceStable(changes, func(i, j int) bool { return changes[i].at.Before(changes[j].at) })
		facet := lifecycle.Facet(k.facet)
		if facet == lifecycle.FacetReleasableName {
			b.p.add("%s records a releasable-name identity transition; a releasable's name changes through a rename. Hand edit: replace it with a releasable-rename line, then migrate", changes[0].where)
			continue
		}
		r, declared := d.Releasable(k.subject)
		retired := b.history.closed[k.subject]
		if !declared && !retired {
			b.p.add("%s records an identity transition of %q, which is neither a releasable nor a retired subject", changes[0].where, k.subject)
			continue
		}
		registry := ""
		if facet == lifecycle.FacetGoModulePath {
			registry = declarations.TargetGo
		}
		patterns := []string{}
		if declared {
			patterns = tagGlob(r, r.Name)
		}
		value, from := changes[0].from, first
		for i, c := range changes {
			if c.from != value {
				b.p.add("%s changes %s from %q, but the identity before it was %q; repair the record by hand", c.where, k.facet, c.from, value)
				break
			}
			released, at, err := b.releasedOn(k.subject, retired, c.effectiveVersion)
			if err != nil {
				b.p.add("%s: %v", c.where, err)
				break
			}
			if !released {
				if i != len(changes)-1 {
					b.p.add("%s is not released yet, and a later transition of the same identity follows it; delete one by hand", c.where)
					break
				}
				must(rec.AddIdentity(lifecycle.Identity{Subject: k.subject, Facet: facet, Value: value, Registry: registry, TagPatterns: patterns, Period: lifecycle.Period{From: from}, Reason: "recorded when rlsbl's records moved to the .strictmetadata layout"}))
				must(rec.AddPendingIdentity(lifecycle.Identity{Subject: k.subject, Facet: facet, Value: c.to, Registry: registry, TagPatterns: patterns, EffectiveVersion: c.effectiveVersion, Reason: "the identity the release of " + c.effectiveVersion + " takes"}))
				value = ""
				break
			}
			must(rec.AddIdentity(lifecycle.Identity{Subject: k.subject, Facet: facet, Value: value, Registry: registry, TagPatterns: patterns, Period: lifecycle.Period{From: from, Until: at}, Reason: "recorded when rlsbl's records moved to the .strictmetadata layout"}))
			value, from = c.to, at
		}
		if value != "" {
			must(rec.AddIdentity(lifecycle.Identity{Subject: k.subject, Facet: facet, Value: value, Registry: registry, TagPatterns: patterns, Period: lifecycle.Period{From: from}, Reason: "recorded when rlsbl's records moved to the .strictmetadata layout"}))
		}
	}
}

// releasedOn reports whether the converted record holds a release of
// version, and the committer date of its release commit.
func (b *builder) releasedOn(subject string, retired bool, version string) (bool, time.Time, error) {
	v, err := semver.Parse(version)
	if err != nil {
		return false, time.Time{}, fmt.Errorf("the effective version %q: %w", version, err)
	}
	a, ok := b.plannedArchive(archiveDirectory(subject, retired), v)
	if !ok || !a.Fate.Released() {
		return false, time.Time{}, nil
	}
	if a.Fate != releaserecord.FateRecorded {
		return false, time.Time{}, fmt.Errorf("the release of %s is recorded unrecoverable, so the day its identity changed cannot be read from its release commit; restore the release commit with `rlsbl release backfill` (the Python rlsbl 0.131.0), then migrate", version)
	}
	at, err := b.repo.CommitterDate(a.ReleaseCommit.Commit)
	if err != nil {
		return false, time.Time{}, fmt.Errorf("reading the release commit of %s: %w", version, err)
	}
	return true, at, nil
}

// addRegistryNames records the registry names of every releasable that
// publishes from CI and has released at least once.
func (b *builder) addRegistryNames(rec *lifecycle.Record, d *declarations.Releasables, must func(error)) {
	seen := map[string]bool{}
	for _, r := range d.Releasables {
		if r.PublishMode != declarations.PublishCI {
			continue
		}
		released := false
		for _, a := range b.plannedArchives(releaserecord.ArchiveDir(r.Name)) {
			if a.Fate.Released() {
				released = true
			}
		}
		if !released {
			continue
		}
		for _, m := range d.MembersOf(r.Name) {
			for _, p := range m.Pipelines {
				dir := m.Path
				for _, t := range m.Targets {
					if t.Name == p.Target {
						dir = joinRel(m.Path, t.Path)
					}
				}
				abs := filepath.Join(b.root, filepath.FromSlash(dir))
				var name string
				switch p.Type {
				case declarations.TargetNPM, declarations.TargetPyPI:
					target, err := targets.Get(p.Type)
					if err != nil {
						continue
					}
					n, found, err := target.ReadName(abs)
					if err != nil {
						b.p.add("reading the package name the %s pipeline %q of %s publishes: %v", p.Type, p.Name, m.Name, err)
						continue
					}
					if !found {
						continue
					}
					name = n
				case declarations.TargetGo:
					if p.Artifact != declarations.ArtifactLibrary && !p.Local {
						continue
					}
					modulePath, found, err := gomodule.ModulePath(abs)
					if err != nil {
						b.p.add("reading the module path the go pipeline %q of %s publishes: %v", p.Name, m.Name, err)
						continue
					}
					if !found {
						continue
					}
					name = modulePath
				}
				k := p.Type + " " + name
				if name == "" || seen[k] {
					continue
				}
				seen[k] = true
				must(rec.AddRegistryName(p.Type, name, r.Name, b.req.Now))
			}
		}
	}
}
