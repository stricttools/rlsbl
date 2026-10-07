package scaffold

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/tagging"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// defaultNpmRegistry is where an npm package publishes unless its
// package.json names another registry in publishConfig.
const defaultNpmRegistry = "https://registry.npmjs.org"

// npmRegistry is the registry the package in dir publishes to.
func npmRegistry(dir string) (string, error) {
	path := filepath.Join(dir, "package.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("reading %s: %w", path, err)
	}
	var pkg struct {
		PublishConfig struct {
			Registry string `json:"registry"`
		} `json:"publishConfig"`
	}
	if err := json.Unmarshal(data, &pkg); err != nil {
		return "", fmt.Errorf("%s: %w", path, err)
	}
	if pkg.PublishConfig.Registry != "" {
		return pkg.PublishConfig.Registry, nil
	}
	return defaultNpmRegistry, nil
}

// fileMerge is a setting merged into a file the project owns, never
// recorded: content is nil when the file stays as it is.
type fileMerge struct {
	path    string
	content []byte
	status  string
}

// projectFileMerges merges into each Python target's pyproject.toml what
// scaffold keeps there: pytest's norecursedirs naming the scratch
// directories, pytest's --ignore of each nested member, and hatchling's
// sdist exclusions of the private paths. What scaffold cannot write
// (pytest.ini outranks pyproject.toml; a backend other than hatchling) is a
// row saying so, and the checks that read those settings refuse until the
// project writes them.
func (c *memberContext) projectFileMerges() ([]fileMerge, error) {
	var out []fileMerge
	var nestedAbs []string
	for _, n := range c.ws.NestedMemberPaths(c.member) {
		nestedAbs = append(nestedAbs, filepath.Join(c.ws.Root, filepath.FromSlash(n)))
	}
	for _, t := range c.targets {
		if t.name != declarations.TargetPyPI || c.goBinaryPackaged(t.name) {
			continue
		}
		rel := c.memberPath(joinDir(t.dir, targets.Pyproject))
		data, err := os.ReadFile(filepath.Join(t.abs, targets.Pyproject))
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", rel, err)
		}
		content := data
		var changes []string
		ini := c.memberPath(joinDir(t.dir, "pytest.ini"))
		hasIni, err := fileExists(filepath.Join(t.abs, "pytest.ini"))
		if err != nil {
			return nil, err
		}
		if hasIni {
			out = append(out, fileMerge{path: ini, status: fmt.Sprintf("not written: pytest.ini outranks pyproject.toml, so add 'norecursedirs = %s' under [pytest] there", strings.Join(append(append([]string(nil), PytestDefaultNorecursedirs...), ScratchDirs...), " "))})
		} else {
			merged, changed, err := MergePytestNorecursedirs(rel, content)
			if err != nil {
				return nil, err
			}
			if changed {
				content = merged
				changes = append(changes, "pytest norecursedirs")
			}
		}
		if nested := targets.NestedPathsInside(t.abs, nestedAbs); len(nested) > 0 {
			file, missing, owed, err := targets.MissingNestedExclusions(t.abs, nested)
			if err != nil {
				return nil, err
			}
			if owed && filepath.Base(file) != targets.Pyproject {
				out = append(out, fileMerge{path: ini, status: fmt.Sprintf("not written: add %s to pytest's addopts there, so pytest leaves the nested members out", strings.Join(missing, " "))})
			} else if owed {
				merged, changed, err := targets.MergePytestIgnores(rel, content, missing)
				if err != nil {
					return nil, err
				}
				if changed {
					content = merged
					changes = append(changes, "pytest ignores of nested members")
				}
			}
		}
		backend, err := targets.BuildBackend(t.abs)
		if err != nil {
			return nil, err
		}
		if strings.HasPrefix(backend, "hatchling") {
			merged, changed, err := targets.MergeHatchSdistExclusions(rel, content)
			if err != nil {
				return nil, err
			}
			if changed {
				content = merged
				changes = append(changes, "sdist exclusions")
			}
		} else {
			name := backend
			if name == "" {
				name = "the default build backend"
			}
			out = append(out, fileMerge{path: rel, status: fmt.Sprintf("sdist exclusions not written: it builds with %s and rlsbl writes them for hatchling only; exclude every private path from the sdist (%s), or CI's private-path check refuses the upload", name, strings.Join(targets.ExcludeEntries(), ", "))})
		}
		if len(changes) == 0 {
			out = append(out, fileMerge{path: rel, status: statusUnchanged})
			continue
		}
		out = append(out, fileMerge{path: rel, content: content, status: "updated (" + strings.Join(changes, ", ") + ")"})
	}
	return out, nil
}

func fileExists(path string) (bool, error) {
	_, err := os.Stat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

// orphan is a managed file of the member the run no longer renders.
type orphan struct {
	path   string
	reason string
	// base: a merge base is stored for it.
	base bool
}

// planOrphans are the member's managed files this run renders none of, each
// removed with its merge base. A file that changed since scaffold recorded
// it is refused before anything is written: removing it would lose the
// change, and keeping it would leave a file scaffold no longer manages
// looking managed.
func planOrphans(root string, ws *workspace.Workspace, m declarations.Member, state State, planned map[string]bool, publishSkip string) ([]orphan, error) {
	var out []orphan
	var refused []string
	paths := make([]string, 0, len(state.Files))
	for p := range state.Files {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	for _, p := range paths {
		owner, _ := ws.Declarations.MemberForPath(p)
		if owner.Path != m.Path || planned[p] {
			continue
		}
		reason := "no template renders it for this member any more"
		if p == joinDir(m.Path, workflows.PublishPath) && publishSkip != "" {
			reason = publishSkip
		}
		data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(p)))
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", p, err)
		}
		if FileHash(data) != state.Files[p] {
			refused = append(refused, fmt.Sprintf("%s (%s)", p, reason))
			continue
		}
		base, err := fileExists(filepath.Join(root, filepath.FromSlash(BasePath(p))))
		if err != nil {
			return nil, err
		}
		out = append(out, orphan{path: p, reason: reason, base: base})
	}
	if len(refused) > 0 {
		return nil, fmt.Errorf("rlsbl scaffold no longer renders these files, which changed after scaffold wrote them: %s. Delete each one you no longer need (saferm delete --on-error abort --description \"<why>\" <path>), or restore it to what scaffold last wrote, then run rlsbl scaffold again", strings.Join(refused, "; "))
	}
	return out, nil
}

// refuseMissingBases refuses a member scaffold managed files for when the
// merge bases' directory is missing altogether: merging without bases would
// rebuild every one from history silently, and the operator opts into that
// by creating the directory.
func refuseMissingBases(root string, ws *workspace.Workspace, m declarations.Member, state State, found bool) error {
	if !found {
		return nil
	}
	managed := false
	for p := range state.Files {
		if owner, _ := ws.Declarations.MemberForPath(p); owner.Path == m.Path {
			managed = true
		}
	}
	if !managed {
		return nil
	}
	dir := filepath.Join(root, filepath.FromSlash(declarations.ScaffoldBasesDir))
	exists, err := fileExists(dir)
	if err != nil || exists {
		return err
	}
	return fmt.Errorf("%s records files scaffold manages for the member %q, but %s, which holds their merge bases, is missing, so template updates cannot be merged safely. Create it (mkdir -p %s) and run rlsbl scaffold again: the run rebuilds each managed file's merge base from its most recent `rlsbl scaffold` commit and merges three ways, conflicts written as markers, never overwritten", declarations.ScaffoldStateFile, m.Name, declarations.ScaffoldBasesDir, dir)
}

// tagPlan is the ecosystem tagging a run does: the manifests that gain the
// rlsbl keyword, whether each was clean before, and the repository the
// topic goes on.
type tagPlan struct {
	manifests tagging.Manifests
	clean     map[string]bool
	repo      github.Repository
}

// taggingPlan is the run's tagging, nil while rlsbl:ecosystem-tagging is
// off for the member.
func (c *memberContext) taggingPlan(r *run) (*tagPlan, error) {
	off, err := c.options.IsOff(options.EcosystemTagging, c.member.Path)
	if err != nil || off {
		return nil, err
	}
	repo, err := c.githubRepository()
	if err != nil {
		return nil, fmt.Errorf("%s%s is on, so scaffold adds the rlsbl topic to the repository on GitHub: %w", options.Prefix, options.EcosystemTagging, err)
	}
	plan := &tagPlan{clean: map[string]bool{}, repo: repo}
	for _, t := range c.targets {
		var manifest string
		switch t.name {
		case declarations.TargetNPM:
			plan.manifests.NpmDirs = append(plan.manifests.NpmDirs, t.abs)
			manifest = "package.json"
		case declarations.TargetPyPI:
			plan.manifests.PypiDirs = append(plan.manifests.PypiDirs, t.abs)
			manifest = targets.Pyproject
		default:
			continue
		}
		rel := c.memberPath(joinDir(t.dir, manifest))
		changed, err := r.repo.Status([]string{rel}, git.UntrackedNormal)
		if err != nil {
			return nil, err
		}
		plan.clean[rel] = len(changed) == 0
	}
	return plan, nil
}

// tag puts the keyword into the manifests and the topic on the repository.
// A manifest that was clean before is committed with the scaffold; one the
// operator is editing keeps the keyword uncommitted, and the report says so.
func (r *run) tag(plan *tagPlan) error {
	if plan == nil {
		return nil
	}
	tagged, err := tagging.EnsureTags(r.e, r.ctx.github, plan.repo, plan.manifests)
	if err != nil {
		return err
	}
	root := r.ctx.ws.Root
	for _, abs := range tagged.Edited {
		rel, err := filepath.Rel(root, abs)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		r.row(rel, "updated (rlsbl keyword)")
		if plan.clean[rel] {
			r.commit = append(r.commit, rel)
		} else {
			r.in.Say(fmt.Sprintf("Note: %s has uncommitted changes of its own, so the rlsbl keyword added to it is not committed with the scaffold; it is committed with those changes.", rel))
		}
	}
	if tagged.TopicAdded {
		r.in.Say(fmt.Sprintf("Added the %s topic to %s on GitHub.", tagging.Keyword, plan.repo))
	}
	return nil
}

// nextSteps are the setup steps of each pipeline publishing from CI.
func (c *memberContext) nextSteps() []string {
	var steps []string
	seen := map[string]bool{}
	add := func(s string) {
		if !seen[s] {
			seen[s] = true
			steps = append(steps, s)
		}
	}
	for _, p := range c.ciPipelines() {
		switch {
		case p.Type == declarations.TargetNPM:
			add("Add an NPM_TOKEN secret to the GitHub repository (Settings > Secrets > Actions)")
		case p.Type == declarations.TargetPyPI:
			add("Configure Trusted Publishing for this repository's publish workflow on pypi.org")
		case p.Artifact == declarations.ArtifactLibrary:
			add("Go library: no binaries are built; publishing verifies the module is available on the Go module proxy")
		default:
			add("GoReleaser builds the binaries in CI (no local install needed)")
		}
		if p.HomebrewTap != "" {
			add("Add a HOMEBREW_TAP_TOKEN secret (a token with contents:write on the tap repository)")
		}
	}
	if len(steps) > 0 {
		add(fmt.Sprintf("Write %s/unreleased.toml (`rlsbl release init`), set its bump, then `rlsbl release run --watch`", declarations.ReleasesDir(c.releasable.Name)))
	}
	return steps
}

// report prints the file table, the publish workflow's absence, and the
// next steps.
func (r *run) report(publishSkip string) {
	rows := append([]Row(nil), r.rows...)
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].Path < rows[j].Path })
	if len(rows) > 0 {
		width := 0
		for _, row := range rows {
			if len(row.Path) > width {
				width = len(row.Path)
			}
		}
		r.in.Say("Files:")
		for _, row := range rows {
			r.in.Say(fmt.Sprintf("  %s %s %s", row.Path, strings.Repeat(".", width+4-len(row.Path)), row.Status))
		}
	}
	if publishSkip != "" {
		r.in.Say("No publish workflow: " + publishSkip + ".")
	}
	if steps := r.ctx.nextSteps(); len(steps) > 0 {
		r.in.Say("")
		r.in.Say("Next steps:")
		for i, s := range steps {
			r.in.Say(fmt.Sprintf("  %d. %s", i+1, s))
		}
	}
}

// commitWritten commits what the run changed, through safegit, with the
// Autogenerated trailer and the message a missing merge base is rebuilt
// from. A file with merge conflicts was never added to the list.
func (r *run) commitWritten() error {
	seen := map[string]bool{}
	var paths []string
	for _, p := range r.commit {
		if !seen[p] {
			seen[p] = true
			paths = append(paths, p)
		}
	}
	if len(paths) == 0 {
		return nil
	}
	committed, err := r.repo.Commit(git.CommitRequest{Message: commitMessage, Paths: paths, Autogenerated: true})
	if err != nil {
		return err
	}
	if committed {
		r.in.Say("Committed the scaffold.")
	}
	return nil
}
