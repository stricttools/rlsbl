package scaffold

import (
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/workflows"
)

// The publish workflow is assembled from parts: a header (the release and
// dispatch triggers, one run per tag, the permissions the jobs together
// need), the wait-for-ci job, and the jobs of every pipeline publishing from
// CI. A go or npm or pypi package pipeline renders one job keyed by the
// pipeline's name from scaffold's templates; a go-binary pipeline's jobs,
// and the wait-for-ci job every publish job waits for, are rendered by
// internal/workflows, which the workspace's publish router shares.

// publishWorkflowTemplate is the publish workflow's frame.
const publishWorkflowTemplate = "shared/publish.yml.tpl"

// PublishJob is one or more jobs of a publish workflow, rendered for one
// pipeline.
type PublishJob struct {
	// Keys are the jobs' keys under jobs:.
	Keys []string
	// Text is the jobs' YAML, each key two spaces in, without a final
	// newline.
	Text string
	// Permissions are the workflow permissions the jobs need, by scope
	// ("read" or "write").
	Permissions map[string]string
}

// PublishTarget is everything one pipeline's publish jobs are rendered from.
type PublishTarget struct {
	Pipeline declarations.Pipeline
	// Dir is the directory of the pipeline's target relative to the
	// directory the workflow's jobs run in (the member's directory: a
	// workspace's publish router composes the member's own path); "." is
	// that directory itself.
	Dir string
	// ModulePath is a go library pipeline's module path.
	ModulePath string
	// RegistryURL is an npm package pipeline's registry.
	RegistryURL string
	// PackageManager is an npm package pipeline's package manager: npm,
	// pnpm, or yarn.
	PackageManager string
	// PackageName is an npm go-binary pipeline's main package name.
	PackageName string
	// BinaryName is the binary a go-binary pipeline packages.
	BinaryName string
	// HomebrewTap is set when a go binary pipeline publishes a formula.
	HomebrewTap bool
	// License is the releasable's license, which the manifests a go-binary
	// pipeline generates carry.
	License string
	// Tag is the releasable's tag scheme, from which a go-binary pipeline's
	// jobs read the released version.
	Tag workflows.TagParts
}

// Features are the publishing features a repository may render, from the
// lifecycle-and-license record and the repository's visibility.
type Features struct {
	// BuildAttestations: npm --provenance and PyPI attestations.
	BuildAttestations bool
	// GoProxyNotification: a request to the Go module proxy for a released
	// version, which a go library's publish job makes.
	GoProxyNotification bool
	// RepositoryURLs: a published manifest or formula naming the
	// repository, which a Homebrew formula does.
	RepositoryURLs bool
}

// packageManagerSteps are the commands an npm package's publish job runs
// with each package manager.
var packageManagerSteps = map[string]struct{ setupAction, install, pack, publish string }{
	"npm":  {"", "npm ci", `npm pack --pack-destination "$RUNNER_TEMP/artifacts"`, "npm publish"},
	"pnpm": {"pnpm/action-setup", "pnpm install --frozen-lockfile", `pnpm pack --pack-destination "$RUNNER_TEMP/artifacts"`, "pnpm publish"},
	"yarn": {"", "yarn install --immutable", `yarn pack --out "$RUNNER_TEMP/artifacts/package.tgz"`, "yarn npm publish"},
}

// actionVersions is the pinned action table in the form internal/workflows
// takes it.
func actionVersions() (workflows.ActionVersions, error) {
	table, err := actions()
	if err != nil {
		return nil, err
	}
	out := workflows.ActionVersions{}
	for k, v := range table {
		out[k] = v
	}
	return out, nil
}

// RenderPublishJob renders the publish jobs of one pipeline publishing from
// CI. Publishing a feature the repository may not render is refused, naming
// the rule: a go library asks the Go module proxy, and a Homebrew formula
// names the repository.
func RenderPublishJob(t PublishTarget, f Features) (PublishJob, error) {
	p := t.Pipeline
	if p.Local {
		return PublishJob{}, fmt.Errorf("the pipeline %q publishes from this machine (local = true), so the publish workflow has no job for it", p.Name)
	}
	if p.Artifact == declarations.ArtifactGoBinary {
		return renderGoBinaryJobs(t, f)
	}
	vars := Vars{"jobKey": p.Name, "needs": workflows.WaitForCIJobKey}
	var template string
	perms := map[string]string{"contents": "read"}
	switch {
	case p.Type == declarations.TargetGo && p.Artifact == declarations.ArtifactBinary:
		template = "go/publish-binary.jobs.tpl"
		perms["contents"] = "write"
		if t.HomebrewTap && !f.RepositoryURLs {
			return PublishJob{}, fmt.Errorf("the pipeline %q publishes a Homebrew formula, which names the repository publicly, and the private-repository-publishing rule refuses that for this repository; remove homebrew_tap from the pipeline in %s", p.Name, declarations.ReleasablesFile)
		}
		vars["homebrewTap"] = boolVar(t.HomebrewTap)
	case p.Type == declarations.TargetGo && p.Artifact == declarations.ArtifactLibrary:
		if !f.GoProxyNotification {
			return PublishJob{}, fmt.Errorf("the pipeline %q publishes a Go library, whose publish asks the Go module proxy for the released version and so records the repository's identity publicly, and the private-repository-publishing rule refuses that for this repository; publish the Go code as binaries (a go pipeline with artifact = \"binary\", packaged by npm and pypi go-binary pipelines) in %s", p.Name, declarations.ReleasablesFile)
		}
		if t.ModulePath == "" {
			return PublishJob{}, fmt.Errorf("the pipeline %q publishes a Go library, and its target holds no go.mod declaring the module path", p.Name)
		}
		template = "go/publish-library.jobs.tpl"
		vars["modulePath"] = t.ModulePath
	case p.Type == declarations.TargetNPM && p.Artifact == declarations.ArtifactPackage:
		steps, ok := packageManagerSteps[t.PackageManager]
		if !ok {
			return PublishJob{}, fmt.Errorf("the npm pipeline %q: %q is not a package manager rlsbl publishes with (npm, pnpm, yarn)", p.Name, t.PackageManager)
		}
		setup := ""
		if steps.setupAction != "" {
			ref, err := Action(steps.setupAction)
			if err != nil {
				return PublishJob{}, err
			}
			setup = "      - uses: " + ref + "\n"
		}
		if t.RegistryURL == "" {
			return PublishJob{}, fmt.Errorf("the npm pipeline %q has no registry to publish to", p.Name)
		}
		template = "npm/publish.jobs.tpl"
		perms["id-token"] = "write"
		vars.Merge(Vars{
			"registryUrl":             t.RegistryURL,
			"npm.setupPackageManager": setup,
			"npm.installCommand":      steps.install,
			"npm.packCommand":         steps.pack,
			"npm.publishCommand":      steps.publish,
			"npm.provenance":          boolVar(f.BuildAttestations),
		})
	case p.Type == declarations.TargetPyPI && p.Artifact == declarations.ArtifactPackage:
		template = "pypi/publish.jobs.tpl"
		perms["id-token"] = "write"
		vars.Merge(Vars{
			"pypi.packagesDir":    joinDir(t.Dir, "dist") + "/",
			"pypi.noAttestations": boolVar(!f.BuildAttestations),
		})
	default:
		return PublishJob{}, fmt.Errorf("the pipeline %q (type %q, artifact %q) is not one rlsbl renders a publish job for", p.Name, p.Type, p.Artifact)
	}
	text, err := renderTemplate(template, vars)
	if err != nil {
		return PublishJob{}, err
	}
	text = strings.TrimRight(text, "\n")
	if t.Dir != "." && t.Dir != "" {
		text = jobsInDirectory(text, t.Dir)
	}
	return PublishJob{Keys: []string{p.Name}, Text: text, Permissions: perms}, nil
}

// renderGoBinaryJobs renders the jobs packaging a go binary pipeline's
// binaries for npm or PyPI, through internal/workflows. Each job runs from
// the directory the workflow's jobs run in and names the target's directory
// itself.
func renderGoBinaryJobs(t PublishTarget, f Features) (PublishJob, error) {
	p := t.Pipeline
	if p.BinaryPipeline == "" {
		return PublishJob{}, fmt.Errorf("the %s pipeline %q packages a go binary pipeline's binaries and names no binary_pipeline in %s", p.Type, p.Name, declarations.ReleasablesFile)
	}
	versions, err := actionVersions()
	if err != nil {
		return PublishJob{}, err
	}
	release := workflows.GoBinaryRelease{BinaryJob: p.BinaryPipeline, Binary: t.BinaryName, Tag: t.Tag}
	dir := t.Dir
	if dir == "" {
		dir = "."
	}
	perms := map[string]string{"contents": "read", "id-token": "write"}
	switch p.Type {
	case declarations.TargetNPM:
		text, err := workflows.NPMPackagingJobs(workflows.NPMPackaging{
			GoBinaryRelease: release,
			Package:         t.PackageName,
			Dir:             dir,
			License:         t.License,
			Provenance:      f.BuildAttestations,
			Actions:         versions,
		})
		if err != nil {
			return PublishJob{}, fmt.Errorf("the npm pipeline %q: %w", p.Name, err)
		}
		keys := jobKeys(text)
		return PublishJob{Keys: keys, Text: strings.TrimRight(text, "\n"), Permissions: perms}, nil
	case declarations.TargetPyPI:
		text, err := workflows.WheelJob(workflows.WheelPackaging{
			GoBinaryRelease: release,
			Dir:             dir,
			Attestations:    f.BuildAttestations,
			Actions:         versions,
		})
		if err != nil {
			return PublishJob{}, fmt.Errorf("the pypi pipeline %q: %w", p.Name, err)
		}
		return PublishJob{Keys: jobKeys(text), Text: strings.TrimRight(text, "\n"), Permissions: perms}, nil
	}
	return PublishJob{}, fmt.Errorf("the pipeline %q (type %q) cannot package a go binary; only npm and pypi pipelines can", p.Name, p.Type)
}

// jobKeys are the keys of the jobs in jobs text.
func jobKeys(text string) []string {
	var keys []string
	for _, line := range strings.Split(text, "\n") {
		if jobHeader.MatchString(line) {
			keys = append(keys, strings.TrimSuffix(strings.TrimSpace(line), ":"))
		}
	}
	return keys
}

func boolVar(b bool) string {
	if b {
		return "1"
	}
	return ""
}

// joinDir is rel inside dir, both slash-separated; dir "." is the root.
func joinDir(dir, rel string) string {
	if dir == "." || dir == "" {
		return rel
	}
	return strings.TrimSuffix(dir, "/") + "/" + rel
}

// PublishWorkflow assembles the publish workflow from the wait-for-ci job
// (workflows.WaitForCIJob) and the publish jobs, in the order given. The
// workflow's permissions are each scope's widest grant among the jobs; a
// job key used twice, or one clashing with wait-for-ci, is refused.
func PublishWorkflow(waitForCI string, jobs []PublishJob) (string, error) {
	if len(jobs) == 0 {
		return "", fmt.Errorf("a publish workflow needs at least one publish job")
	}
	perms := map[string]string{}
	seen := map[string]bool{workflows.WaitForCIJobKey: true}
	texts := []string{strings.TrimRight(waitForCI, "\n")}
	for _, j := range jobs {
		for _, key := range j.Keys {
			if seen[key] {
				return "", fmt.Errorf("the publish workflow would carry two jobs keyed %q; rename the pipeline in %s so its jobs' keys differ from every other job's and from %s", key, declarations.ReleasablesFile, workflows.WaitForCIJobKey)
			}
			seen[key] = true
		}
		for scope, level := range j.Permissions {
			if perms[scope] != "write" {
				perms[scope] = level
			}
		}
		texts = append(texts, j.Text)
	}
	scopes := make([]string, 0, len(perms))
	for s := range perms {
		scopes = append(scopes, s)
	}
	sort.Strings(scopes)
	lines := make([]string, len(scopes))
	for i, s := range scopes {
		lines[i] = "  " + s + ": " + perms[s]
	}
	return renderTemplate(publishWorkflowTemplate, Vars{
		"permissions": strings.Join(lines, "\n"),
		"jobs":        strings.Join(texts, "\n"),
	})
}
