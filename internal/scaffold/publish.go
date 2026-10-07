package scaffold

import (
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/pipelines"
)

// The publish workflow is assembled from parts: a header (the release and
// dispatch triggers, one run per tag, the permissions every job together
// needs), the wait-for-ci job, and one job per pipeline publishing from CI,
// keyed by the pipeline's name. Every publish job waits for wait-for-ci,
// which confirms CI passed on the release commit before anything is
// published, and a go-binary job also waits for the go binary job whose
// archives it packages.

// WaitForCIJobKey is the key of the job every publish job waits for.
const WaitForCIJobKey = "wait-for-ci"

// The wait-for-ci job's limits, written into the job's environment so a
// repository changes them by editing the workflow.
const (
	waitTimeoutMinutes      = 20
	waitGraceMinutes        = 5
	waitPollSeconds         = 15
	waitMarkerAttempts      = 5
	waitMarkerRetrySeconds  = 5
	waitJobMarginMinutes    = 5
	ciJobIndent             = "          "
	defaultWheelsDir        = "rlsbl-wheels"
	publishWorkflowTemplate = "shared/publish.yml.tpl"
)

// CIJobNames are the job names each target's CI workflow runs, which name
// the check runs CI reports on a commit (a matrix job's runs add " (...)").
var CIJobNames = map[string][]string{
	declarations.TargetGo:   {"test"},
	declarations.TargetNPM:  {"test"},
	declarations.TargetPyPI: {"test"},
}

// CICheckPattern is the check-run name pattern of the targets' CI jobs and
// their matrix expansions. A target without CI jobs is refused.
func CICheckPattern(targets []string) (string, error) {
	seen := map[string]bool{}
	for _, t := range targets {
		names, ok := CIJobNames[t]
		if !ok {
			return "", fmt.Errorf("the %q target has no CI workflow, so nothing would report the check runs a publish waits for", t)
		}
		for _, n := range names {
			seen[n] = true
		}
	}
	if len(seen) == 0 {
		return "", fmt.Errorf("no target to wait for: a publish workflow waits for the CI of the targets it publishes")
	}
	var names []string
	for n := range seen {
		names = append(names, regexp.QuoteMeta(n))
	}
	sort.Strings(names)
	return `^(` + strings.Join(names, "|") + `)( \(.*\))?$`, nil
}

// WaitForCI declares the wait-for-ci job.
type WaitForCI struct {
	// CheckPattern is the check-run name pattern the job waits for; empty
	// when ResolverScript sets it at run time.
	CheckPattern string
	// ResolverScript, when set, is a shell script run first that writes
	// CI_CHECK_REGEX into $GITHUB_ENV (a workspace's publish workflow, whose
	// releasing project the tag names). Its lines are indented as written.
	ResolverScript string
	// TagInput passes the dispatch's tag input to the job as TAG_INPUT.
	TagInput bool
}

// WaitForCIJob is the wait-for-ci job's YAML, indented to sit under jobs:.
func WaitForCIJob(w WaitForCI) (string, error) {
	if (w.CheckPattern == "") == (w.ResolverScript == "") {
		return "", fmt.Errorf("the wait-for-ci job takes a check-run pattern or a script resolving one, and never both")
	}
	if strings.Contains(w.CheckPattern, "'") {
		return "", fmt.Errorf("the check-run pattern %q holds a single quote, which the workflow's single-quoted value cannot carry", w.CheckPattern)
	}
	tagInput := ""
	if w.TagInput {
		tagInput = "1"
	}
	resolver := ""
	if w.ResolverScript != "" {
		resolver = indentBlock(w.ResolverScript, ciJobIndent)
	}
	text, err := renderTemplate("shared/wait-for-ci.job.tpl", Vars{
		"jobTimeoutMinutes":  strconv.Itoa(waitTimeoutMinutes + waitJobMarginMinutes),
		"timeoutMinutes":     strconv.Itoa(waitTimeoutMinutes),
		"graceMinutes":       strconv.Itoa(waitGraceMinutes),
		"pollSeconds":        strconv.Itoa(waitPollSeconds),
		"markerAttempts":     strconv.Itoa(waitMarkerAttempts),
		"markerRetrySeconds": strconv.Itoa(waitMarkerRetrySeconds),
		"checkPattern":       w.CheckPattern,
		"tagInput":           tagInput,
		"resolverScript":     resolver,
	})
	if err != nil {
		return "", err
	}
	return strings.TrimRight(text, "\n"), nil
}

// indentBlock prefixes every non-empty line of text and drops its final
// newline.
func indentBlock(text, prefix string) string {
	lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
	for i, l := range lines {
		if l != "" {
			lines[i] = prefix + l
		}
	}
	return strings.Join(lines, "\n")
}

// PublishJob is one job of a publish workflow.
type PublishJob struct {
	// Key is the job's key under jobs:.
	Key string
	// Text is the job's YAML, starting with "  <key>:".
	Text string
	// Permissions are the workflow permissions the job needs, by scope
	// ("read" or "write").
	Permissions map[string]string
}

// PublishTarget is everything one pipeline's publish job is rendered from.
type PublishTarget struct {
	Pipeline declarations.Pipeline
	// Dir is the directory of the pipeline's target, repository-relative
	// ("." for the repository root); a job of a target elsewhere runs its
	// steps there.
	Dir string
	// ModulePath is a go pipeline's module path.
	ModulePath string
	// RegistryURL is an npm pipeline's registry.
	RegistryURL string
	// PackageManager is an npm package pipeline's package manager: npm,
	// pnpm, or yarn.
	PackageManager string
	// BinaryName is the binary a go binary pipeline builds, and the binary
	// a go-binary pipeline packages.
	BinaryName string
	// HomebrewTap is set when a go binary pipeline publishes a formula.
	HomebrewTap bool
	// License is the releasable's license, written into the manifests a
	// go-binary pipeline generates.
	License string
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

// RenderPublishJob renders the publish job of one pipeline publishing from
// CI. Publishing a feature the repository may not render is refused, naming
// the rule: a go library asks the Go module proxy, and a Homebrew formula
// names the repository.
func RenderPublishJob(t PublishTarget, f Features) (PublishJob, error) {
	p := t.Pipeline
	if p.Local {
		return PublishJob{}, fmt.Errorf("the pipeline %q publishes from this machine (local = true), so the publish workflow has no job for it", p.Name)
	}
	vars := Vars{"jobKey": p.Name, "needs": WaitForCIJobKey}
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
	case (p.Type == declarations.TargetNPM || p.Type == declarations.TargetPyPI) && p.Artifact == declarations.ArtifactGoBinary:
		if t.BinaryName == "" || p.BinaryPipeline == "" {
			return PublishJob{}, fmt.Errorf("the %s pipeline %q packages a go binary pipeline's binaries, and names no binary pipeline whose binary it packages", p.Type, p.Name)
		}
		if strings.TrimSpace(t.License) == "" {
			return PublishJob{}, fmt.Errorf("the %s pipeline %q generates platform packages, whose manifests carry the releasable's license, and the lifecycle-and-license record holds no license in effect for it", p.Type, p.Name)
		}
		perms["id-token"] = "write"
		vars.Merge(Vars{
			"needs":      "[" + WaitForCIJobKey + ", " + p.BinaryPipeline + "]",
			"binaryName": t.BinaryName,
			"binaryJob":  p.BinaryPipeline,
			"license":    t.License,
		})
		var calls, names []string
		for _, pl := range pipelines.Platforms() {
			if p.Type == declarations.TargetNPM {
				calls = append(calls, fmt.Sprintf("%splatform_package %s %s %s %s %s", ciJobIndent, pl.Name, pl.OS, pl.CPU, pl.GOOS, pl.GOARCH))
				names = append(names, pl.Name)
			} else {
				calls = append(calls, fmt.Sprintf("%swheel %s %s %s", ciJobIndent, pl.WheelTag, pl.GOOS, pl.GOARCH))
			}
		}
		if p.Type == declarations.TargetNPM {
			template = "shared/go-binary/npm.jobs.tpl"
			flag := ""
			if f.BuildAttestations {
				flag = " --provenance"
			}
			vars.Merge(Vars{"platformCalls": strings.Join(calls, "\n"), "platformNames": strings.Join(names, " "), "npm.provenanceFlag": flag})
		} else {
			template = "shared/go-binary/pypi.jobs.tpl"
			vars.Merge(Vars{"wheelCalls": strings.Join(calls, "\n"), "wheelsDir": defaultWheelsDir, "pypi.noAttestations": boolVar(!f.BuildAttestations)})
		}
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
	return PublishJob{Key: p.Name, Text: text, Permissions: perms}, nil
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
// and the publish jobs, in the order given. The workflow's permissions are
// each scope's widest grant among the jobs; a job key used twice, or a key
// clashing with wait-for-ci, is refused.
func PublishWorkflow(waitForCI string, jobs []PublishJob) (string, error) {
	if len(jobs) == 0 {
		return "", fmt.Errorf("a publish workflow needs at least one publish job")
	}
	perms := map[string]string{}
	seen := map[string]bool{WaitForCIJobKey: true}
	texts := []string{waitForCI}
	for _, j := range jobs {
		if seen[j.Key] {
			return "", fmt.Errorf("the publish workflow would carry two jobs keyed %q; pipeline names must differ from each other and from %s", j.Key, WaitForCIJobKey)
		}
		seen[j.Key] = true
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
